# main.py
import os
import time
import threading
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, JSONResponse
import uvicorn

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
ALERTS_API_TOKEN = os.getenv("ALERTS_API_TOKEN")

REGION_NAME = "Київська область"

# Картинки
ALARM_IMAGE_DEFAULT = "images/Alarm.jpg"
CLEAR_IMAGE = "images/Clear.jpg"
SAFETY_IMAGE = "images/Saefty.jpg"

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

app = FastAPI()

last_alert_active = False
active_districts = set()
DISTRICTS = []  # автоматично заповниться з API

def fetch_all_districts():
    """Отримати всі райони Київської області з API"""
    url = "https://alerts.in.ua/api/v1/alerts/active.json"
    headers = {"Authorization": f"Bearer {ALERTS_API_TOKEN}"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        districts_set = set()
        for alert in data.get("alerts", []):
            if alert.get("oblast") == REGION_NAME:
                district = alert.get("district")
                if district:
                    districts_set.add(district)
        return sorted(list(districts_set))
    except Exception as e:
        print("Помилка отримання районів:", e)
        return []

def fetch_alerts():
    """Отримати активні тривоги з alerts.in.ua"""
    url = "https://alerts.in.ua/api/v1/alerts/active.json"
    headers = {"Authorization": f"Bearer {ALERTS_API_TOKEN}"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        alerts_list = []
        for alert in data.get("alerts", []):
            oblast = alert.get("oblast")
            district = alert.get("district")
            alerts_list.append({"oblast": oblast, "district": district})
        return alerts_list
    except Exception as e:
        print("Помилка при запиті alerts.in.ua:", e)
        return []

def send_telegram_message(text, image_path=None, chat_id=None):
    if chat_id is None:
        chat_id = TELEGRAM_CHAT_ID
    if image_path:
        if not os.path.exists(image_path):
            image_path = ALARM_IMAGE_DEFAULT
        try:
            with open(image_path, "rb") as f:
                files = {"photo": f}
                data = {"chat_id": chat_id, "caption": text}
                requests.post(f"{TELEGRAM_API_URL}/sendPhoto", data=data, files=files)
        except Exception as e:
            print("Помилка відправки фото в Telegram:", e)
    else:
        try:
            data = {"chat_id": chat_id, "text": text}
            requests.post(f"{TELEGRAM_API_URL}/sendMessage", data=data)
        except Exception as e:
            print("Помилка відправки тексту в Telegram:", e)

def check_alerts_loop():
    global last_alert_active, active_districts, DISTRICTS
    while True:
        if not DISTRICTS:
            DISTRICTS = fetch_all_districts()

        alerts = fetch_alerts()
        new_active_districts = set()

        for alert in alerts:
            if alert.get("oblast") == REGION_NAME:
                district = alert.get("district")
                if district in DISTRICTS:
                    new_active_districts.add(district)

        # Нові тривоги
        for district in new_active_districts - active_districts:
            text = f"Увага! У {district} районі оголошено повітряну тривогу!"
            send_telegram_message(text)

        # Відбій
        if not new_active_districts and last_alert_active:
            send_telegram_message(f"✅ Відбій повітряної тривоги у {REGION_NAME}", CLEAR_IMAGE)

        active_districts = new_active_districts
        last_alert_active = bool(active_districts)

        time.sleep(25)

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    message = data.get("message", {})
    text = message.get("text", "")
    chat_id = message.get("chat", {}).get("id")

    if text.strip() == "Що по області?":
        if not active_districts:
            image_path = SAFETY_IMAGE if os.path.exists(SAFETY_IMAGE) else CLEAR_IMAGE
            send_telegram_message("Все чисто!", image_path, chat_id=chat_id)
        else:
            districts_text = ", ".join(sorted(active_districts))
            send_telegram_message(f"Тривожаться такі райони: {districts_text}", chat_id=chat_id)

    return {"ok": True}

@app.get("/")
def root():
    return PlainTextResponse("Bot is running!")

def self_ping_loop():
    port = int(os.getenv("PORT", 10000))
    url = f"http://localhost:{port}/"
    while True:
        try:
            requests.get(url, timeout=5)
        except:
            pass
        time.sleep(300)

if __name__ == "__main__":
    threading.Thread(target=check_alerts_loop, daemon=True).start()
    threading.Thread(target=self_ping_loop, daemon=True).start()
    port = int(os.getenv("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
