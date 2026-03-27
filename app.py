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

# ================== ДОДАНО ДЛЯ RENDER ==================
from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

def keep_alive():
    t = Thread(target=run)
    t.start()
# =======================================================

# ================== CONFIG ==================
TELEGRAM_BOT_TOKEN = "8566965927:AAHvK0eNxVp3Imb3jIfdmbiv4EEj-5gt8nA"
TELEGRAM_CHAT_ID = "-1003798450910"

EMAIL_USER = "signalcom46@gmail.com"
EMAIL_PASS = "nbgaewtkyoffhruy"
IMAP_HOST = "imap.gmail.com"

CHECK_INTERVAL = 25
MIN_SCORE = 55
STATE_FILE = "bot_state.json"

ALLOWED_SENDERS = {
    "noreply@tradingview.com",
    "alerts@tradingview.com",
    "no-reply@tradingview.com",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("SignalBot")

seen_ids = set()
sent_hashes = set()
stats = {
    "sent": 0,
    "skipped_low_score": 0,
    "skipped_sender": 0,
    "duplicates": 0,
}

# ================== STATE ==================
def load_state():
    global seen_ids, sent_hashes, stats
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            seen_ids = set(data.get("seen_ids", []))
            sent_hashes = set(data.get("sent_hashes", []))
            stats.update(data.get("stats", {}))
        logger.info(f"✅ State loaded: {len(seen_ids)} seen, {len(sent_hashes)} hashes")
    except Exception as e:
        logger.error(f"⚠️ State load error: {e}")

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "seen_ids": list(seen_ids)[-5000:],
                    "sent_hashes": list(sent_hashes)[-5000:],
                    "stats": stats,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception as e:
        logger.error(f"⚠️ State save error: {e}")

# ================== HELPERS ==================
def decode_mime(value):
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for part, enc in parts:
        if isinstance(part, bytes):
            try:
                out.append(part.decode(enc or "utf-8", errors="ignore"))
            except Exception:
                out.append(part.decode("utf-8", errors="ignore"))
        else:
            out.append(str(part))
    return "".join(out)

def normalize_text(text):
    text = text or ""
    text = text.replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def strip_html(html_text):
    html_text = re.sub(r"(?is)<(script|style).*?>.*?(</\1>)", "", html_text)
    html_text = re.sub(r"(?is)<br\s*/?>", "\n", html_text)
    html_text = re.sub(r"(?is)</p>", "\n", html_text)
    html_text = re.sub(r"(?is)<.*?>", " ", html_text)
    html_text = re.sub(r"&nbsp;", " ", html_text)
    html_text = re.sub(r"&amp;", "&", html_text)
    html_text = re.sub(r"\s{2,}", " ", html_text)
    return html_text.strip()

def get_body(msg):
    body_plain = ""
    body_html = ""

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))

            if "attachment" in disp.lower():
                continue

            payload = part.get_payload(decode=True)
            if not payload:
                continue

            if ctype == "text/plain" and not body_plain:
                body_plain = payload.decode(errors="ignore")
            elif ctype == "text/html" and not body_html:
                body_html = payload.decode(errors="ignore")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body_plain = payload.decode(errors="ignore")

    if body_plain.strip():
        return normalize_text(body_plain)
    if body_html.strip():
        return normalize_text(strip_html(body_html))
    return ""

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            logger.info("✅ Sent to Telegram")
        else:
            logger.warning(f"⚠️ Telegram response: {resp.status_code} {resp.text[:150]}")
        time.sleep(1)
    except Exception as e:
        logger.error(f"❌ Telegram error: {e}")

def is_allowed_sender(from_header):
    from_email = parseaddr(from_header or "")[1].lower()
    if not from_email:
        return False
    if from_email in ALLOWED_SENDERS:
        return True
    if from_email.endswith("@tradingview.com"):
        return True
    return False

def message_hash(sender, subject, body):
    raw = f"{sender}|{subject}|{body[:700]}"
    return str(abs(hash(raw)))

# ================== EXTRACTORS ==================
PAIR_PATTERNS = [
    r"ПАРА:\s*([A-Z0-9/._-]+)",
    r"PAIR:\s*([A-Z0-9/._-]+)",
    r"SYMBOL:\s*([A-Z0-9/._-]+)",
    r"\b([A-Z]{3}[\/\.\-_]?[A-Z]{3})\b",   # EURUSD / EUR/USD
    r"\b([A-Z]{3,5}[\/\.\-_]?[A-Z]{3,5})\b",  # BTCUSDT, XAUUSD
]

