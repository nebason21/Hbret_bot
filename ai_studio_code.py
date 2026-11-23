import os, json, hmac, hashlib, random, asyncio
from urllib.parse import parse_qsl
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, Float
from sqlalchemy.orm import declarative_base, sessionmaker
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
WEBAPP_URL = os.getenv("RENDER_EXTERNAL_URL", "http://localhost:8000")
HOUSE_PERCENT = 0.20  # 20% ለቦቱ, 80% ለአሸናፊ

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./bingo.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    tg_id = Column(BigInteger, primary_key=True)
    name = Column(String, default="Player")
    phone = Column(String, nullable=True)
    balance = Column(Float, default=0.0)
    referred_by = Column(BigInteger, nullable=True)

class DepositReq(Base):
    __tablename__ = "deposits"
    id = Column(Integer, primary_key=True, autoincrement=True)
    tg_id = Column(BigInteger)
    amount = Column(Float)
    method = Column(String)
    tx_ref = Column(String)
    status = Column(String, default="pending")

Base.metadata.create_all(bind=engine)

# --- 80/20 Live Bingo Room State ---
class BingoRoom:
    def __init__(self, bet=25.0):
        self.bet = bet
        self.status = "waiting"  # waiting | playing | ended
        self.players = {}        # tg_id -> {name, card_no, grid}
        self.taken_cards = {}    # card_no -> tg_id
        self.drawn = []
        self.connections = set()
        self.countdown = 15

    def make_card(self, card_no: int):
        rng = random.Random(card_no * 7919)
        ranges = [(1,15), (16,30), (31,45), (46,60), (61,75)]
        cols = [rng.sample(range(a, b+1), 5) for a, b in ranges]
        cols[2][2] = 0  # FREE center
        return [[cols[c][r] for c in range(5)] for r in range(5)]

    def check_bingo(self, grid):
        d = set(self.drawn) | {0}
        for r in range(5):
            if all(grid[r][c] in d for c in range(5)): return True
        for c in range(5):
            if all(grid[r][c] in d for r in range(5)): return True
        if all(grid[i][i] in d for i in range(5)): return True
        if all(grid[i][4-i] in d for i in range(5)): return True
        return False

    def state(self):
        pool = len(self.players) * self.bet
        winner_prize = round(pool * (1 - HOUSE_PERCENT), 2)
        return {
            "type": "state", "status": self.status, "bet": self.bet,
            "countdown": self.countdown, "players_count": len(self.players),
            "winner_prize": winner_prize, "house_percent": int(HOUSE_PERCENT * 100),
            "taken_cards": list(self.taken_cards.keys()), "drawn": self.drawn
        }

    async def broadcast(self, msg: dict):
        dead = []
        for ws in list(self.connections):
            try: await ws.send_json(msg)
            except: dead.append(ws)
        for ws in dead: self.connections.discard(ws)

room = BingoRoom(bet=25.0)

async def game_loop():
    while True:
        await asyncio.sleep(1)
        if room.status == "waiting" and len(room.players) >= 1:
            room.countdown -= 1
            await room.broadcast(room.state())
            if room.countdown <= 0:
                room.status = "playing"
                room.drawn = []
                await room.broadcast(room.state())
                bag = list(range(1, 76))
                random.shuffle(bag)
                while room.status == "playing" and bag:
                    await asyncio.sleep(3.5)
                    if room.status != "playing": break
                    ball = bag.pop()
                    room.drawn.append(ball)
                    await room.broadcast({"type": "ball", "ball": ball, "drawn": room.drawn})
                if room.status == "playing":
                    room.status = "waiting"
                    room.players.clear()
                    room.taken_cards.clear()
                    room.drawn.clear()
                    room.countdown = 15
                    await room.broadcast(room.state())

# --- Telegram Bot Setup ---
ptb = Application.builder().token(BOT_TOKEN).build() if BOT_TOKEN else None

