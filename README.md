# FennecReach License Server

Render build command:
`pip install -r requirements.txt`

Render start command:
`uvicorn app:app --host 0.0.0.0 --port $PORT`

Environment variables:
- TELEGRAM_BOT_TOKEN
- TELEGRAM_OWNER_CHAT_ID
- TELEGRAM_WEBHOOK_SECRET
- ADMIN_API_KEY
- DB_PATH (optional)

Telegram commands:
- /details FR-XXXXXXXX ТЕКСТ
- /paid FR-XXXXXXXX
- /license FR-XXXXXXXX ЛИЦЕНЗИОННЫЙ_КОД

Важно: SQLite здесь подходит для первого теста. Для реальных продаж перед релизом подключим постоянную БД, чтобы заявки не терялись при пересоздании инстанса Render.
