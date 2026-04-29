"""
SHARK RADAR BOT - BTC/USDT
Works with Python 3.13 + python-telegram-bot 20.x or 21.x
"""

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
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("SharkRadar")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TRADE_MODE       = os.getenv("TRADE_MODE", "notify")
TRADE_SIZE_USDT  = float(os.getenv("TRADE_SIZE_USDT", "50"))
USE_FUTURES      = os.getenv("USE_FUTURES", "true").lower() == "true"
TESTNET          = os.getenv("TESTNET", "true").lower() == "true"

T = {
    "PRICE_1M":   0.25,
    "PRICE_30S":  0.15,
    "VOLUME_X":   2.5,
    "WHALE":      300_000,
    "FUT_WHALE":  500_000,
    "LIQ_MIN":    200_000,
    "LIQ_BIG":  1_000_000,
    "OB_IMBAL":   0.62,
    "FUNDING":    0.0005,
    "SCORE_MIN":  40,
    "ALERT_GAP":  10,
}

SIGNAL_WEIGHTS = {
    "BULLISH": 30, "BEARISH": 30, "WHALE": 20, "FWHALE": 25,
    "VOLUME": 15, "PRESSURE": 15, "LIQ": 25, "FUNDING": 10, "MOMENTUM": 20,
}

EMOJI = {
    "BULLISH": "🟢", "BEARISH": "🔴", "WHALE": "🦈", "FWHALE": "🐋",
    "VOLUME": "📊", "PRESSURE": "⚖️", "LIQ": "💥", "FUNDING": "💰", "MOMENTUM": "⚡",
}

WS_SPOT = (
    "wss://stream.binance.com:9443/stream?streams="
    "btcusdt@aggTrade/btcusdt@kline_1m/btcusdt@depth5@500ms"
)
WS_FUT = (
    "wss://fstream.binance.com/stream?streams="
    "btcusdt@forceOrder/btcusdt@markPrice@1s/btcusdt@aggTrade"
)

state = {
    "price": None,
    "mark_price": None,
    "funding_rate": None,
    "avg_volume": None,
    "volume_window": deque(maxlen=30),
    "price_history": deque(maxlen=300),
    "last_alert": {},
    "signal_window": deque(maxlen=50),
    "pending_trades": {},
}

def now_ts():
    return datetime.now().strftime("%H:%M:%S")

def fmtk(n):
    if n is None: return "—"
    if n >= 1_000_000: return f"${n/1_000_000:.2f}M"
    if n >= 1_000: return f"${n/1_000:.1f}K"
    return f"${n:.0f}"

def fmt(n, d=2):
    if n is None: return "—"
    return f"{n:,.{d}f}"

def can_alert(key, gap=None):
    gap = gap or T["ALERT_GAP"]
    now = time.time()
    if now - state["last_alert"].get(key, 0) >= gap:
        state["last_alert"][key] = now
        return True
    return False

def uid():
    import random, string
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))

def score_bar(s):
    n = int(s / 10)
    return "█" * n + "░" * (10 - n)

def confluence(sig_type, side):
    now = time.time()
    w = SIGNAL_WEIGHTS.get(sig_type, 10)
    state["signal_window"].append((sig_type, side, now, w))
    bull, bear = 0, 0
    for _, s, t, wt in state["signal_window"]:
        if now - t > 90: continue
        if s == "BULL": bull += wt
        elif s == "BEAR": bear += wt
        else: bull += wt * 0.5; bear += wt * 0.5
    score = min(100, max(bull, bear))
    dominant = "BULL" if bull > bear else "BEAR" if bear > bull else "—"
    return round(score), dominant

async def notify(app, text):
    try:
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="Markdown"
        )
    except Exception as e:
        log.error(f"Notify error: {e}")

