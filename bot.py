import asyncio
import json
import logging
import os
import time
from collections import deque
from datetime import datetime

import websockets
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("SharkBot")

TOKEN   = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
MODE    = os.getenv("TRADE_MODE", "notify")
SIZE    = float(os.getenv("TRADE_SIZE_USDT", "50"))
TESTNET = os.getenv("TESTNET", "true").lower() == "true"

T = {
    "PRICE_1M":  0.25,
    "PRICE_30S": 0.15,
    "VOL_X":     2.5,
    "WHALE":     300_000,
    "FWHALE":    500_000,
    "LIQ":       200_000,
    "BIG_LIQ": 1_000_000,
    "OB":        0.62,
    "FUND":      0.0005,
    "SCORE":     40,
    "GAP":       10,
}

W = {
    "BULL":30,"BEAR":30,"WHALE":20,"FWHALE":25,
    "VOL":15,"PRESS":15,"LIQ":25,"FUND":10,"MOM":20,
}

WS_SPOT = (
    "wss://stream.binance.com:9443/stream?streams="
    "btcusdt@aggTrade/btcusdt@kline_1m/btcusdt@depth5@500ms"
)
WS_FUT = (
    "wss://fstream.binance.com/stream?streams="
    "btcusdt@forceOrder/btcusdt@markPrice@1s/btcusdt@aggTrade"
)

st = {
    "price": None, "mark": None, "fr": None, "avgvol": None,
    "vols": deque(maxlen=30), "prices": deque(maxlen=300),
    "alerted": {}, "sigs": deque(maxlen=50), "orders": {},
}

def ts():
    return datetime.now().strftime("%H:%M:%S")

def fk(n):
    if n is None: return "—"
    if n >= 1_000_000: return f"${n/1_000_000:.2f}M"
    if n >= 1_000: return f"${n/1_000:.1f}K"
    return f"${n:.0f}"

def f(n, d=2):
    if n is None: return "—"
    return f"{n:,.{d}f}"

def ok(key, gap=None):
    gap = gap or T["GAP"]
    now = time.time()
    if now - st["alerted"].get(key, 0) >= gap:
        st["alerted"][key] = now
        return True
    return False

def uid():
    import random, string
    return ''.join(random.choices(string.ascii_lowercase+string.digits, k=6))

def bar(s):
    n = int(s/10)
    return "█"*n + "░"*(10-n)

def score(stype, side):
    now = time.time()
    w = W.get(stype, 10)
    st["sigs"].append((stype, side, now, w))
    bull = bear = 0
    for _, s, t, wt in st["sigs"]:
        if now - t > 90: continue
        if s == "BULL": bull += wt
        elif s == "BEAR": bear += wt
        else: bull += wt*0.5; bear += wt*0.5
    sc = min(100, max(bull, bear))
    dom = "BULL" if bull > bear else "BEAR" if bear > bull else "—"
    return round(sc), dom

async def msg(app, text):
    try:
        await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")
    except Exception as e:
        log.error(f"msg error: {e}")