def verify_tg_user(init_data: str) -> dict:
    if not BOT_TOKEN or not init_data:
        raise HTTPException(status_code=401, detail="Unauthorized")
    parsed = dict(parse_qsl(init_data))
    hash_val = parsed.pop("hash", "")
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    if hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest() != hash_val:
        raise HTTPException(status_code=401, detail="Invalid signature")
    return json.loads(parsed["user"])

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ref = int(context.args[0].split("_")[1]) if context.args and context.args[0].startswith("ref_") else None
    db = SessionLocal()
    user = db.query(User).filter(User.tg_id == u.id).first()
    if not user:
        user = User(tg_id=u.id, name=u.first_name, balance=0.0, referred_by=ref if ref != u.id else None)
        db.add(user)
        db.commit()
    bal, phone = user.balance, user.phone
    db.close()

    kb = [
        [InlineKeyboardButton("🎮 ጨዋታ ጀምር (Video Bingo)", web_app=WebAppInfo(url=WEBAPP_URL))],
        [InlineKeyboardButton("📝 Register (ስልክ)", callback_data="ask_reg"),
         InlineKeyboardButton("💰 Deposit (ገቢ)", callback_data="ask_dep")],
        [InlineKeyboardButton("💳 Balance (ቀሪ ሂሳብ)", callback_data="chk_bal")]
    ]
    status_txt = "✅ ተመዝግበዋል" if phone else "⚠️ ለመጫወት መጀመሪያ Register ይጫኑ"
    await update.message.reply_text(
        f"🏆 **እንኳን ወደ Hbret Video Bingo በደህና መጡ!**\n\n"
        f"👤 ስም፦ {u.first_name}\n💰 ቀሪ ሂሳብ፦ **{bal} ETB**\n📌 ሁኔታ፦ {status_txt}\n\n"
        f"🔥 ከእያንዳንዱ ጨዋታ **80%** ለአሸናፊ ይከፈላል!\n👇 ከታች **ጨዋታ ጀምር** የሚለውን ይጫኑ፦",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown"
    )

async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "ask_reg":
        kb = ReplyKeyboardMarkup([[KeyboardButton("📱 ስልክ ቁጥሬን አጋራ (Register)", request_contact=True)]], resize_keyboard=True, one_time_keyboard=True)
        await q.message.reply_text("👇 ለመመዝገብ ከታች ያለውን ቁልፍ ይጫኑ፦", reply_markup=kb)
    elif q.data == "ask_dep":
        await q.message.reply_text("💰 ገንዘብ ለማስገባት **ጨዋታ ጀምር** የሚለውን ከፍተው **Deposit** ክፍል ውስጥ Telebirr/CBE መረጃ ያስገቡ።")
    elif q.data == "chk_bal":
        db = SessionLocal()
        user = db.query(User).filter(User.tg_id == q.from_user.id).first()
        bal = user.balance if user else 0.0
        db.close()
        await q.message.reply_text(f"💳 የእርስዎ ቀሪ ሂሳብ፦ **{bal} ETB**", parse_mode="Markdown")
    elif q.data.startswith(("app_", "rej_")) and q.from_user.id == ADMIN_ID:
        act, dep_id = q.data.split("_")
        db = SessionLocal()
        dep = db.query(DepositReq).filter(DepositReq.id == int(dep_id)).first()
        if dep and dep.status == "pending":
            if act == "app":
                dep.status = "approved"
                u = db.query(User).filter(User.tg_id == dep.tg_id).first()
                if u: u.balance += dep.amount
                db.commit()
                await q.edit_message_text(f"✅ Approved: {dep.amount} ETB ለ {dep.tg_id}")
                await context.bot.send_message(dep.tg_id, f"✅ ያስገቡት **{dep.amount} ETB** ተረጋግጦ ሂሳብዎ ላይ ገብቷል!", parse_mode="Markdown")
            else:
                dep.status = "rejected"
                db.commit()
                await q.edit_message_text(f"❌ Rejected: {dep.id}")
                await context.bot.send_message(dep.tg_id, "❌ የላኩት የDeposit መረጃ አልተረጋገጠም።")
        db.close()

async def contact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    c = update.message.contact
    db = SessionLocal()
    u = db.query(User).filter(User.tg_id == update.effective_user.id).first()
    if u:
        u.phone = c.phone_number
        db.commit()
    db.close()
    await update.message.reply_text("✅ በተሳካ ሁኔታ ተመዝግበዋል! አሁን `/start` ብለው **ጨዋታ ጀምር** የሚለውን ይክፈቱ።", reply_markup=ReplyKeyboardRemove())

if ptb:
    ptb.add_handler(CommandHandler("start", cmd_start))
    ptb.add_handler(CallbackQueryHandler(cb_handler))
    ptb.add_handler(MessageHandler(filters.CONTACT, contact_handler))

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(game_loop())
    if ptb:
        await ptb.initialize()
        await ptb.start()
        if WEBAPP_URL.startswith("https://"):
            await ptb.bot.set_webhook(f"{WEBAPP_URL}/webhook")
    yield
    task.cancel()
    if ptb:
        await ptb.stop()
        await ptb.shutdown()