async def send_signal(app, sig_type, title, detail, side=None, order=None):
    score, dominant = confluence(sig_type, side or "—")
    if score < T["SCORE_MIN"] and sig_type not in ("LIQ", "FWHALE"):
        return
    bias = "🟢 BULL" if dominant == "BULL" else "🔴 BEAR" if dominant == "BEAR" else "⚪ —"
    emoji = EMOJI.get(sig_type, "📡")
    p = state["price"]
    msg = (
        f"{emoji} *{title}*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📌 `{detail}`\n"
        f"⏰ `{now_ts()}`\n\n"
        f"📊 *Score:* `{score_bar(score)}` `{score}/100`\n"
        f"Bias: {bias}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💲 BTC `${fmt(p, 0)}`"
    )
    keyboard = None
    if TRADE_MODE == "confirm" and order and score >= 50:
        cid = uid()
        state["pending_trades"][cid] = order
        asyncio.get_event_loop().call_later(
            120, lambda: state["pending_trades"].pop(cid, None)
        )
        btn_side = order.get("side", "BUY")
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"✅ {btn_side} {fmtk(TRADE_SIZE_USDT)}",
                callback_data=f"trade:{cid}"
            ),
            InlineKeyboardButton("❌ Skip", callback_data=f"skip:{cid}"),
        ]])
    try:
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID, text=msg,
            parse_mode="Markdown", reply_markup=keyboard
        )
        log.info(f"Signal: {title} score={score}")
    except Exception as e:
        log.error(f"Signal error: {e}")

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🦈 *Shark Radar Bot — LIVE*\n\n"
        f"Mode: `{TRADE_MODE.upper()}`\n"
        f"Network: `{'TESTNET ⚠️' if TESTNET else 'LIVE 🔴'}`\n\n"
        "Commands:\n/status — live price\n/mode — current mode",
        parse_mode="Markdown"
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    fr = state["funding_rate"]
    await update.message.reply_text(
        f"📡 *Live Status*\n"
        f"Spot:    `${fmt(state['price'], 0)}`\n"
        f"Mark:    `${fmt(state['mark_price'], 0)}`\n"
        f"Funding: `{f'{fr*100:.4f}%' if fr else '—'}`\n"
        f"Avg Vol: `{fmtk(state['avg_volume'])}`",
        parse_mode="Markdown"
    )

async def cmd_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Mode: *{TRADE_MODE.upper()}*", parse_mode="Markdown")

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("trade:"):
        cid = data.split(":", 1)[1]
        order = state["pending_trades"].pop(cid, None)
        if not order:
            await query.edit_message_text("⏰ Expired.")
            return
        await query.edit_message_text(
            f"✅ *Trade Signal Logged*\n"
            f"Side: `{order.get('side')}`\n"
            f"Size: `{fmtk(TRADE_SIZE_USDT)}`\n"
            f"Price: `${fmt(state['price'])}`\n\n"
            f"_Connect Binance API to auto-execute_",
            parse_mode="Markdown"
        )
    elif data.startswith("skip:"):
        cid = data.split(":", 1)[1]
        state["pending_trades"].pop(cid, None)
        await query.edit_message_text("❌ Skipped.")

async def load_avg_volume():
    try:
        url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=30"
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                klines = await r.json()
        vols = [float(k[5]) * float(k[4]) for k in klines]
        for v in vols:
            state["volume_window"].append(v)
        state["avg_volume"] = sum(vols) / len(vols)
        log.info(f"Avg volume: {fmtk(state['avg_volume'])}")
    except Exception as e:
        log.warning(f"Avg volume load failed: {e}")

async def process_spot(app, stream, data):
    if "aggTrade" in stream:
        p   = float(data["p"])
        usd = p * float(data["q"])
        state["price"] = p
        state["price_history"].append((p, time.time()))

        now = time.time()
        old = next((h for h in state["price_history"] if now - h[1] >= 28), None)
        if old and can_alert("MOMENTUM", 12):
            pct = (p - old[0]) / old[0] * 100
            if abs(pct) >= T["PRICE_30S"]:
                side = "BULL" if pct > 0 else "BEAR"
                await send_signal(app, "MOMENTUM",
                    f"⚡ MICRO {'BULL' if pct>0 else 'BEAR'}",
                    f"{'+' if pct>0 else ''}{pct:.3f}% in 30s @ ${fmt(p)}",
                    side, {"side": "BUY" if side=="BULL" else "SELL"})

        if usd >= T["WHALE"] and can_alert(f"WHALE_{data['m']}", 5):
            ts2 = "SELL" if data["m"] else "BUY"
            await send_signal(app, "WHALE", f"🦈 SPOT WHALE {ts2}",
                f"{fmtk(usd)} @ ${fmt(p)}",
                "BEAR" if data["m"] else "BULL",
                {"side": ts2})

    elif "kline" in stream:
        k   = data["k"]
        op  = float(k["o"]); cl = float(k["c"]); vol = float(k["q"])
        pct = (cl - op) / op * 100
        state["volume_window"].append(vol)
        if len(state["volume_window"]) > 5:
            state["avg_volume"] = sum(state["volume_window"]) / len(state["volume_window"])

        av = state["avg_volume"]
        if av and can_alert("VOLUME", 12) and vol / av >= T["VOLUME_X"]:
            await send_signal(app, "VOLUME",
                f"📊 VOL SPIKE ×{vol/av:.1f}",
                f"{fmtk(vol)} vs avg {fmtk(av)}", None)

        if abs(pct) >= T["PRICE_1M"] and can_alert("CANDLE_"+("UP" if pct>0 else "DN"), 10):
            sig  = "BULLISH" if pct > 0 else "BEARISH"
            side = "BULL"    if pct > 0 else "BEAR"
            await send_signal(app, sig,
                f"{'🟢 STRONG BULL' if pct>0 else '🔴 STRONG BEAR'}",
                f"{'+' if pct>0 else ''}{pct:.3f}% 1m @ ${fmt(cl)}",
                side, {"side": "BUY" if side=="BULL" else "SELL"})

    elif "depth5" in stream:
        bids = data.get("bids", []); asks = data.get("asks", [])
        bv = sum(float(b[0])*float(b[1]) for b in bids)
        av = sum(float(a[0])*float(a[1]) for a in asks)
        tot = bv + av
        if tot > 0:
            br = bv / tot
            if br >= T["OB_IMBAL"] and can_alert("PRESS_BUY", 15):
                await send_signal(app, "PRESSURE", "📗 BUY WALL",
                    f"Book {br*100:.0f}% bids", "BULL")
            elif br <= 1-T["OB_IMBAL"] and can_alert("PRESS_SELL", 15):
                await send_signal(app, "PRESSURE", "📕 SELL WALL",
                    f"Book {(1-br)*100:.0f}% asks", "BEAR")