async def signal(app, stype, title, detail, side=None, order=None):
    sc, dom = score(stype, side or "—")
    if sc < T["SCORE"] and stype not in ("LIQ","FWHALE"):
        return
    bias = "🟢 BULL" if dom=="BULL" else "🔴 BEAR" if dom=="BEAR" else "⚪"
    p = st["price"]
    text = (
        f"*{title}*\n"
        f"━━━━━━━━━━━━━━\n"
        f"📌 `{detail}`\n"
        f"⏰ `{ts()}`\n\n"
        f"Score: `{bar(sc)}` `{sc}/100`\n"
        f"Bias: {bias}\n"
        f"━━━━━━━━━━━━━━\n"
        f"BTC `${f(p,0)}`"
    )
    kb = None
    if MODE == "confirm" and order and sc >= 50:
        cid = uid()
        st["orders"][cid] = order
        asyncio.get_event_loop().call_later(120, lambda: st["orders"].pop(cid, None))
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ {order.get('side','BUY')} {fk(SIZE)}", callback_data=f"t:{cid}"),
            InlineKeyboardButton("❌ Skip", callback_data=f"s:{cid}"),
        ]])
    try:
        await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown", reply_markup=kb)
        log.info(f"Signal: {title} sc={sc}")
    except Exception as e:
        log.error(f"signal error: {e}")

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🦈 *Shark Radar LIVE*\n"
        f"Mode: `{MODE}`\n"
        f"Net: `{'TESTNET' if TESTNET else 'LIVE'}`\n\n"
        f"/status — price\n/mode — mode",
        parse_mode="Markdown"
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    fr = st["fr"]
    await update.message.reply_text(
        f"📡 *Status*\n"
        f"Spot: `${f(st['price'],0)}`\n"
        f"Mark: `${f(st['mark'],0)}`\n"
        f"FR: `{f'{fr*100:.4f}%' if fr else '—'}`\n"
        f"AvgVol: `{fk(st['avgvol'])}`",
        parse_mode="Markdown"
    )

async def cmd_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Mode: *{MODE}*", parse_mode="Markdown")

async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data
    if d.startswith("t:"):
        cid = d[2:]
        order = st["orders"].pop(cid, None)
        if not order:
            await q.edit_message_text("⏰ Expired.")
            return
        await q.edit_message_text(
            f"✅ *Trade Logged*\n"
            f"Side: `{order.get('side')}`\n"
            f"Size: `{fk(SIZE)}`\n"
            f"Price: `${f(st['price'])}`",
            parse_mode="Markdown"
        )
    elif d.startswith("s:"):
        st["orders"].pop(d[2:], None)
        await q.edit_message_text("❌ Skipped.")

async def load_vol():
    try:
        url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=30"
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                klines = await r.json()
        vols = [float(k[5])*float(k[4]) for k in klines]
        for v in vols: st["vols"].append(v)
        st["avgvol"] = sum(vols)/len(vols)
        log.info(f"AvgVol: {fk(st['avgvol'])}")
    except Exception as e:
        log.warning(f"Vol load failed: {e}")

async def spot(app, stream, data):
    if "aggTrade" in stream:
        p = float(data["p"])
        usd = p * float(data["q"])
        st["price"] = p
        st["prices"].append((p, time.time()))
        now = time.time()
        old = next((h for h in st["prices"] if now-h[1] >= 28), None)
        if old and ok("MOM", 12):
            pct = (p-old[0])/old[0]*100
            if abs(pct) >= T["PRICE_30S"]:
                side = "BULL" if pct > 0 else "BEAR"
                await signal(app,"MOM",
                    f"⚡ MICRO {'BULL' if pct>0 else 'BEAR'}",
                    f"{'+' if pct>0 else ''}{pct:.3f}% in 30s @ ${f(p)}",
                    side, {"side":"BUY" if side=="BULL" else "SELL"})
        if usd >= T["WHALE"] and ok(f"WH_{data['m']}", 5):
            s2 = "SELL" if data["m"] else "BUY"
            await signal(app,"WHALE",f"🦈 SPOT WHALE {s2}",
                f"{fk(usd)} @ ${f(p)}",
                "BEAR" if data["m"] else "BULL",{"side":s2})

    elif "kline" in stream:
        k = data["k"]
        op=float(k["o"]); cl=float(k["c"]); vol=float(k["q"])
        pct=(cl-op)/op*100
        st["vols"].append(vol)
        if len(st["vols"])>5:
            st["avgvol"]=sum(st["vols"])/len(st["vols"])
        av=st["avgvol"]
        if av and ok("VOL",12) and vol/av>=T["VOL_X"]:
            await signal(app,"VOL",f"📊 VOL SPIKE x{vol/av:.1f}",
                f"{fk(vol)} vs avg {fk(av)}",None)
        if abs(pct)>=T["PRICE_1M"] and ok("C"+("U" if pct>0 else "D"),10):
            sig="BULL" if pct>0 else "BEAR"
            await signal(app,"BULL" if pct>0 else "BEAR",
                f"{'🟢 STRONG BULL' if pct>0 else '🔴 STRONG BEAR'}",
                f"{'+' if pct>0 else ''}{pct:.3f}% 1m @ ${f(cl)}",
                sig,{"side":"BUY" if sig=="BULL" else "SELL"})

    elif "depth5" in stream:
        bids=data.get("bids",[]); asks=data.get("asks",[])
        bv=sum(float(b[0])*float(b[1]) for b in bids)
        av=sum(float(a[0])*float(a[1]) for a in asks)
        tot=bv+av
        if tot>0:
            br=bv/tot
            if br>=T["OB"] and ok("PB",15):
                await signal(app,"PRESS","📗 BUY WALL",f"Book {br*100:.0f}% bids","BULL")
            elif br<=1-T["OB"] and ok("PS",15):
                await signal(app,"PRESS","📕 SELL WALL",f"Book {(1-br)*100:.0f}% asks","BEAR")

