import os, time, hmac, hashlib, json, asyncio, sqlite3
from flask import Flask, request, jsonify
from aiogram import Bot, Dispatcher, F
from aiogram.types import LabeledPrice

DB = sqlite3.connect("access.db", check_same_thread=False)
DB.execute("""CREATE TABLE IF NOT EXISTS access (
  user_id  INTEGER PRIMARY KEY,
  until_ts INTEGER
)""")

BOT_TOKEN      = os.getenv("BOT_TOKEN")
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN")
bot = Bot(BOT_TOKEN)
dp  = Dispatcher()

# ─── REST /api/has ─────────────────────────────────────────────────────────
def verify(init_data: str) -> int | None:
    try:
        parts = dict(p.split('=') for p in init_data.split('&'))
        hash_  = parts.pop('hash')
        payload = '\n'.join(f"{k}={v}" for k, v in sorted(parts.items()))
        secret  = hashlib.sha256(BOT_TOKEN.encode()).digest()
        calc    = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, hash_):
            return None
        return int(parts["user%5Bid%5D"])  # user[id] URL‑кодировано
    except Exception:
        return None

app = Flask(__name__)

@app.post("/api/has")
def has_access():
    uid = verify(request.get_data(as_text=True))
    if not uid:
        return jsonify(ok=False), 403
    row = DB.execute("SELECT until_ts FROM access WHERE user_id=?", (uid,)).fetchone()
    now = int(time.time())
    if row and row[0] > now:
        return jsonify(ok=True, has=True, until=row[0])
    return jsonify(ok=True, has=False, until=0)

# ─── Bot logic ─────────────────────────────────────────────────────────────
@dp.message(F.text == "/buy")
async def buy(msg):
    prices = [LabeledPrice("1 день доступа", 29900)]
    await bot.send_invoice(
        chat_id=msg.chat.id,
        title="1 день доступа",
        description="Цифровой контент развлек. характера",
        payload="premium_1d",
        provider_token=PROVIDER_TOKEN,
        currency="RUB",
        prices=prices,
        need_email=True,
        send_email_to_provider=True,
    )

@dp.message(lambda m: m.successful_payment)
async def paid(msg):
    uid = msg.from_user.id
    until = int(time.time()) + 86400  # +1 день
    DB.execute("INSERT INTO access(user_id,until_ts) VALUES(?,?) "
               "ON CONFLICT(user_id) DO UPDATE SET until_ts=?", (uid, until, until))
    DB.commit()
    await msg.answer("✅ Оплата получена, доступ активирован на 1 день.")

# ─── Runner ────────────────────────────────────────────────────────────────
async def main():
    asyncio.create_task(dp.start_polling(bot))
    app.run(host="0.0.0.0", port=8080)

if __name__ == "__main__":
    asyncio.run(main())