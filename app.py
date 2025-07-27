import os, time, json, hmac, hashlib, sqlite3, asyncio, threading, requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from aiogram import Bot, Dispatcher, F
from aiogram.types import LabeledPrice, PreCheckoutQuery, SuccessfulPayment

load_dotenv()                                         # загружаем .env

BOT_TOKEN       = os.getenv("BOT_TOKEN")              # токен бота
PROVIDER_TOKEN  = os.getenv("PROVIDER_TOKEN")         # ЮKassa TEST / LIVE
BOT_API_URL     = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ───── база доступа ──────────────────────────────────────────────────────
DB = sqlite3.connect("access.db", check_same_thread=False)
DB.execute(
    "CREATE TABLE IF NOT EXISTS access ("
    "user_id INTEGER PRIMARY KEY, "
    "until_ts INTEGER)"
)

def grant_access(user_id: int, days: int):
    until = int(time.time()) + days * 86400
    DB.execute(
        "INSERT INTO access(user_id, until_ts) VALUES(?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET until_ts = excluded.until_ts",
        (user_id, until)
    )
    DB.commit()

# ───── валидация initData от Mini‑App ─────────────────────────────────────
def verify_initdata(data: str) -> int | None:
    try:
        parts = dict(p.split("=", 1) for p in data.split("&"))
        passed_hash = parts.pop("hash")
        payload     = "\n".join(f"{k}={v}" for k, v in sorted(parts.items()))
        secret      = hashlib.sha256(BOT_TOKEN.encode()).digest()
        calc_hash   = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc_hash, passed_hash):
            return None
        return int(parts["user%5Bid%5D"])                   # user[id]
    except Exception:
        return None

# ───── HTTP‑API (Flask) ──────────────────────────────────────────────────
app = Flask(__name__)

@app.post("/api/has")
def api_has():
    uid = verify_initdata(request.get_data(as_text=True))
    if not uid:
        return jsonify(ok=False), 403
    row = DB.execute("SELECT until_ts FROM access WHERE user_id=?", (uid,)).fetchone()
    now = int(time.time())
    if row and row[0] > now:
        return jsonify(ok=True, has=True, until=row[0])
    return jsonify(ok=True, has=False, until=0)

@app.post("/buy")
def api_buy():
    data = request.get_json(silent=True) or {}
    chat_id = data.get("user_id")
    days    = data.get("days", 1)

    if not chat_id or days not in (1, 30):
        return jsonify(ok=False, error="bad args"), 400

    amount = 29900 if days == 1 else 150000          # копейки
    payload = f"premium_{days}d"

    # отправляем счёт через Bot API
    invoice = dict(
        chat_id      = chat_id,
        title        = "Доступ к отчёту",
        description  = f"{days} дней доступа",
        payload      = payload,
        provider_token = PROVIDER_TOKEN,
        currency     = "RUB",
        prices       = [dict(label=f"{days} дн.", amount=amount)],
        need_email   = True,
        send_email_to_provider = True,
    )
    r = requests.post(f"{BOT_API_URL}/sendInvoice", json=invoice, timeout=10)
    if r.ok and r.json().get("ok"):
        return jsonify(ok=True)
    return jsonify(ok=False, error=r.text), 500

# ───── Bot (aiogram) ─────────────────────────────────────────────────────
bot = Bot(BOT_TOKEN)
dp  = Dispatcher()

@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(q.id, ok=True)

@dp.message(F.successful_payment)
async def on_paid(msg):
    days = 1 if msg.successful_payment.invoice_payload.endswith("1d") else 30
    grant_access(msg.from_user.id, days)
    await msg.answer("✅ Оплата получена, доступ продлён!")

# ───── запуск: Flask + polling в одном процессе ──────────────────────────
def run_flask():
    app.run(host="0.0.0.0", port=8080, use_reloader=False)

async def run_bot():
    await dp.start_polling(bot)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()   # HTTP‑API
    asyncio.run(run_bot())                                    # bot polling