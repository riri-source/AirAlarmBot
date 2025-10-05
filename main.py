import requests
import time
import os
from dotenv import load_dotenv

# Завантаження змінних середовища
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

ALERTS_API = "https://alerts.in.ua/api/alerts?limit=100"

CHECK_INTERVAL = 25  # секунд

# Для відстеження останнього стану тривоги
last_alert_active = False

def get_alerts():
    try:
        response = requests.get(ALERTS_API)
        response.raise_for_status()
        data = response.json()
        return data.get("alerts", [])
    except Exception as e:
        print(f"Помилка при отриманні даних: {e}")
        return []

def send_telegram_message(text, image_path=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto" if image_path else f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    
    if image_path:
        with open(image_path, "rb") as img:
            files = {"photo": img}
            data = {"chat_id": TELEGRAM_CHAT_ID, "caption": text}
            requests.post(url, files=files, data=data)
    else:
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
        requests.post(url, data=data)

def check_alerts():
    global last_alert_active
    alerts = get_alerts()

    # Фільтруємо тільки Київську область
    kyiv_alerts = [a for a in alerts if a.get("location_oblast") == "Київська область"]

    # Перевіряємо, чи є активна тривога
    active_alerts = [a for a in kyiv_alerts if a.get("finished_at") is None]

    if active_alerts and not last_alert_active:
        # Є нова активна тривога
        send_telegram_message("‼️ Увага! Повітряна тривога у Київській області!", "images/alarm.jpg")
        last_alert_active = True
    elif not active_alerts and last_alert_active:
        # Тривога завершилась
        send_telegram_message("✅ Тривога завершилась у Київській області.", "images/clear.jpg")
        last_alert_active = False

if __name__ == "__main__":
    while True:
        check_alerts()
        time.sleep(CHECK_INTERVAL)
