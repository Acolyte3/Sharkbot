import os
import websocket
import json
import requests
import time
from collections import deque

# === ENV VARIABLES ===
BOT_TOKEN = os.getenv("8723462226:AAGdtpSafFJ0Wv5z8ii7pLykvvWRHDg9-Fg")
CHAT_ID = os.getenv("6757053692")

if not BOT_TOKEN or not CHAT_ID:
    raise Exception("Missing BOT_TOKEN or CHAT_ID in environment variables")

WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@kline_1m"

volume_history = deque(maxlen=20)

spike_detected = False
spike_direction = None
spike_volume = 0
spike_time = 0

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg})
    except Exception as e:
        print("Telegram Error:", e)

def process_candle(o, c, v):
    global spike_detected, spike_direction, spike_volume, spike_time

    volume_history.append(v)

    if len(volume_history) < 20:
        return

    avg_vol = sum(volume_history) / len(volume_history)

    # STEP 1: Detect spike
    if not spike_detected and v > avg_vol * 2:
        spike_detected = True
        spike_volume = v / avg_vol
        spike_direction = "bullish" if c > o else "bearish"
        spike_time = time.time()
        return

    # STEP 2: Confirm continuation (within 3 mins)
    if spike_detected:
        if time.time() - spike_time > 180:
            spike_detected = False
            return

        continued = v > avg_vol

        direction_ok = (
            (spike_direction == "bullish" and c > o) or
            (spike_direction == "bearish" and c < o)
        )

        if continued and direction_ok:
            move = ((c - o) / o) * 100
            direction_icon = "🟢" if spike_direction == "bullish" else "🔴"

            msg = f"""
🦈 SHARK RADAR ALERT

Direction: {direction_icon} {spike_direction.capitalize()}
Volume Spike: {spike_volume:.2f}x avg
Continuation: Confirmed

Move: {move:.2f}%
Timeframe: 1m → 3m
"""

            send_telegram(msg.strip())
            spike_detected = False

def on_message(ws, message):
    data = json.loads(message)
    k = data['k']

    if k['x']:
        process_candle(
            float(k['o']),
            float(k['c']),
            float(k['v'])
        )

def on_error(ws, error):
    print("WebSocket error:", error)

def on_close(ws, a, b):
    print("Reconnecting in 5 seconds...")
    time.sleep(5)
    run()

def run():
    ws = websocket.WebSocketApp(
        WS_URL,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws.run_forever()

if __name__ == "__main__":
    print("Starting Shark Radar Bot...")
    run()
