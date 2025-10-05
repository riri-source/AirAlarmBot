# main.py
import os
import time
import threading
import requests
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
import uvicorn

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
ALERTS_API_TOKEN = os.getenv("ALERTS_API_TOKEN")  # якщо треба для авторизації API

# Київська область
REGION_NAME = "Київська область"

# Зображення для тривоги / без тривоги
ALARM_IMAGE = "images/alarm.jpg"
CLEAR_IMAGE = "images/clear.jpg"

# Telegram API
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# FastAPI для Render
app = FastAPI()

# Стан останньої тривоги
last_alert_active = False

def fetch_alerts():
    """Отримати тривоги з alerts.in.ua"""
    url = "https://alerts.in.ua/api/alerts"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data.get("alerts", [])
    except Exception as e:
        print("Помилка при запиті alerts.in.ua:", e)
        return []

def send_telegram_message(text, image_path=None):
    """Відправка повідомлення в Telegram"""
    if image_path:
        files = {"photo": open(image_path, "rb")}
        data = {"chat_id": TELEGRAM_CHAT_ID, "caption": text}
        try:
            resp = requests.post(f"{TELEGRAM_API_URL}/sendPhoto", data=data, files=files)
            resp.raise_for_status()
        except Exception as e:
            print("Помилка відправки фото в Telegram:", e)
    else:
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
        try:
            resp = requests.post(f"{TELEGRAM_API_URL}/sendMessage", data=data)
            resp.raise_for_status()
        except Exception as e:
            print("Помилка відправки тексту в Telegram:", e)

def check_alerts_loop():
    """Фоновий цикл опитування"""
    global last_alert_active
    while True:
        alerts = fetch_alerts()
        active_alert = False
        for alert in alerts:
            if alert.get("location_oblast") == REGION_NAME:
                active_alert = True
                break

        if active_alert != last_alert_active:
            if active_alert:
                send_telegram_message(f"❗️ ТРИВОГА у {REGION_NAME}!", ALARM_IMAGE)
            else:
                send_telegram_message(f"✅ Тривога відсутня у {REGION_NAME}", CLEAR_IMAGE)
            last_alert_active = active_alert

        time.sleep(25)

# Endpoint для Render
@app.get("/")
def root():
    return PlainTextResponse("Bot is running!")

if __name__ == "__main__":
    # Запуск фонової задачі
    threading.Thread(target=check_alerts_loop, daemon=True).start()
    # Запуск FastAPI
    port = int(os.getenv("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