async def fut(app, stream, data):
    if "forceOrder" in stream:
        o=data["o"]
        usd=float(o["p"])*float(o["q"])
        if usd>=T["LIQ"]:
            big=usd>=T["BIG_LIQ"]
            side=o["S"]; lbl="SHORT" if side=="BUY" else "LONG"
            if ok(f"LQ_{side}",3 if big else 6):
                await signal(app,"LIQ",
                    f"{'💥 MEGA' if big else '💧'} LIQ {lbl} WIPED",
                    f"{fk(usd)} @ ${f(float(o['p']))}",
                    "BULL" if side=="BUY" else "BEAR")
    elif "markPrice" in stream:
        st["mark"]=float(data["p"])
        fr=float(data["r"]); st["fr"]=fr
        if abs(fr)>=T["FUND"] and ok("FR",60):
            await signal(app,"FUND",
                f"{'📈 HIGH' if fr>0 else '📉 NEG'} FUNDING",
                f"FR {fr*100:.4f}%","BEAR" if fr>0 else "BULL")
    elif stream=="btcusdt@aggTrade":
        p=float(data["p"]); usd=p*float(data["q"])
        if usd>=T["FWHALE"] and ok(f"FW_{data['m']}",5):
            s2="SELL" if data["m"] else "BUY"
            await signal(app,"FWHALE",f"🐋 FUT WHALE {s2}",
                f"{fk(usd)} @ ${f(p)}",
                "BEAR" if data["m"] else "BULL",{"side":s2})

async def ws_spot(app):
    while True:
        try:
            async with websockets.connect(WS_SPOT, ping_interval=20) as ws:
                log.info("✅ Spot connected")
                async for raw in ws:
                    d=json.loads(raw)
                    await spot(app,d.get("stream",""),d.get("data",{}))
        except Exception as e:
            log.warning(f"Spot WS: {e}")
            await asyncio.sleep(3)

async def ws_fut(app):
    while True:
        try:
            async with websockets.connect(WS_FUT, ping_interval=20) as ws:
                log.info("✅ Futures connected")
                async for raw in ws:
                    d=json.loads(raw)
                    await fut(app,d.get("stream",""),d.get("data",{}))
        except Exception as e:
            log.warning(f"Fut WS: {e}")
            await asyncio.sleep(3)

async def hello(app):
    await asyncio.sleep(4)
    await msg(app,
        f"🦈 *Shark Radar LIVE*\n"
        f"━━━━━━━━━━━━━━\n"
        f"Mode: `{MODE}`\n"
        f"Net: `{'TESTNET ⚠️' if TESTNET else 'LIVE 🔴'}`\n\n"
        f"Watching BTC/USDT 🚀\n/status to check price"
    )

async def main():
    log.info("Starting...")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CallbackQueryHandler(cb))
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    log.info("Polling started ✅")
    await asyncio.gather(load_vol(), hello(app), ws_spot(app), ws_fut(app))

asyncio.run(main())
