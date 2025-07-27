#!/usr/bin/env python
"""
app.py ― Telegram‑бот + HTTP‑API «доступ по подписке».
Одним процессом поднимаем:
  • Flask‑сервер (порт берётся из $PORT, как требуют PaaS‑платформы);
  • aiogram‑бот (polling).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import sqlite3
import threading
import time
from typing import Any

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request

# aiogram‑3.x
from aiogram import Bot, Dispatcher, F
from aiogram.types import PreCheckoutQuery

# ─────────── настройка окружения ────────────────────────────────────────
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("Переменная окружения BOT_TOKEN не задана")

PORT = int(os.getenv("PORT", "8080"))  # PaaS обычно пробрасывает PORT
BOT_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ─────────── логирование ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("app")

# ─────────── SQLite ─────────────────────────────────────────────────────
DB = sqlite3.connect("access.db", check_same_thread=False)
DB.execute(
    """
    CREATE TABLE IF NOT EXISTS access (
        user_id  INTEGER PRIMARY KEY,
        until_ts INTEGER NOT NULL
    )
    """
)
DB.commit()


def grant_access(user_id: int, days: int) -> None:
    """Дать <days> дней доступа пользователю <user_id>."""
    until_ts = int(time.time()) + days * 86400
    DB.execute(
        """
        INSERT INTO access (user_id, until_ts)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE
            SET until_ts = excluded.until_ts
        """,
        (user_id, until_ts),
    )
    DB.commit()
    log.info("✅ user %s получил доступ до %s", user_id, until_ts)


# ─────────── проверка initData от mini‑apps ──────────────────────────────
def verify_initdata(data: str) -> int | None:
    """Вернёт Telegram user_id, если hash сходится, иначе None."""
    try:
        parts = dict(p.split("=", 1) for p in data.split("&"))
        passed_hash = parts.pop("hash")
    except (ValueError, KeyError):
        return None

    payload = "\n".join(f"{k}={v}" for k, v in sorted(parts.items()))
    secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
    calc_hash = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc_hash, passed_hash):
        return None

    try:
        return int(parts["user%5Bid%5D"])  # поле user[id] приходит url‑encoded
    except (KeyError, ValueError):
        return None


# ─────────── Flask HTTP‑API ─────────────────────────────────────────────
app = Flask(__name__)


@app.post("/api/has")
def api_has():
    uid = verify_initdata(request.get_data(as_text=True))
    if not uid:
        return jsonify(ok=False), 403

    now_ts = int(time.time())
    row = DB.execute("SELECT until_ts FROM access WHERE user_id=?", (uid,)).fetchone()
    has_access = bool(row and row[0] > now_ts)
    return jsonify(ok=True, has=has_access, until=row[0] if row else 0)


@app.post("/buy")
def api_buy():
    data: dict[str, Any] = request.get_json(silent=True) or {}

    chat_id = data.get("user_id")
    days = int(data.get("days", 1))

    if not chat_id or days not in (1, 30):
        return jsonify(ok=False, error="bad args"), 400

    amount = 29_900 if days == 1 else 150_000  # копейки
    payload = f"premium_{days}d"

    invoice = dict(
        chat_id=chat_id,
        title="Доступ к отчёту",
        description=f"{days} дн. доступа",
        payload=payload,
        provider_token=PROVIDER_TOKEN,
        currency="RUB",
        prices=[dict(label=f"{days} дн.", amount=amount)],
        need_email=True,
        send_email_to_provider=True,
    )

    try:
        r = requests.post(f"{BOT_API_URL}/sendInvoice", json=invoice, timeout=10)
        r.raise_for_status()
        if r.json().get("ok"):
            return jsonify(ok=True)
        log.error("Telegram ответил ошибкой: %s", r.text)
    except requests.RequestException as exc:
        log.exception("sendInvoice упал: %s", exc)

    return jsonify(ok=False, error="invoice failed"), 500


# ─────────── Telegram‑бот (aiogram‑3) ───────────────────────────────────
bot = Bot(BOT_TOKEN)
dp = Dispatcher()


@dp.pre_checkout_query()
async def on_pre_checkout(q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(q.id, ok=True)


@dp.message(F.successful_payment)
async def on_success(msg):
    days = 1 if msg.successful_payment.invoice_payload.endswith("1d") else 30
    grant_access(msg.from_user.id, days)
    await msg.answer("✅ Оплата получена, доступ продлён!")


# ─────────── запуск ─────────────────────────────────────────────────────
def run_flask():
    log.info("Запускаю Flask на 0.0.0.0:%s …", PORT)
    # threaded=False, т.к. Flask уже в отдельном thread
    app.run(host="0.0.0.0", port=PORT, use_reloader=False, threaded=False)


async def run_bot():
    log.info("Запускаю polling‑бота …")
    await dp.start_polling(bot)


def main() -> None:
    threading.Thread(target=run_flask, daemon=True, name="flask").start()
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()