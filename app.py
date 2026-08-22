import os, re, secrets
import json
from datetime import datetime, timezone
from typing import Optional
from pathlib import Path

import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

APP_NAME = "FennecReach License Server"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_OWNER_CHAT_ID = os.getenv("TELEGRAM_OWNER_CHAT_ID", "").strip()
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "").strip()
PAYMENT_TRC20_ADDRESS = os.getenv("PAYMENT_TRC20_ADDRESS", "TUYstja5qm4abCciKfxfB6uEE7y6xDKAJV").strip()
PAYMENT_PRICE_USDT = os.getenv("PAYMENT_PRICE_USDT", "99").strip()
PAYMENT_NETWORK = os.getenv("PAYMENT_NETWORK", "TRON (TRC20)").strip()
LICENSE_PRIVATE_KEY_FILE = os.getenv("LICENSE_PRIVATE_KEY_FILE", "/etc/secrets/OWNER_PRIVATE_KEY.pem").strip()

app = FastAPI(title=APP_NAME, version="1.0.0")

def utc_now():
    return datetime.now(timezone.utc).isoformat()

def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    with db() as con:
        with con.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS purchases(
                request_id TEXT PRIMARY KEY,
                machine_id TEXT NOT NULL,
                email TEXT,
                app_version TEXT,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                payment_message TEXT,
                license_code TEXT,
                payment_proof TEXT
            )""")
            cur.execute("ALTER TABLE purchases ADD COLUMN IF NOT EXISTS payment_proof TEXT")
        con.commit()

@app.on_event("startup")
def startup():
    init_db()

def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")

def generate_license_code(machine_id_value: str) -> str:
    key_path = Path(LICENSE_PRIVATE_KEY_FILE)
    if not key_path.exists():
        raise RuntimeError(
            f"Закрытый ключ лицензии не найден: {LICENSE_PRIVATE_KEY_FILE}"
        )

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519, ed448, rsa, ec, padding

    private_key = serialization.load_pem_private_key(
        key_path.read_bytes(),
        password=None,
    )

    payload = {
        "product": "FennecReach",
        "machine_id": machine_id_value.strip(),
        "license": "FULL",
        "issued_at": utc_now(),
    }
    payload_raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    if isinstance(private_key, (ed25519.Ed25519PrivateKey, ed448.Ed448PrivateKey)):
        signature = private_key.sign(payload_raw)
    elif isinstance(private_key, rsa.RSAPrivateKey):
        signature = private_key.sign(
            payload_raw,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    elif isinstance(private_key, ec.EllipticCurvePrivateKey):
        signature = private_key.sign(
            payload_raw,
            ec.ECDSA(hashes.SHA256()),
        )
    else:
        raise RuntimeError(
            f"Неподдерживаемый тип закрытого ключа: {type(private_key).__name__}"
        )

    return _b64u(payload_raw) + "." + _b64u(signature)

def payment_message():
    return (
        f"Сумма: {PAYMENT_PRICE_USDT} USDT\n"
        f"Сеть: {PAYMENT_NETWORK}\n"
        f"Адрес: {PAYMENT_TRC20_ADDRESS}\n\n"
        "Отправляйте только USDT в сети TRON (TRC20)."
    )

def tg_send(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_OWNER_CHAT_ID:
        return False
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_OWNER_CHAT_ID, "text": text, "disable_web_page_preview": True},
        timeout=20,
    )
    r.raise_for_status()
    return True

class PurchaseRequest(BaseModel):
    machine_id: str = Field(min_length=8, max_length=128)
    email: Optional[str] = Field(default=None, max_length=254)
    app_version: Optional[str] = Field(default=None, max_length=64)

class PaymentUpdate(BaseModel):
    message: str = Field(min_length=1, max_length=2000)

class LicenseUpdate(BaseModel):
    license_code: str = Field(min_length=8, max_length=10000)

class PaymentProof(BaseModel):
    proof: str = Field(min_length=3, max_length=4000)

@app.get("/")
def root():
    return {"ok": True, "service": APP_NAME, "version": "1.0.0"}

@app.get("/health")
def health():
    return {"ok": True, "service": APP_NAME}

@app.post("/purchase/request")
def purchase_request(data: PurchaseRequest):
    machine_id = re.sub(r"[^A-Za-z0-9\-_:]", "", data.machine_id.strip())[:128]
    if len(machine_id) < 8:
        raise HTTPException(400, "Invalid machine_id")

    request_id = "FR-" + secrets.token_hex(4).upper()

    with db() as con:
        with con.cursor() as cur:
            cur.execute(
                """INSERT INTO purchases
                   (request_id,machine_id,email,app_version,created_at,status,payment_message,license_code,payment_proof)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (request_id, machine_id, data.email or "", data.app_version or "",
                 utc_now(), "payment_details_ready", payment_message(), "", "")
            )
        con.commit()

    text = (
        "🔔 НОВАЯ ЗАЯВКА FennecReach\n\n"
        f"Заявка: {request_id}\n"
        f"Код ПК: {machine_id}\n"
        f"Email: {data.email or '-'}\n"
        f"Версия: {data.app_version or '-'}\n\n"
        "Реквизиты уже показаны покупателю автоматически.\n\n"
        "Команды:\n"
        f"/paid {request_id}\n"
        f"/license {request_id} ЛИЦЕНЗИОННЫЙ_КОД"
    )
    try:
        tg_send(text)
        sent = True
    except Exception:
        sent = False

    return {
        "ok": True,
        "request_id": request_id,
        "status": "payment_details_ready",
        "payment_message": payment_message(),
        "payment_address": PAYMENT_TRC20_ADDRESS,
        "payment_price_usdt": PAYMENT_PRICE_USDT,
        "payment_network": PAYMENT_NETWORK,
        "telegram_sent": sent,
    }

