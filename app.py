import time
import imaplib
import email
import requests
import re
import logging
import json
import os
import html
from email.header import decode_header
from email.utils import parseaddr
from flask import Flask
from threading import Thread

# ================== НАСТРОЙКИ ==================
TELEGRAM_BOT_TOKEN = "8566965927:AAHF280cyaeLNDNSnu-iOnht7uCpCjEXnko"
TELEGRAM_CHAT_ID = "-1003798450910"

EMAIL_USER = "signalcom46@gmail.com"
EMAIL_PASS = "nbgaewtkyoffhruy"

IMAP_HOST = "imap.gmail.com"

CHECK_INTERVAL = 25
MIN_SCORE = 65  # 🔥 тільки сильні сигнали
STATE_FILE = "bot_state.json"

# ================== WEB (RENDER) ==================
app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    Thread(target=run).start()

# ================== LOG ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("BOT")

seen_ids = set()
sent_hashes = set()
recent_pairs = {}

# ================== HELPERS ==================
def send_telegram(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
        time.sleep(1)
    except:
        pass

def decode_mime(value):
    if not value:
        return ""
    parts = decode_header(value)
    return "".join(
        p.decode(enc or "utf-8", "ignore") if isinstance(p, bytes) else str(p)
        for p, enc in parts
    )

def get_body(msg):
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            return part.get_payload(decode=True).decode(errors="ignore")
    return ""

def is_allowed_sender(from_header):
    email_addr = parseaddr(from_header)[1].lower()
    return email_addr.endswith("@tradingview.com")

# ================== PARSING ==================
def extract(patterns, text):
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return m.group(1)
    return None

def score_signal(body):
    t = body.upper()
    score = 0

    if "BUY" in t or "CALL" in t: score += 15
    if "SELL" in t or "PUT" in t: score += 15
    if "STRONG" in t: score += 20
    if "CONFIRM" in t: score += 10
    if "TP" in t: score += 10
    if "SL" in t: score += 10
    if "ENTRY" in t: score += 10

    if any(x in t for x in ["MAYBE", "WAIT", "POSSIBLE", "UNCLEAR"]):
        score -= 30

    return max(0, min(100, score))

# ================== MAIN MESSAGE ==================
def build_message(body):
    pair = extract([r"([A-Z]{3,5}/?[A-Z]{3,5})"], body) or "N/A"
    price = extract([r"([\d.]+)"], body) or "MARKET"
    tf = extract([r"(1M|3M|5M|15M|1H)"], body) or "5M"

    t = body.upper()

    if "SELL" in t or "PUT" in t:
        direction = "ВНИЗ 🔴"
    elif "BUY" in t or "CALL" in t:
        direction = "ВВЕРХ 🟢"
    else:
        return None, 0, None, None

    score = score_signal(body)

    # анти спам по парі
    key = f"{pair}_{direction}"
    now = time.time()
    if key in recent_pairs and now - recent_pairs[key] < 300:
        return None, 0, None, None
    recent_pairs[key] = now

    # емодзі
    emoji = "🔥🔥🔥" if score >= 80 else "🔥" if score >= 70 else ""

    tf_map = {
        "1M": "1 минута",
        "3M": "3 минуты",
        "5M": "5 минут",
        "15M": "15 минут",
        "1H": "1 час"
    }

    duration = tf_map.get(tf, "5 минут")

    text = f"""
<b>🚀 СИГНАЛ {emoji}</b>

<b>Пара:</b> {pair}
<b>Направление:</b> {direction}
<b>Цена входа:</b> {price}
<b>Время сделки:</b> {duration}
<b>Качество:</b> {score}%
<b>Время:</b> {time.strftime("%H:%M:%S")}
""".strip()

    return text, score, pair, direction

# ================== IMAP ==================
def connect():
    while True:
        try:
            m = imaplib.IMAP4_SSL(IMAP_HOST)
            m.login(EMAIL_USER, EMAIL_PASS)
            return m
        except:
            time.sleep(10)

def check_mail():
    mail = connect()

    while True:
        try:
            mail.select("inbox")
            _, data = mail.search(None, "UNSEEN")

            for num in data[0].split():
                _, msg_data = mail.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])

                from_header = decode_mime(msg.get("From", ""))

                if not is_allowed_sender(from_header):
                    continue

                body = get_body(msg)

                if any(x in body.upper() for x in ["MAYBE","WAIT","UNCLEAR"]):
                    continue

                text, score, pair, direction = build_message(body)

                if not text or score < MIN_SCORE:
                    continue

                send_telegram(text)

                mail.store(num, '+FLAGS', '\\Seen')

        except:
            mail = connect()

        time.sleep(CHECK_INTERVAL)

# ================== START ==================
if __name__ == "__main__":
    keep_alive()
    check_mail()
