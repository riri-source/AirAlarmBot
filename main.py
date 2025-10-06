# main.py
import os
import time
import threading
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, JSONResponse
import uvicorn
import unicodedata

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
ALERTS_API_TOKEN = os.getenv("ALERTS_API_TOKEN")

# region name as returned by API (exact match)
REGION_NAME = "Київська область"

# images and defaults
IMAGES_FOLDER = "images"
ALARM_IMAGE_DEFAULT = os.path.join(IMAGES_FOLDER, "Alarm.jpg")
CLEAR_IMAGE = os.path.join(IMAGES_FOLDER, "Clear.jpg")
SAFETY_IMAGE = os.path.join(IMAGES_FOLDER, "Saefty.jpg")  # залишаємо опечатку, як просили

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

app = FastAPI()

# internal state
lock = threading.Lock()
active_districts = set()  # поточні активні райони (normalized form)
last_alert_active = False

# canonical list of districts you care about (we'll normalize inputs before comparing)
CANONICAL_DISTRICTS = [
    "Бориспільський",
    "Білоцерківський",
    "Броварський",
    "Бучанський",
    "Вишгородський",
    "Обухівський",
    "Фастівський"
]

# prepare normalized map: normalized -> canonical
def normalize_name(s: str) -> str:
    if not s:
        return ""
    s = s.strip()
    # remove typical suffixes and punctuation and lowercase
    s = s.replace("р-н", "").replace("р.", "").replace("район", "")
    s = s.replace("району", "").replace("область", "").replace("м.", "")
    s = s.replace("'", "").replace("’", "")
    s = " ".join(s.split())
    s = s.lower()
    # remove diacritics if any (defensive)
    s = unicodedata.normalize("NFKD", s)
    s = "".join([c for c in s if not unicodedata.combining(c)])
    return s

NORMALIZED_TO_CANONICAL = {normalize_name(c): c for c in CANONICAL_DISTRICTS}

def find_image_path_prefer(preferred_path: str) -> str:
    """Return existing file path for image; try exact, then case-insensitive search in images folder, else None."""
    if not preferred_path:
        return None
    if os.path.exists(preferred_path):
        return preferred_path
    # fallback: try case-insensitive match inside images folder
    try:
        folder = os.path.dirname(preferred_path) or IMAGES_FOLDER
        name = os.path.basename(preferred_path).lower()
        if not os.path.isdir(folder):
            folder = IMAGES_FOLDER
        for f in os.listdir(folder):
            if f.lower() == name:
                return os.path.join(folder, f)
    except Exception:
        pass
    # try searching whole images folder for substring match (lenient)
    try:
        for f in os.listdir(IMAGES_FOLDER):
            if os.path.splitext(f.lower())[0] == os.path.splitext(name)[0]:
                return os.path.join(IMAGES_FOLDER, f)
    except Exception:
        pass
    return None

def send_telegram_message(text: str, image_path: str = None, chat_id: str = None):
    if chat_id is None:
        chat_id = TELEGRAM_CHAT_ID
    if image_path:
        # resolve path leniently
        resolved = find_image_path_prefer(image_path) or find_image_path_prefer(ALARM_IMAGE_DEFAULT)
        if not resolved:
            # fallback to text-only if no images present
            try:
                data = {"chat_id": chat_id, "text": text}
                requests.post(f"{TELEGRAM_API_URL}/sendMessage", data=data, timeout=10)
                return
            except Exception as e:
                print("Помилка відправки текстового повідомлення (fallback):", e)
                return
        try:
            with open(resolved, "rb") as f:
                files = {"photo": f}
                data = {"chat_id": chat_id, "caption": text}
                resp = requests.post(f"{TELEGRAM_API_URL}/sendPhoto", data=data, files=files, timeout=15)
                resp.raise_for_status()
        except Exception as e:
            print("Помилка відправки фото в Telegram:", e)
            # try text fallback
            try:
                data = {"chat_id": chat_id, "text": text}
                requests.post(f"{TELEGRAM_API_URL}/sendMessage", data=data, timeout=10)
            except Exception as e2:
                print("Помилка відправки текстового повідомлення після помилки з фото:", e2)
    else:
        try:
            data = {"chat_id": chat_id, "text": text}
            resp = requests.post(f"{TELEGRAM_API_URL}/sendMessage", data=data, timeout=10)
            resp.raise_for_status()
        except Exception as e:
            print("Помилка відправки тексту в Telegram:", e)

