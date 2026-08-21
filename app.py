import os, re, secrets
from datetime import datetime, timezone
from typing import Optional

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
                license_code TEXT
            )""")
        con.commit()

@app.on_event("startup")
def startup():
    init_db()

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
                "INSERT INTO purchases VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (request_id, machine_id, data.email or "", data.app_version or "",
                 utc_now(), "waiting_payment_details", "", "")
            )
        con.commit()

    text = (
        "🔔 НОВАЯ ЗАЯВКА FennecReach\n\n"
        f"Заявка: {request_id}\n"
        f"Код ПК: {machine_id}\n"
        f"Email: {data.email or '-'}\n"
        f"Версия: {data.app_version or '-'}\n\n"
        "Команды:\n"
        f"/details {request_id} ТЕКСТ_РЕКВИЗИТОВ\n"
        f"/paid {request_id}\n"
        f"/license {request_id} ЛИЦЕНЗИОННЫЙ_КОД"
    )
    try:
        tg_send(text)
        sent = True
    except Exception:
        sent = False

    return {"ok": True, "request_id": request_id,
            "status": "waiting_payment_details",
            "telegram_sent": sent}

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
        "payment_message": row["payment_message"] or "",
        "license_code": row["license_code"] or ""
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
            "/details FR-XXXXXXXX ТЕКСТ — отправить пользователю реквизиты\n"
            "/paid FR-XXXXXXXX — отметить оплату\n"
            "/license FR-XXXXXXXX ЛИЦЕНЗИОННЫЙ_КОД — выдать лицензию\n"
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
        with db() as con:
            with con.cursor() as cur:
                cur.execute(
                    "UPDATE purchases SET status=%s WHERE request_id=%s",
                    ("payment_confirmed", parts[1].strip()))
                changed = cur.rowcount
            con.commit()
        tg_send("✅ Оплата отмечена." if changed else "❌ Заявка не найдена.")

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
            "/details FR-XXXXXXXX ТЕКСТ — отправить реквизиты пользователю\n"
            "/paid FR-XXXXXXXX — отметить оплату\n"
            "/license FR-XXXXXXXX КОД — передать лицензию пользователю"
        )

    else:
        tg_send("Неизвестная команда. Отправь /help.")

    return {"ok": True}
