import time
import imaplib
import email
import requests
import re
import logging
import os
import hashlib
from email.header import decode_header
from flask import Flask
from threading import Thread
from pymongo import MongoClient

# ================== НАЛАШТУВАННЯ ==================
TELEGRAM_BOT_TOKEN = "8566965927:AAHF280cyaeLNDNSnu-iOnht7uCpCjEXnko"
TELEGRAM_CHAT_ID = "-1003798450910"
EMAIL_USER = "signalcom46@gmail.com"
EMAIL_PASS = "nbgaewtkyoffhruy"
IMAP_HOST = "imap.gmail.com"

# ВСТАВ СВОЄ ПОСИЛАННЯ ВІД MONGODB Atlas:
MONGO_URI = "ТВІЙ_РЯДОК_ПІДКЛЮЧЕННЯ_ТУТ"

# ================== ПАРАМЕТРИ ФІЛЬТРАЦІЇ ==================
CHECK_INTERVAL = 25
MIN_SCORE = 75
PAIR_COOLDOWN_SEC = 300

# 💰 Пункт 2: Тільки перевірені пари
ALLOWED_PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "BTCUSD", "ETHUSD", "EURJPY"]

BAD_WORDS = ["MAYBE", "WAIT", "UNCLEAR", "LOW CONFIDENCE", "SIDEWAYS", "RANGE", "FLAT", "CHOPPY"]
GOOD_WORDS = ["CONFIRM", "BREAKOUT", "RETEST", "STRONG"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("PocketBot")

# ================== БД ТА ВЕБ-СЕРВЕР ==================
try:
    client = MongoClient(MONGO_URI)
    db = client['trading_bot']
    collection = db['state']
    logger.info("✅ MongoDB Connected")
except Exception as e:
    logger.error(f"❌ DB Error: {e}"); collection = None

app = Flask(__name__)
@app.route("/")
def home(): return "PocketBot v3.0 (PRO) is ACTIVE 🚀"

def run():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def keep_alive(): Thread(target=run, daemon=True).start()

# ================== СТАН (DB) ==================
def load_state():
    if collection is None: return set(), {}, set()
    doc = collection.find_one({"_id": "v3_state"})
    if not doc: return set(), {}, set()
    return set(doc.get("seen_ids", [])), doc.get("recent_pairs", {}), set(doc.get("sent_hashes", []))

def save_state(seen_ids, recent_pairs, sent_hashes):
    if collection is None: return
    collection.update_one(
        {"_id": "v3_state"},
        {"$set": {
            "seen_ids": list(seen_ids)[-1000:],
            "recent_pairs": recent_pairs,
            "sent_hashes": list(sent_hashes)[-1000:]
        }}, upsert=True
    )

# ================== ЛОГІКА ТА ПАРСИНГ ==================
def make_hash(body):
    return hashlib.md5(body[:500].encode('utf-8')).hexdigest()

def is_good_time():
    hour = int(time.strftime("%H"))
    return 9 <= hour <= 22

# 🧠 Пункт 3: Покращений скоринг
def score_signal(body):
    t = body.upper()
    score = 0
    if "STRONG" in t: score += 25
    if "CONFIRMED" in t: score += 20
    if "HIGH PROBABILITY" in t: score += 25
    if any(x in t for x in ["BUY", "CALL", "SELL", "PUT"]): score += 15
    if "BREAKOUT" in t: score += 15
    if "RETEST" in t: score += 10
    if "TREND" in t: score += 10
    if "TP" in t: score += 10
    if "SL" in t: score += 10
    
    # Штрафи
    if "MAYBE" in t: score -= 40
    if "WAIT" in t: score -= 30
    if "UNCLEAR" in t: score -= 50
    return max(0, min(100, score))

def build_message(body):
    raw_pair = re.search(r"([A-Z]{3,5}/?[A-Z]{3,5})", body, re.I)
    pair = raw_pair.group(1).replace("/", "").upper() if raw_pair else "N/A"
    
    # 💰 Пункт 2: Перевірка пари
    if pair not in ALLOWED_PAIRS:
        return "FILTERED_PAIR", 0, None, None

    price_m = re.search(r"(?:ENTRY|PRICE|ВХОД)[:\s]*([\d.,]+)", body, re.I)
    price = price_m.group(1) if price_m else "MARKET"
    
    tf_m = re.search(r"\b(1M|3M|5M|15M|30M|1H)\b", body, re.I)
    tf = tf_m.group(1).upper() if tf_m else "5M"
    
    t = body.upper()
    if any(x in t for x in ["SELL", "PUT", "DOWN", "ВНИЗ"]): direction = "ВНИЗ 🔴"
    elif any(x in t for x in ["BUY", "CALL", "UP", "ВВЕРХ"]): direction = "ВВЕРХ 🟢"
    else: return None, 0, None, None

    score = score_signal(body)
    
    # 🚀 Пункт 4: Тег "Рекомендовано"
    tag = "🔥 <b>РЕКОМЕНДОВАНО</b>\n" if score >= 85 else ""
    status = "🟢 СИЛЬНЫЙ" if score >= 85 else "🟡 СРЕДНИЙ" if score >= 75 else "🔴 СЛАБЫЙ"
    
    emoji = "🔥🔥🔥" if score >= 85 else "🔥🔥" if score >= 75 else "🔥"
    text = f"""
{tag}<b>🚀 СИГНАЛ {emoji}</b>

<b>📊 Пара:</b> <code>{pair}</code>
<b>📈 Направление:</b> <b>{direction}</b>
<b>💰 Цена входа:</b> <code>{price}</code>
<b>⏱ Время сделки:</b> {tf}
<b>📊 Качество:</b> {score}% ({status})

<b>🕒 Время сигнала:</b> {time.strftime('%H:%M:%S')}
""".strip()
    return text, score, pair, direction

def send_telegram(text):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", 
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
        time.sleep(1) 
    except: pass

# ================== ЦИКЛ ==================
def connect_mail():
    while True:
        try:
            m = imaplib.IMAP4_SSL(IMAP_HOST)
            m.login(EMAIL_USER, EMAIL_PASS); return m
        except: time.sleep(10)

def check_mail():
    seen_ids, recent_pairs, sent_hashes = load_state()
    mail = connect_mail()

    while True:
        try:
            if not is_good_time():
                time.sleep(60); continue

            mail.select("inbox")
            _, data = mail.search(None, "UNSEEN")
            for num in data[0].split():
                mid = num.decode()
                if mid in seen_ids: continue
                
                _, msg_data = mail.fetch(num, "(RFC822)")
                if not msg_data or not msg_data[0]: continue
                
                seen_ids.add(mid)
                mail.store(num, '+FLAGS', '\\Seen')
                
                msg = email.message_from_bytes(msg_data[0][1])
                body = ""
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode(errors="ignore")

                if not body or not ("@tradingview.com" in msg.get("From", "").lower()):
                    save_state(seen_ids, recent_pairs, sent_hashes); continue

                # 📊 Пункт 1: Фільтр по кількості слів
                if len(body.split()) < 5:
                    save_state(seen_ids, recent_pairs, sent_hashes); continue

                if any(x in body.upper() for x in BAD_WORDS):
                    save_state(seen_ids, recent_pairs, sent_hashes); continue

                if not any(x in body.upper() for x in GOOD_WORDS):
                    save_state(seen_ids, recent_pairs, sent_hashes); continue

                h = make_hash(body)
                if h in sent_hashes:
                    save_state(seen_ids, recent_pairs, sent_hashes); continue

                res = build_message(body)
                if res[0] is None or res[0] == "FILTERED_PAIR":
                    save_state(seen_ids, recent_pairs, sent_hashes); continue
                
                text, score, pair, direction = res
                key = pair 
                now = time.time()
                
                if score >= MIN_SCORE and (key not in recent_pairs or now - recent_pairs[key] > PAIR_COOLDOWN_SEC):
                    send_telegram(text)
                    recent_pairs[key] = now
                    sent_hashes.add(h)
                
                save_state(seen_ids, recent_pairs, sent_hashes)

            mail.noop()
        except: mail = connect_mail()
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    keep_alive()
    check_mail()
    