def fetch_active_alerts_raw():
    """
    Get raw alerts from API. Returns list of alert dicts (as returned under "alerts").
    Query uses token either in header or query string (we use query string to be maximally compatible).
    """
    if not ALERTS_API_TOKEN:
        print("ALERTS_API_TOKEN not set in environment")
        return []
    url = f"https://api.alerts.in.ua/v1/alerts/active.json?token={ALERTS_API_TOKEN}"
    headers = {"Authorization": f"Bearer {ALERTS_API_TOKEN}"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "alerts" in data:
            return data.get("alerts", [])
        # sometimes API might return list directly
        if isinstance(data, list):
            return data
        print("Невідомий формат відповіді API:", type(data))
        return []
    except Exception as e:
        print("Помилка запиту до alerts.in.ua:", e)
        return []

def get_active_districts_from_api():
    """
    Return set of normalized canonical district names (from CANONICAL_DISTRICTS)
    that are currently in active alerts for REGION_NAME.
    """
    alerts = fetch_active_alerts_raw()
    found = set()
    for a in alerts:
        # try possible keys for oblast and raion
        oblast = a.get("location_oblast") or a.get("location_title") or a.get("oblast") or a.get("location_oblast_title")
        raion = a.get("location_raion") or a.get("location_raion") or a.get("location_district") or a.get("location_raion") or a.get("location_raion_title") or a.get("location_raion_name") or a.get("location_raion") or a.get("location_raion")
        # some payloads have "location_raion" spelled similarly; defensive
        # if raion is absent, sometimes alert is for the whole oblast (we handle that below)
        if oblast:
            oblast = oblast.strip()
        if oblast != REGION_NAME:
            continue
        # if raion is None but alert pertains to oblast-level, mark 'Уся область'
        if not raion:
            found.add("Уся область")
            continue
        # normalize raion string and map to canonical district if possible
        norm = normalize_name(raion)
        # exact normalized match
        canonical = NORMALIZED_TO_CANONICAL.get(norm)
        if canonical:
            found.add(canonical)
        else:
            # try to match by startswith or contains (some API values include "Білоцерківський район")
            for key_norm, canon in NORMALIZED_TO_CANONICAL.items():
                if key_norm in norm or norm in key_norm:
                    found.add(canon)
                    break
            else:
                # if unknown raion for our canonical set, ignore OR optionally include raw name (choose include raw)
                # include raw (but normalized) to show user what's active even if not in canonical set
                # revert to cleaned original (strip 'район', etc.)
                cleaned = raion.strip()
                found.add(cleaned)
    return found

def check_alerts_loop():
    global active_districts, last_alert_active
    while True:
        try:
            new_active = get_active_districts_from_api()
            with lock:
                prev = set(active_districts)
                # send notifications for newly appeared districts
                for d in sorted(new_active - prev):
                    text = f"❗️ Увага! Повітряна тривога: {d}"
                    # pick district image if exists: look up canonical mapping
                    # try canonical image filename patterns
                    possible_image = None
                    # try mapping by canonical name
                    for canon in CANONICAL_DISTRICTS:
                        if d == canon:
                            # construct filename pattern (lowercase, replace spaces)
                            filename = f"alarm_{canon.lower().replace(' ', '_')}.jpg"
                            possible_image = os.path.join(IMAGES_FOLDER, filename)
                            break
                    # fallback default
                    if not possible_image:
                        possible_image = ALARM_IMAGE_DEFAULT
                    send_telegram_message(text, possible_image)
                # send "відбій" if previously had alerts and now none
                if not new_active and last_alert_active:
                    send_telegram_message(f"✅ Відбій повітряної тривоги у {REGION_NAME}", find_image_path_prefer(CLEAR_IMAGE) or None)
                active_districts = new_active
                last_alert_active = bool(active_districts)
        except Exception as e:
            print("Помилка в циклі перевірки тривог:", e)
        time.sleep(25)

@app.post("/webhook")
async def webhook(request: Request):
    """
    Telegram webhook endpoint for incoming messages.
    Expects Telegram to POST updates here.
    When a user sends "Що по області?" bot replies with live data from API.
    """
    try:
        payload = await request.json()
    except Exception:
        return {"ok": False}
    message = payload.get("message") or payload.get("edited_message") or {}
    text = (message.get("text") or "").strip()
    chat = message.get("chat", {})
    chat_id = chat.get("id") or TELEGRAM_CHAT_ID

    if text == "Що по області?":
        # live fetch
        current = get_active_districts_from_api()
        if not current:
            # send "Все чисто!" with safety image if exists
            img = find_image_path_prefer(SAFETY_IMAGE) or find_image_path_prefer(CLEAR_IMAGE)
            send_telegram_message("Все чисто!", img, chat_id=chat_id)
        else:
            # format list nicely
            msg = "Тривожаться такі райони: " + ", ".join(sorted(current))
            send_telegram_message(msg, chat_id=chat_id)
    # Return ok for Telegram
    return {"ok": True}

@app.get("/debug_active_alerts")
def debug_active_alerts():
    """
    Returns raw alerts from API (for debugging on Render).
    """
    raw = fetch_active_alerts_raw()
    # compress to readable minimal form
    try:
        minimal = []
        for a in raw:
            minimal.append({
                "location_oblast": a.get("location_oblast") or a.get("location_title"),
                "location_raion": a.get("location_raion") or a.get("location_district") or a.get("location_raion"),
                "alert_type": a.get("alert_type"),
                "started_at": a.get("started_at"),
                "finished_at": a.get("finished_at")
            })
        return JSONResponse({"alerts": minimal})
    except Exception as e:
        return JSONResponse({"error": str(e), "raw_len": len(raw)})

@app.get("/")
def root():
    return PlainTextResponse("Bot is running!")

def self_ping_loop():
    port = int(os.getenv("PORT", 10000))
    url = f"http://localhost:{port}/"
    while True:
        try:
            requests.get(url, timeout=5)
        except Exception:
            pass
        time.sleep(300)

if __name__ == "__main__":
    # start background workers
    t = threading.Thread(target=check_alerts_loop, daemon=True)
    t.start()
    p = threading.Thread(target=self_ping_loop, daemon=True)
    p.start()
    port = int(os.getenv("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
