#!/usr/bin/env python
from __future__ import annotations
import os
import time
import hmac
import hashlib
import logging
import threading
import sqlite3
import requests
import asyncio
from urllib.parse import parse_qsl

from dotenv import load_dotenv
from flask import Flask, request, jsonify
from flask_cors import CORS, cross_origin

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import PreCheckoutQuery, Message

# ─────────── Настройка ───────────
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN")
PORT = int(os.getenv("PORT", "8080"))

if not BOT_TOKEN:
    raise RuntimeError("❌ BOT_TOKEN не задан")
if not PROVIDER_TOKEN:
    raise RuntimeError("❌ PROVIDER_TOKEN не задан")

BOT_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ─────────── Логирование ───────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("app")

# ─────────── Flask ───────────
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# ─────────── SQLite ───────────
DB = sqlite3.connect("access.db", check_same_thread=False)
DB_LOCK = threading.Lock()
DB.execute("""
    CREATE TABLE IF NOT EXISTS access (
        user_id INTEGER PRIMARY KEY,
        until_ts INTEGER NOT NULL
    )
""")
DB.execute("""
    CREATE TABLE IF NOT EXISTS charges (
        user_id INTEGER,
        charge_id TEXT
    )
""")
DB.commit()

def grant_access(user_id: int, days: int) -> None:
    until_ts = int(time.time()) + days * 86400
    with DB_LOCK:
        DB.execute("""
            INSERT INTO access (user_id, until_ts)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET until_ts = excluded.until_ts
        """, (user_id, until_ts))
        DB.commit()
    log.info("✅ user %s получил доступ до %s", user_id, until_ts)

def verify_initdata(data: str) -> int | None:
    try:
        parts = dict(parse_qsl(data))
        passed_hash = parts.pop("hash")
    except Exception:
        return None

    payload = "\n".join(f"{k}={v}" for k, v in sorted(parts.items()))
    secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
    calc_hash = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calc_hash, passed_hash):
        return None

    try:
        return int(parts["user[id]"])
    except Exception:
        return None

# ─────────── Flask API ───────────

@app.get("/api/has")
@cross_origin()
def api_has():
    init_data = request.args.get("initData", "")
    uid = verify_initdata(init_data)
    if not uid:
        return jsonify(ok=False), 403

    now_ts = int(time.time())
    with DB_LOCK:
        row = DB.execute("SELECT until_ts FROM access WHERE user_id=?", (uid,)).fetchone()
    has_access = bool(row and row[0] > now_ts)
    return jsonify(ok=True, has=has_access, until=row[0] if row else 0)

@app.post("/buy")
@cross_origin()
def api_buy():
    data: dict = request.get_json(silent=True) or {}
    chat_id = data.get("user_id")
    days = int(data.get("days", 1))

    if not chat_id or days not in (1, 30):
        return jsonify(ok=False, error="bad args"), 400

    amount = 10000 if days == 1 else 29300
    payload = f"premium_{days}d"

    invoice_req = {
        "chat_id": chat_id,
        "title": "Доступ к отчёту",
        "description": f"{days} дн. доступа",
        "payload": payload,
        "provider_token": PROVIDER_TOKEN,
        "currency": "RUB",
        "prices": [{"label": f"{days} дн.", "amount": amount}],
        "start_parameter": payload,
        "photo_url": "https://raw.githubusercontent.com/ElenaNub/tg-back/main/pay.jpg",
        "photo_width": 512,
        "photo_height": 256,

        # Добавлено для чека
        "need_email": True,
        "send_email_to_provider": True,
        "provider_data": {
            "receipt": {
                "customer": {
                    "email": ""
                },
                "items": [
                    {
                        "description": f"Доступ к отчёту на {days} дн.",
                        "quantity": 1,
                        "amount": {
                            "value": round(amount / 100, 2),
                            "currency": "RUB"
                        },
                        "vat_code": 1,
                        "payment_mode": "full_payment",
                        "payment_subject": "service"
                    }
                ],
                "tax_system_code": 6
            }
        }
    }

    log.info("▶️ Запрос sendInvoice: %r", invoice_req)
    try:
        r = requests.post(f"{BOT_API_URL}/sendInvoice", json=invoice_req, timeout=10)
        log.info("🔄 Ответ от Telegram: %s", r.text)
        r.raise_for_status()

        resp = r.json()
        if resp.get("ok"):
            return jsonify(ok=True)
        else:
            log.error("❌ Ошибка sendInvoice: %r", resp)
            return jsonify(ok=False, error="invoice failed"), 502

    except requests.RequestException as exc:
        log.exception("❌ Ошибка сети или Telegram: %s", exc)
        return jsonify(ok=False, error="network error"), 500

# ─────────── Aiogram ───────────
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
router = Router()

@router.pre_checkout_query()
async def on_pre_checkout(q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(q.id, ok=True)

@router.message(F.successful_payment)
async def on_success(msg: Message):
    days = 1 if msg.successful_payment.invoice_payload.endswith("1d") else 30
    grant_access(msg.from_user.id, days)
    charge_id = msg.successful_payment.provider_payment_charge_id
    with DB_LOCK:
        DB.execute("INSERT INTO charges (user_id, charge_id) VALUES (?, ?)", (msg.from_user.id, charge_id))
        DB.commit()
    await msg.answer("✅ Оплата получена, доступ продлён!")

dp.include_router(router)

# ─────────── Запуск ───────────
def run_flask():
    log.info("🌐 Flask стартует на 0.0.0.0:%s …", PORT)
    app.run(host="0.0.0.0", port=PORT, use_reloader=False, threaded=False)

async def run_bot():
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("🤖 Запускаю polling-бота …")
    await dp.start_polling(bot, skip_updates=True, reset_webhook=True)

def main():
    threading.Thread(target=run_flask, daemon=True, name="flask").start()
    asyncio.run(run_bot())

if __name__ == "__main__":
    main()