app = FastAPI(lifespan=lifespan)

@app.get("/health")
def health(): return {"status": "ok"}

@app.post("/webhook")
async def webhook(req: Request):
    if ptb:
        await ptb.process_update(Update.de_json(await req.json(), ptb.bot))
    return {"ok": True}

@app.get("/", response_class=HTMLResponse)
def index():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/api/me")
async def api_me(req: Request):
    body = await req.json()
    u = verify_tg_user(body.get("initData", ""))
    db = SessionLocal()
    user = db.query(User).filter(User.tg_id == u["id"]).first()
    if not user:
        user = User(tg_id=u["id"], name=u.get("first_name", "Player"))
        db.add(user); db.commit()
    res = {"id": user.tg_id, "name": user.name, "balance": user.balance, "registered": bool(user.phone)}
    db.close()
    return res

@app.post("/api/buy_card")
async def buy_card(req: Request):
    body = await req.json()
    u = verify_tg_user(body.get("initData", ""))
    card_no = int(body["card_no"])
    if room.status != "waiting": raise HTTPException(400, "ጨዋታው ተጀምሯል፣ ቀጣዩን ዙር ይጠብቁ!")
    if card_no in room.taken_cards: raise HTTPException(400, "ይህ ካርድ ተይዟል!")
    db = SessionLocal()
    user = db.query(User).filter(User.tg_id == u["id"]).first()
    if not user or user.balance < room.bet:
        db.close()
        raise HTTPException(400, "በቂ ቀሪ ሂሳብ የለም! Deposit ያድርጉ።")
    user.balance -= room.bet
    bal = user.balance
    db.commit(); db.close()
    grid = room.make_card(card_no)
    room.taken_cards[card_no] = u["id"]
    room.players[u["id"]] = {"name": u.get("first_name", "Player"), "card_no": card_no, "grid": grid}
    await room.broadcast(room.state())
    return {"grid": grid, "balance": bal}

@app.post("/api/claim_bingo")
async def claim_bingo(req: Request):
    body = await req.json()
    u = verify_tg_user(body.get("initData", ""))
    p = room.players.get(u["id"])
    if not p or room.status != "playing": raise HTTPException(400, "ንቁ ጨዋታ የለም!")
    if not room.check_bingo(p["grid"]): raise HTTPException(400, "ገና BINGO አልሞላም!")
    prize = round(len(room.players) * room.bet * (1 - HOUSE_PERCENT), 2)
    db = SessionLocal()
    user = db.query(User).filter(User.tg_id == u["id"]).first()
    if user: user.balance += prize; db.commit()
    db.close()
    room.status = "ended"
    await room.broadcast({"type": "winner", "name": p["name"], "card_no": p["card_no"], "prize": prize})
    await asyncio.sleep(5)
    room.status = "waiting"; room.players.clear(); room.taken_cards.clear(); room.drawn.clear(); room.countdown = 15
    await room.broadcast(room.state())
    return {"prize": prize}

@app.post("/api/deposit")
async def api_deposit(req: Request):
    body = await req.json()
    u = verify_tg_user(body.get("initData", ""))
    amt, method, tx = float(body["amount"]), body["method"], body["tx_ref"]
    db = SessionLocal()
    dep = DepositReq(tg_id=u["id"], amount=amt, method=method, tx_ref=tx)
    db.add(dep); db.commit(); dep_id = dep.id; db.close()
    if ptb and ADMIN_ID:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve", callback_data=f"app_{dep_id}"), InlineKeyboardButton("❌ Reject", callback_data=f"rej_{dep_id}")]])
        await ptb.bot.send_message(ADMIN_ID, f"💰 **አዲስ Deposit ጥያቄ**\n👤 {u.get('first_name')} (`{u['id']}`)\n💵 መጠን፦ {amt} ETB\n🏦 ባንክ፦ {method}\n🧾 TX: `{tx}`", reply_markup=kb, parse_mode="Markdown")
    return {"ok": True}

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    room.connections.add(ws)
    await ws.send_json(room.state())
    try:
        while True: await ws.receive_text()
    except WebSocketDisconnect:
        room.connections.discard(ws)