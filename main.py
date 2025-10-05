import os
import time
import threading
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
ALERTS_API_TOKEN = os.getenv("ALERTS_API_TOKEN")

REGION_NAME = "Київська область"

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Повний список районів Київської області
DISTRICTS = [
    "Бориспільський", "Білоцерківський", "Броварський", "Бучанський",
    "Вишгородський", "Обухівський", "Фастівський", "Ставищенський",
    "Сквирський", "Софіївський", "Тетіївський", "Таращанський",
    "Яготинський"
]

# Стан останньої тривоги
last_alert_active = False
active_districts = set()

app = FastAPI()

def fetch_alerts():
    """Отримати активні тривоги з alerts.in.ua"""
    url = f"https://api.alerts.in.ua/v1/alerts/active.json?token={ALERTS_API_TOKEN}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data.get("alerts", [])
    except Exception as e:
        print("Помилка при запиті alerts.in.ua:", e)
        return []

def send_telegram_message(text, chat_id=None):
    if chat_id is None:
        chat_id = TELEGRAM_CHAT_ID
    try:
        data = {"chat_id": chat_id, "text": text}
        resp = requests.post(f"{TELEGRAM_API_URL}/sendMessage", data=data)
        resp.raise_for_status()
    except Exception as e:
        print("Помилка відправки повідомлення в Telegram:", e)

def check_alerts_loop():
    """Фоновий цикл опитування тривог"""
    global last_alert_active, active_districts
    while True:
        alerts = fetch_alerts()
        new_active_districts = set()

        for alert in alerts:
            if alert.get("location_oblast") == REGION_NAME:
                raion = alert.get("location_raion")
                if raion in DISTRICTS:
                    new_active_districts.add(raion)

        # Надсилання повідомлень по нових районах
        for district in new_active_districts - active_districts:
            send_telegram_message(f"⚠️ Тривога у {district} районі!")

        # Відбій тривоги
        if not new_active_districts and last_alert_active:
            send_telegram_message(f"✅ Відбій тривоги у {REGION_NAME}")

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
            send_telegram_message("Все чисто! Немає активних тривог.", chat_id=chat_id)
        else:
            districts_text = ", ".join(sorted(active_districts))
            send_telegram_message(f"Тривожаться райони: {districts_text}", chat_id=chat_id)

    return {"ok": True}

@app.get("/")
def root():
    return PlainTextResponse("Bot is running!")

if __name__ == "__main__":
    threading.Thread(target=check_alerts_loop, daemon=True).start()
    import uvicorn
    port = int(os.getenv("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