PRICE_PATTERNS = [
    r"ЦІНА:\s*([\d.,]+)",
    r"PRICE:\s*([\d.,]+)",
    r"ENTRY:\s*([\d.,]+)",
    r"ВХІД:\s*([\d.,]+)",
]

TF_PATTERNS = [
    r"TF:\s*([1-9]?[mhd])",
    r"TIMEFRAME:\s*([1-9]?[mhd])",
    r"TIME FRAME:\s*([1-9]?[mhd])",
    r"\b(1M|3M|5M|15M|30M|1H|4H|1D)\b",
]

TP_PATTERNS = [
    r"TP1:\s*([\d.,]+)",
    r"TP2:\s*([\d.,]+)",
    r"TP:\s*([\d.,]+)",
    r"TAKE PROFIT:\s*([\d.,]+)",
    r"ПРИБУТОК:\s*([\d.,]+)",
]

SL_PATTERNS = [
    r"SL:\s*([\d.,]+)",
    r"STOP LOSS:\s*([\d.,]+)",
    r"СТОП:\s*([\d.,]+)",
]

RISK_PATTERNS = [
    r"RISK:\s*([\d.,]+)\s*%?",
    r"RISK\s*-\s*([\d.,]+)\s*%?",
    r"РИЗИК:\s*([\d.,]+)\s*%?",
]

def extract_first(patterns, body, flags=re.IGNORECASE):
    for pat in patterns:
        m = re.search(pat, body, flags)
        if m:
            return m.group(1)
    return None

def normalize_price(value):
    if not value:
        return None
    return value.replace(",", ".").strip()

def score_signal(body):
    t = body.upper()
    score = 0

    positive_weights = {
        "BUY": 12,
        "SELL": 12,
        "CALL": 12,
        "PUT": 12,
        "BREAKOUT": 10,
        "RETEST": 8,
        "CONFLUENCE": 10,
        "SUPPORT": 8,
        "RESISTANCE": 8,
        "BOS": 8,
        "CHOCH": 8,
        "ENTRY": 8,
        "SETUP": 7,
        "CONFIRMED": 10,
        "STRONG": 12,
        "HIGH PROBABILITY": 15,
        "TREND": 6,
        "RISK/REWARD": 8,
        "RR": 6,
        "TP": 6,
        "SL": 6,
    }

    negative_weights = {
        "MAYBE": -8,
        "PROBABLY": -6,
        "NOT SURE": -15,
        "UNCLEAR": -10,
        "WAIT": -4,
        "WATCH": -2,
        "LOW CONFIDENCE": -15,
        "WEAK": -20,
        "NO TRADE": -30,
        "POSSIBLE": -6,
        "SOMETIME": -8,
    }

    for k, w in positive_weights.items():
        if k in t:
            score += w

    for k, w in negative_weights.items():
        if k in t:
            score += w

    if re.search(r"(PAIR|ПАРА)", t):
        score += 8
    if re.search(r"(ENTRY|ВХІД)", t):
        score += 8
    if re.search(r"(TIMEFRAME|TF|1M|5M|15M|1H|4H|1D)", t):
        score += 6
    if re.search(r"(TP|TAKE PROFIT|SL|STOP LOSS)", t):
        score += 8
    if extract_first(PAIR_PATTERNS, body):
        score += 10
    if extract_first(PRICE_PATTERNS, body):
        score += 6
    if re.search(r"\b(BUY|SELL|CALL|PUT|ВГОРУ|ВНИЗ|UP|DOWN)\b", t):
        score += 8

    return max(0, min(100, score))