@app.get("/purchase/status/{request_id}")
def purchase_status(request_id: str):
    with db() as con:
        with con.cursor() as cur:
            cur.execute("SELECT * FROM purchases WHERE request_id=%s", (request_id.strip(),))
            row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Request not found")
    return {
        "ok": True,
        "request_id": row["request_id"],
        "status": row["status"],
        "payment_message": payment_message(),
        "payment_address": PAYMENT_TRC20_ADDRESS,
        "payment_price_usdt": PAYMENT_PRICE_USDT,
        "payment_network": PAYMENT_NETWORK,
        "payment_proof": row.get("payment_proof") or "",
        "license_code": row["license_code"] or ""
    }

@app.post("/purchase/confirm/{request_id}")
def purchase_confirm(request_id: str, data: PaymentProof):
    request_id = request_id.strip()
    proof = data.proof.strip()

    with db() as con:
        with con.cursor() as cur:
            cur.execute(
                """UPDATE purchases
                   SET payment_proof=%s, status=%s
                   WHERE request_id=%s
                   RETURNING machine_id,email,app_version""",
                (proof, "payment_proof_sent", request_id)
            )
            row = cur.fetchone()
        con.commit()

    if not row:
        raise HTTPException(404, "Request not found")

    try:
        tg_send(
            "💳 ПОДТВЕРЖДЕНИЕ ОПЛАТЫ FennecReach\n\n"
            f"Заявка: {request_id}\n"
            f"Код ПК: {row['machine_id']}\n"
            f"Email: {row['email'] or '-'}\n"
            f"Версия: {row['app_version'] or '-'}\n\n"
            "Подтверждение / TxID:\n"
            f"{proof}\n\n"
            "После проверки оплаты отправьте:\n"
            f"/paid {request_id}\n\n"
            "После /paid сервер сам создаст и выдаст настоящую FULL-лицензию."
        )
        sent = True
    except Exception:
        sent = False

    return {
        "ok": True,
        "request_id": request_id,
        "status": "payment_proof_sent",
        "telegram_sent": sent,
    }

def require_admin(request: Request):
    if not ADMIN_API_KEY:
        raise HTTPException(503, "ADMIN_API_KEY is not configured")
    if not secrets.compare_digest(request.headers.get("x-admin-key", ""), ADMIN_API_KEY):
        raise HTTPException(403, "Forbidden")

@app.post("/admin/payment/{request_id}")
def admin_payment(request_id: str, data: PaymentUpdate, request: Request):
    require_admin(request)
    with db() as con:
        with con.cursor() as cur:
            cur.execute(
                "UPDATE purchases SET payment_message=%s, status=%s WHERE request_id=%s",
                (data.message.strip(), "payment_details_ready", request_id.strip()))
            changed = cur.rowcount
        con.commit()
    if changed == 0:
        raise HTTPException(404, "Request not found")
    return {"ok": True}

