"""" TradingView -> Telegram webhook relay

Runs well in a Python environment on Android (for example Pydroid 3) for testing, but TradingView must be able to reach this app over a public HTTPS URL. TradingView sends webhook alerts as an HTTP POST request to the URL you provide, uses application/json when the alert body is valid JSON, accepts only ports 80/443, and cancels requests that take longer than 3 seconds to process.

Docs recap:

TradingView webhook alerts: https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/

Telegram Bot API: sendMessage is part of the Bot API


Install: pip install flask requests

Environment variables: TELEGRAM_BOT_TOKEN   -> your bot token from @BotFather TELEGRAM_CHAT_ID     -> chat id or comma-separated list of chat ids WEBHOOK_SECRET       -> a secret string you also include in TradingView alert JSON HOST                 -> default 0.0.0.0 PORT                 -> default 8080

TradingView alert JSON example: { "secret": "YOUR_SECRET", "symbol": "{{ticker}}", "direction": "UP", "timeframe": "{{interval}}", "price": "{{close}}", "confidence": 82, "candle": "bullish_engulfing", "expiry": 3, "source": "TradingView" }

Use this relay as the no-delay bridge: TradingView alert -> webhook -> Telegram message """

import os import time import json import hashlib import logging import threading from typing import Any, Dict, List, Optional

import requests from flask import Flask, request, jsonify

---------------------- Config ----------------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip() TELEGRAM_CHAT_ID_RAW = os.getenv("TELEGRAM_CHAT_ID", "").strip() WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip() HOST = os.getenv("HOST", "0.0.0.0").strip() PORT = int(os.getenv("PORT", "8080"))

CHAT_IDS: List[str] = [c.strip() for c in TELEGRAM_CHAT_ID_RAW.split(",") if c.strip()]

app = Flask(name) logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s") logger = logging.getLogger("tv-webhook-relay")

dedupe repeated webhook retries for a short time

_recent: Dict[str, float] = {} _recent_ttl_seconds = 90

---------------------- Helpers ----------------------

def _cleanup_recent() -> None: now = time.time() expired = [k for k, ts in _recent.items() if now - ts > _recent_ttl_seconds] for k in expired: _recent.pop(k, None)

def _fingerprint(payload: Dict[str, Any]) -> str: raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")) return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def _safe_text(value: Any) -> str: if value is None: return "" return str(value)

def format_signal(payload: Dict[str, Any]) -> str: symbol = _safe_text(payload.get("symbol") or payload.get("ticker") or "?") direction = _safe_text(payload.get("direction") or payload.get("signal") or "NEUTRAL") timeframe = _safe_text(payload.get("timeframe") or payload.get("tf") or "") price = _safe_text(payload.get("price") or payload.get("close") or "") confidence = _safe_text(payload.get("confidence") or payload.get("score") or "") candle = _safe_text(payload.get("candle") or payload.get("pattern") or "") expiry = _safe_text(payload.get("expiry") or 3) source = _safe_text(payload.get("source") or "TradingView") ts = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())

arrow = "🟢 UP" if direction.upper() in {"UP", "BUY", "LONG"} else "🔴 DOWN" if direction.upper() in {"DOWN", "SELL", "SHORT"} else direction

lines = [
    f"⚡️ {source} SIGNAL",
    f"PAIR: {symbol}",
]
if timeframe:
    lines.append(f"TF: {timeframe}")
lines.append(f"EXPIRY: {expiry}m")
lines.append(f"DIRECTION: {arrow}")
if confidence not in {"", "None"}:
    lines.append(f"CONFIDENCE: {confidence}%")
if price not in {"", "None"}:
    lines.append(f"PRICE: {price}")
if candle:
    lines.append(f"CANDLE: {candle}")
lines.append(f"TIME: {ts}")
return "\n".join(lines)

def send_telegram(text: str) -> None: if not TELEGRAM_BOT_TOKEN: logger.error("TELEGRAM_BOT_TOKEN is missing") return if not CHAT_IDS: logger.error("TELEGRAM_CHAT_ID is missing") return

url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
for chat_id in CHAT_IDS:
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning("Telegram send failed for %s: %s %s", chat_id, resp.status_code, resp.text[:300])
        else:
            logger.info("Sent Telegram message to %s", chat_id)
    except Exception as e:
        logger.exception("Telegram error for %s: %s", chat_id, e)

def _launch_telegram_send(text: str) -> None: t = threading.Thread(target=send_telegram, args=(text,), daemon=True) t.start()

def parse_payload() -> Optional[Dict[str, Any]]: """Accept JSON or plain text. For plain text we keep it simple.""" if request.is_json: data = request.get_json(silent=True) if isinstance(data, dict): return data return None

raw = request.get_data(as_text=True).strip()
if not raw:
    return None

# plain-text fallback: symbol=EURUSD;direction=UP;confidence=80;expiry=3
if raw.startswith("{") and raw.endswith("}"):
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

data: Dict[str, Any] = {}
for chunk in raw.replace("\n", ";").split(";"):
    if "=" in chunk:
        k, v = chunk.split("=", 1)
        data[k.strip()] = v.strip()
if data:
    return data

# final fallback: raw text as message body
return {"symbol": "?", "direction": "NEUTRAL", "source": "TradingView", "raw": raw}

---------------------- Routes ----------------------

@app.get("/") def home(): return jsonify( ok=True, service="TradingView -> Telegram relay", status="running", hint="POST /webhook with TradingView alert JSON", )

@app.get("/health") def health(): return jsonify(ok=True)

@app.post("/webhook") def webhook(): payload = parse_payload() if not payload: return jsonify(ok=False, error="empty payload"), 400

# secret check if configured
if WEBHOOK_SECRET:
    sent_secret = str(payload.get("secret") or payload.get("token") or "")
    if sent_secret != WEBHOOK_SECRET:
        return jsonify(ok=False, error="unauthorized"), 401

_cleanup_recent()
fp = _fingerprint(payload)
if fp in _recent:
    return jsonify(ok=True, duplicate=True), 200
_recent[fp] = time.time()

message = format_signal(payload)
_launch_telegram_send(message)

# Respond immediately so TradingView does not wait on downstream messaging.
return jsonify(ok=True, delivered=True), 200

---------------------- Main ----------------------

def main(): if not TELEGRAM_BOT_TOKEN: print("Set TELEGRAM_BOT_TOKEN first") return if not CHAT_IDS: print("Set TELEGRAM_CHAT_ID first (comma-separated is allowed)") return

print("Relay is starting...")
print(f"Listening on http://{HOST}:{PORT}")
print("TradingView must reach this app via a public HTTPS URL.")
print("Use your tunnel / VPS / reverse proxy to expose port 80 or 443.")
app.run(host=HOST, port=PORT, debug=False, use_reloader=False)

if name == "main": main()"
