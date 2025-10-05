# main.py
import os
import time
import threading
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
import uvicorn

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
ALERTS_API_TOKEN = os.getenv("ALERTS_API_TOKEN")  # якщо треба

REGION_NAME = "Київська область"

# Картинки
ALARM_IMAGE_DEFAULT = "images/alarm.jpg"
CLEAR_IMAGE = "images/clear.jpg"
SAFETY_IMAGE = "images/saefty.jpg"

# Райони Київської області
DISTRICTS = [
    "Бориспільський",
    "Білоцерківський",
    "Броварський",
    "Бучанський",
    "Вишгородський",
    "Обухівський",
    "Фастівський"
]

# Відповідні картинки (якщо немає файлу, буде alarm.jpg)
DISTRICT_IMAGES = {
    "Бориспільський": "images/alarm_boryspil.jpg",
    "Білоцерківський": "images/alarm_bila_tserkva.jpg",
    "Броварський": "images/alarm_brovary.jpg",
    "Бучанський": "images/alarm_bucha.jpg",
    "Вишгородський": "images/alarm_vyshhorod.jpg",
    "Обухівський": "images/alarm_obukhiv.jpg",
    "Фастівський": "images/alarm_fastiv.jpg"
}

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

app = FastAPI()

# Стан останньої тривоги
last_alert_active = False
active_districts = set()  # активні райони

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

def send_telegram_message(text, image_path=None, chat_id=None):
    if chat_id is None:
        chat_id = TELEGRAM_CHAT_ID
    if image_path:
        # Перевірка наявності файлу
        if not os.path.exists(image_path):
            image_path = ALARM_IMAGE_DEFAULT
        files = {"photo": open(image_path, "rb")}
        data = {"chat_id": chat_id, "caption": text}
        try:
            resp = requests.post(f"{TELEGRAM_API_URL}/sendPhoto", data=data, files=files)
            resp.raise_for_status()
        except Exception as e:
            print("Помилка відправки фото в Telegram:", e)
    else:
        data = {"chat_id": chat_id, "text": text}
        try:
            resp = requests.post(f"{TELEGRAM_API_URL}/sendMessage", data=data)
            resp.raise_for_status()
        except Exception as e:
            print("Помилка відправки тексту в Telegram:", e)

def check_alerts_loop():
    """Фоновий цикл опитування для районів"""
    global last_alert_active, active_districts
    while True:
        alerts = fetch_alerts()
        new_active_districts = set()

        # Перевіряємо тривоги по районах
        for alert in alerts:
            if alert.get("location_oblast") == REGION_NAME:
                district = alert.get("location_district")
                if district in DISTRICTS:
                    new_active_districts.add(district)

        # Надсилання тривог по нових районах
        for district in new_active_districts - active_districts:
            text = f"Увага! У {district} районі оголошено повітряну тривогу! Будьте обережні і дійте відповідним чином!"
            image_path = DISTRICT_IMAGES.get(district, ALARM_IMAGE_DEFAULT)
            send_telegram_message(text, image_path)

        # Відбій — якщо немає жодної активної тривоги
        if not new_active_districts and last_alert_active:
            send_telegram_message(f"✅ Відбій повітряної тривоги у {REGION_NAME}", CLEAR_IMAGE)

        active_districts = new_active_districts
        last_alert_active = bool(active_districts)

        time.sleep(25)

# Обробка команди "Що по області?"
@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    message = data.get("message", {})
    text = message.get("text", "")
    chat_id = message.get("chat", {}).get("id")

    if text.strip() == "Що по області?":
        if not active_districts:
            # Відповідь "Все чисто!" з картинкою saefty.jpg
            image_path = SAFETY_IMAGE if os.path.exists(SAFETY_IMAGE) else CLEAR_IMAGE
            send_telegram_message("Все чисто!", image_path, chat_id=chat_id)
        else:
            districts_text = ", ".join(sorted(active_districts))
            send_telegram_message(f"Тривожаться такі райони: {districts_text}", chat_id=chat_id)

    return {"ok": True}

@app.get("/")
def root():
    return PlainTextResponse("Bot is running!")

if __name__ == "__main__":
    # Фоновий цикл опитування
    threading.Thread(target=check_alerts_loop, daemon=True).start()
    # Запуск FastAPI
    port = int(os.getenv("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