def build_message(body, subject="", sender=""):
    pair = extract_first(PAIR_PATTERNS, body) or "N/A"
    price = normalize_price(extract_first(PRICE_PATTERNS, body)) or "MARKET"
    tf = extract_first(TF_PATTERNS, body) or "N/A"
    tp = normalize_price(extract_first(TP_PATTERNS, body)) or "N/A"
    sl = normalize_price(extract_first(SL_PATTERNS, body)) or "N/A"
    risk = normalize_price(extract_first(RISK_PATTERNS, body)) or "1.0"

    t = body.upper()
    if any(x in t for x in ["ВНИЗ", "PUT", "SELL", "DOWN"]):
        action = "SELL 🔴"
    elif any(x in t for x in ["ВГОРУ", "CALL", "BUY", "UP"]):
        action = "BUY 🟢"
    else:
        action = "CHECK CHART 🟡"

    score = score_signal(body)
    if score >= 80:
        quality = "ULTRA"
    elif score >= 65:
        quality = "STRONG"
    elif score >= 55:
        quality = "OK"
    else:
        quality = "WEAK"

    if tp == "N/A" and price != "MARKET":
        tp = "MANUAL"
    if sl == "N/A" and price != "MARKET":
        sl = "MANUAL"

    text = f"""
<b>🚀 SIGNAL</b>

<b>PAIR:</b> <code>{html.escape(pair)}</code>
<b>ACTION:</b> <code>{html.escape(action)}</code>
<b>ENTRY:</b> <code>{html.escape(price)}</code>
<b>TP:</b> <code>{html.escape(tp)}</code>
<b>SL:</b> <code>{html.escape(sl)}</code>
<b>RISK:</b> <code>{html.escape(str(risk))}%</code>
<b>TF:</b> <code>{html.escape(tf)}</code>
<b>QUALITY:</b> <code>{quality} ({score}/100)</code>
<b>TIME:</b> <code>{time.strftime("%H:%M:%S")}</code>
""".strip()

    if subject:
        text += f"\n<b>SUBJECT:</b> <code>{html.escape(subject[:120])}</code>"
    if sender:
        text += f"\n<b>FROM:</b> <code>{html.escape(sender[:120])}</code>"

    return text, score

# ================== IMAP ==================
def connect_mail():
    while True:
        try:
            mail = imaplib.IMAP4_SSL(IMAP_HOST)
            mail.login(EMAIL_USER, EMAIL_PASS)
            logger.info("📩 IMAP connected")
            return mail
        except Exception as e:
            logger.error(f"❌ IMAP connect error: {e}")
            time.sleep(10)

def check_mail():
    load_state()
    mail = connect_mail()

    while True:
        try:
            mail.select("inbox")

            _, data = mail.search(None, "UNSEEN")
            msg_ids = data[0].split()

            if msg_ids:
                logger.info(f"📬 Unseen messages: {len(msg_ids)}")

            for num in msg_ids:
                mid = num.decode(errors="ignore") if isinstance(num, bytes) else str(num)
                if mid in seen_ids:
                    continue

                _, msg_data = mail.fetch(num, "(RFC822)")
                if not msg_data or not msg_data[0]:
                    continue

                raw_msg = msg_data[0][1]
                msg = email.message_from_bytes(raw_msg)

                from_header = decode_mime(msg.get("From", ""))
                subject = decode_mime(msg.get("Subject", ""))

                if not is_allowed_sender(from_header):
                    stats["skipped_sender"] = stats.get("skipped_sender", 0) + 1
                    seen_ids.add(mid)
                    mail.store(num, "+FLAGS", "\\Seen")
                    continue

                body = get_body(msg)
                if not body:
                    seen_ids.add(mid)
                    mail.store(num, "+FLAGS", "\\Seen")
                    continue

                h = message_hash(from_header, subject, body)
                if h in sent_hashes:
                    stats["duplicates"] = stats.get("duplicates", 0) + 1
                    seen_ids.add(mid)
                    mail.store(num, "+FLAGS", "\\Seen")
                    continue

                pretty_msg, score = build_message(body=body, subject=subject, sender=from_header)

                if score < MIN_SCORE:
                    stats["skipped_low_score"] = stats.get("skipped_low_score", 0) + 1
                    logger.info(f"🟡 Low score ({score}) skipped")
                    seen_ids.add(mid)
                    mail.store(num, "+FLAGS", "\\Seen")
                    continue

                send_telegram(pretty_msg)
                stats["sent"] = stats.get("sent", 0) + 1
                sent_hashes.add(h)
                seen_ids.add(mid)
                mail.store(num, "+FLAGS", "\\Seen")

                save_state()

            mail.noop()

        except Exception as e:
            logger.error(f"🔄 Loop error: {e}")
            try:
                mail.logout()
            except Exception:
                pass
            time.sleep(5)
            mail = connect_mail()

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    # ЗАПУСКАЄМО ФЕЙКОВИЙ СЕРВЕР ПЕРЕД БОТОМ
    keep_alive()
    # ЗАПУСКАЄМО ОСНОВНОГО БОТА
    check_mail()