@app.post("/admin/license/{request_id}")
def admin_license(request_id: str, data: LicenseUpdate, request: Request):
    require_admin(request)
    with db() as con:
        with con.cursor() as cur:
            cur.execute(
                "UPDATE purchases SET license_code=%s, status=%s WHERE request_id=%s",
                (data.license_code.strip(), "license_ready", request_id.strip()))
            changed = cur.rowcount
        con.commit()
    if changed == 0:
        raise HTTPException(404, "Request not found")
    return {"ok": True}

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if TELEGRAM_WEBHOOK_SECRET:
        got = request.headers.get("x-telegram-bot-api-secret-token", "")
        if not secrets.compare_digest(got, TELEGRAM_WEBHOOK_SECRET):
            raise HTTPException(403, "Forbidden")

    update = await request.json()
    msg = update.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    text = (msg.get("text") or "").strip()

    if TELEGRAM_OWNER_CHAT_ID and chat_id != TELEGRAM_OWNER_CHAT_ID:
        return {"ok": True}

    if not text.startswith("/"):
        tg_send(
            "✅ FennecReach Bot на связи.\n"
            "Сервер работает и получает сообщения.\n\n"
            "Команды:\n"
            "/start — проверить бота\n"
            "/help — список команд"
        )
        return {"ok": True}

    parts = text.split(maxsplit=2)
    cmd = parts[0].split("@")[0].lower()

    if cmd == "/start":
        tg_send(
            "🦊 FennecReach Bot запущен.\n\n"
            "✅ Telegram подключён\n"
            "✅ Сервер Render работает\n"
            "✅ Заявки на покупку будут приходить сюда\n\n"
            "Команды:\n"
            "/paid FR-XXXXXXXX — подтвердить полученную оплату\n"
            "/help — помощь"
        )

    elif cmd == "/details" and len(parts) >= 3:
        with db() as con:
            with con.cursor() as cur:
                cur.execute(
                    "UPDATE purchases SET payment_message=%s, status=%s WHERE request_id=%s",
                    (parts[2].strip(), "payment_details_ready", parts[1].strip()))
                changed = cur.rowcount
            con.commit()
        tg_send("✅ Реквизиты отправлены в FennecReach." if changed else "❌ Заявка не найдена.")

    elif cmd == "/paid" and len(parts) >= 2:
        request_id = parts[1].strip()
        try:
            with db() as con:
                with con.cursor() as cur:
                    cur.execute(
                        "SELECT machine_id,email FROM purchases WHERE request_id=%s",
                        (request_id,)
                    )
                    row = cur.fetchone()

                    if not row:
                        tg_send("❌ Заявка не найдена.")
                        return {"ok": True}

                    license_code = generate_license_code(row["machine_id"])

                    cur.execute(
                        """UPDATE purchases
                           SET status=%s, license_code=%s
                           WHERE request_id=%s""",
                        ("license_ready", license_code, request_id)
                    )
                con.commit()

            tg_send(
                "✅ Оплата подтверждена.\n"
                "🔑 Настоящая лицензия FULL создана автоматически и отправлена в FennecReach.\n\n"
                f"Заявка: {request_id}\n"
                f"Email: {row['email'] or '-'}"
            )
        except Exception as e:
            tg_send(
                "❌ Не удалось автоматически создать лицензию.\n\n"
                f"Заявка: {request_id}\n"
                f"Ошибка: {e}"
            )

    elif cmd == "/license" and len(parts) >= 3:
        with db() as con:
            with con.cursor() as cur:
                cur.execute(
                    "UPDATE purchases SET license_code=%s, status=%s WHERE request_id=%s",
                    (parts[2].strip(), "license_ready", parts[1].strip()))
                changed = cur.rowcount
            con.commit()
        tg_send("🔑 Лицензия отправлена пользователю." if changed else "❌ Заявка не найдена.")

    elif cmd == "/help":
        tg_send(
            "FennecReach команды:\n\n"
            "/start — проверить работу бота\n"
            "/paid FR-XXXXXXXX — подтвердить полученную оплату\n"
            "После /paid лицензия создаётся и выдаётся автоматически."
        )

    else:
        tg_send("Неизвестная команда. Отправь /help.")

    return {"ok": True}
