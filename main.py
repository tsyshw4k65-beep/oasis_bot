import asyncio, hashlib, hmac, json, os, sqlite3, time
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

import anthropic
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp,
                           Message, WebAppInfo)
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ["WEBAPP_URL"]
MODEL = os.getenv("MODEL", "claude-sonnet-5")
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "50"))
DB_PATH = os.getenv("DB_PATH", "oasis.db")

SYSTEM = (
    "Sen OASIS — foydalanuvchining shaxsiy AI yordamchisisan, Telegram Mini App ichida ishlaysan. "
    "Foydalanuvchi qaysi tilda yozsa, shu tilda javob ber (asosan o'zbek tilida). "
    "Javoblaring qisqa, aniq va do'stona bo'lsin, chunki ular telefon ekranida o'qiladi va ovozda aytilishi mumkin."
)

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
ai = anthropic.AsyncAnthropic()  # ANTHROPIC_API_KEY muhit o'zgaruvchisidan oladi

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("""CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, ts INTEGER NOT NULL)""")
db.execute("CREATE INDEX IF NOT EXISTS idx_user ON messages(user_id, id)")
db.commit()


def check_init_data(init_data: str):
    """Telegram initData imzosini tekshiradi. To'g'ri bo'lsa foydalanuvchi ma'lumotini qaytaradi."""
    data = dict(parse_qsl(init_data, keep_blank_values=True))
    their_hash = data.pop("hash", None)
    if not their_hash or "user" not in data:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    mine = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mine, their_hash):
        return None
    if time.time() - int(data.get("auth_date", 0)) > 86400:
        return None
    return json.loads(data["user"])


def history(uid: int, limit: int = 20):
    rows = db.execute("SELECT role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?",
                      (uid, limit)).fetchall()[::-1]
    while rows and rows[0][0] != "user":  # Claude suhbati "user" bilan boshlanishi kerak
        rows.pop(0)
    return [{"role": r, "content": c} for r, c in rows]


def save(uid: int, role: str, content: str):
    db.execute("INSERT INTO messages (user_id, role, content, ts) VALUES (?,?,?,?)",
               (uid, role, content, int(time.time())))
    db.commit()


def used_today(uid: int) -> int:
    since = int(time.time()) - 86400
    return db.execute("SELECT COUNT(*) FROM messages WHERE user_id=? AND role='user' AND ts>?",
                      (uid, since)).fetchone()[0]


class Chat(BaseModel):
    text: str
    initData: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(text="OASIS", web_app=WebAppInfo(url=WEBAPP_URL)))
    task = asyncio.create_task(dp.start_polling(bot))
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse("index.html")


@app.post("/api/chat")
async def chat(body: Chat):
    user = check_init_data(body.initData)
    if not user:
        raise HTTPException(status_code=401, detail="Telegram orqali oching")
    uid, text = user["id"], body.text.strip()[:2000]
    if not text:
        raise HTTPException(status_code=400)
    if used_today(uid) >= DAILY_LIMIT:
        return {"reply": "Bugungi limit tugadi. Ertaga yana urinib ko'ring."}
    save(uid, "user", text)
    try:
        r = await ai.messages.create(model=MODEL, max_tokens=600, system=SYSTEM,
                                     messages=history(uid))
        reply = "".join(b.text for b in r.content if b.type == "text").strip() or "..."
    except Exception:
        reply = "Hozir javob bera olmayapman. Birozdan keyin qayta urinib ko'ring."
        db.execute("DELETE FROM messages WHERE id=(SELECT MAX(id) FROM messages WHERE user_id=?)", (uid,))
        db.commit()
        return {"reply": reply}
    save(uid, "assistant", reply)
    return {"reply": reply}


@dp.message(CommandStart())
async def start(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="OASIS'ni ochish", web_app=WebAppInfo(url=WEBAPP_URL))]])
    await m.answer("Salom! Men OASIS. Pastdagi tugmani bosing.", reply_markup=kb)