async def process_futures(app, stream, data):
    if "forceOrder" in stream:
        o   = data["o"]
        usd = float(o["p"]) * float(o["q"])
        if usd >= T["LIQ_MIN"]:
            big  = usd >= T["LIQ_BIG"]
            side = o["S"]
            lbl  = "SHORT" if side=="BUY" else "LONG"
            if can_alert(f"LIQ_{side}", 3 if big else 6):
                await send_signal(app, "LIQ",
                    f"{'💥 MEGA' if big else '💧'} LIQ — {lbl} WIPED",
                    f"{fmtk(usd)} @ ${fmt(float(o['p']))}",
                    "BULL" if side=="BUY" else "BEAR")

    elif "markPrice" in stream:
        state["mark_price"]  = float(data["p"])
        fr = float(data["r"])
        state["funding_rate"] = fr
        if abs(fr) >= T["FUNDING"] and can_alert("FUNDING", 60):
            await send_signal(app, "FUNDING",
                f"{'📈 HIGH' if fr>0 else '📉 NEG'} FUNDING",
                f"FR {fr*100:.4f}%",
                "BEAR" if fr > 0 else "BULL")

    elif stream == "btcusdt@aggTrade":
        p   = float(data["p"])
        usd = p * float(data["q"])
        if usd >= T["FUT_WHALE"] and can_alert(f"FWHALE_{data['m']}", 5):
            ts2 = "SELL" if data["m"] else "BUY"
            await send_signal(app, "FWHALE", f"🐋 FUTURES WHALE {ts2}",
                f"{fmtk(usd)} @ ${fmt(p)}",
                "BEAR" if data["m"] else "BULL",
                {"side": ts2})

async def listen_spot(app):
    while True:
        try:
            async with websockets.connect(WS_SPOT, ping_interval=20) as ws:
                log.info("✅ Spot WS connected")
                async for raw in ws:
                    msg = json.loads(raw)
                    await process_spot(app, msg.get("stream",""), msg.get("data",{}))
        except Exception as e:
            log.warning(f"Spot WS error: {e} — retry in 3s")
            await asyncio.sleep(3)

async def listen_futures(app):
    while True:
        try:
            async with websockets.connect(WS_FUT, ping_interval=20) as ws:
                log.info("✅ Futures WS connected")
                async for raw in ws:
                    msg = json.loads(raw)
                    await process_futures(app, msg.get("stream",""), msg.get("data",{}))
        except Exception as e:
            log.warning(f"Futures WS error: {e} — retry in 3s")
            await asyncio.sleep(3)

async def startup_msg(app):
    await asyncio.sleep(3)
    await notify(app,
        f"🦈 *Shark Radar is LIVE*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Mode: `{TRADE_MODE.upper()}`\n"
        f"Network: `{'TESTNET ⚠️' if TESTNET else 'LIVE 🔴'}`\n\n"
        f"Watching BTC/USDT 🚀\nSend /status to check price"
    )

async def run_bot():
    log.info("Starting Shark Radar Bot...")
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("mode",   cmd_mode))
    app.add_handler(CallbackQueryHandler(handle_callback))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    log.info("Bot polling started")

    await asyncio.gather(
        load_avg_volume(),
        startup_msg(app),
        listen_spot(app),
        listen_futures(app),
    )

if __name__ == "__main__":
    asyncio.run(run_bot())
