import os
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
import asyncio
import nest_asyncio
import logging
import aiohttp
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# ===== Словничок типів тривог =====
ALERT_TYPE_UA = {
    "air_raid": "Повітряна тривога!",
    "artillery_shelling": "Артилерійський обстріл!",
    "urban_fights": "Бої в населеному пункті!",
    "chemical_threat": "Хімічна небезпека!",
    "nuclear_threat": "Радіаційна небезпека!",
    "unknown": "Невідома тривога!"
}

# ===== Фейковий HTTP сервер для Render =====
class StubHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_http_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), StubHandler)
    server.serve_forever()

Thread(target=run_http_server, daemon=True).start()

# ===== Логування =====
logging.basicConfig(level=logging.INFO)

# ===== Змінні оточення =====
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
ALERTS_TOKEN = os.getenv("ALERTS_TOKEN")
REGION = os.getenv("REGION", "Київська область")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 25))
CHAT_ID = int(os.getenv("CHAT_ID", "177475616"))

if not TELEGRAM_TOKEN or not ALERTS_TOKEN or not CHAT_ID:
    raise RuntimeError("Не задано одну або кілька обов'язкових змінних оточення: BOT_TOKEN, ALERTS_TOKEN, CHAT_ID")

API_URL = "https://api.alerts.in.ua/v1/alerts/active.json"

# ===== Хендлери =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Привіт 🌸\nНапиши «Що по області» щоб дізнатись, де зараз тривога у {REGION}."
    )

async def oblast_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(API_URL, headers=headers, timeout=10) as resp:
                data = await resp.json()

        region_alerts = [
            alert for alert in data.get("alerts", [])
            if alert.get("location_oblast") == REGION
        ]

        if not region_alerts:
            await update.message.reply_text(f"✅ {REGION} - зараз все чисто!")
            with open("images/Saefty.jpg", "rb") as photo:
                await update.message.reply_photo(photo=photo)
            return

        text = f"🚨 *Активні тривоги у {REGION}:*\n"
        for alert in region_alerts:
            raion = alert.get("location_title", "Невідомий район")
            alert_type = ALERT_TYPE_UA.get(alert.get("alert_type", "unknown"), "Невідома тривога!")
            text += f"• {raion} — {alert_type}\n"

        await update.message.reply_markdown(text)

    except Exception as e:
        await update.message.reply_text(f"Помилка отримання даних: {e}")

# ===== Тестовий запит: "Як там Крим?" =====
async def krym_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(API_URL, headers=headers, timeout=10) as resp:
                data = await resp.json()

        krym_alerts = [
            alert for alert in data.get("alerts", [])
            if alert.get("location_oblast") == "Автономна Республіка Крим"
        ]

        if krym_alerts:
            text = "🚨 У Криму зафіксована тривога!\n"
            for alert in krym_alerts:
                raion = alert.get("location_title", "Невідомий район")
                alert_type = ALERT_TYPE_UA.get(alert.get("alert_type", "unknown"), "Невідома тривога!")
                text += f"• {raion} — {alert_type}\n"
        else:
            text = "✅ У Криму зараз все спокійно (нема активних тривог)."

        await update.message.reply_text(text)

    except Exception as e:
        await update.message.reply_text(f"Помилка при запиті до API: {e}")

# ===== Новий тестовий запит: "Що по Києву?" =====
async def kyiv_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(API_URL, headers=headers, timeout=10) as resp:
                data = await resp.json()

        kyiv_alerts = [
            alert for alert in data.get("alerts", [])
            if alert.get("location_city") == "Київ"
        ]

        if kyiv_alerts:
            text = "🚨 У м.Київ зафіксована тривога!\n"
            for alert in kyiv_alerts:
                raion = alert.get("location_title", "Невідомий район")
                alert_type = ALERT_TYPE_UA.get(alert.get("alert_type", "unknown"), "Невідома тривога!")
                text += f"• {raion} — {alert_type}\n"
        else:
            text = "✅ У м.Київ зараз все спокійно (нема активних тривог)."

        await update.message.reply_text(text)

    except Exception as e:
        await update.message.reply_text(f"Помилка при запиті до API: {e}")

# ===== Функції для відбоїв та тривог =====
async def send_clear_message(app, region):
    caption = f"✅ Відбій повітряної тривоги в {region}!"
    photo_path = "images/Clear.jpg"
    with open(photo_path, "rb") as photo:
        await app.bot.send_photo(chat_id=CHAT_ID, photo=photo, caption=caption)

async def send_alarm_message(app, region, alerts):
    text = f"🚨 *Активні тривоги у {region}:*\n"
    for alert in alerts:
        raion = alert.get("location_title", "Невідомий район")
        alert_type = ALERT_TYPE_UA.get(alert.get("alert_type", "unknown"), "Невідома тривога!")
        text += f"• {raion} — {alert_type}\n"

    photo_path = "images/Alarm.jpg"
    with open(photo_path, "rb") as photo:
        await app.bot.send_photo(chat_id=CHAT_ID, photo=photo, caption=text, parse_mode="Markdown")

# ===== Фонове опитування API з відбоєм та картинками =====
last_alert_active = False

async def poll_alerts(app):
    global last_alert_active
    while True:
        headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(API_URL, headers=headers, timeout=10) as resp:
                    data = await resp.json()

            region_alerts = [
                alert for alert in data.get("alerts", [])
                if alert.get("location_oblast") == REGION
            ]

            if region_alerts and not last_alert_active:
                last_alert_active = True
                await send_alarm_message(app, REGION, region_alerts)

            elif not region_alerts and last_alert_active:
                last_alert_active = False
                await send_clear_message(app, REGION)

        except Exception as e:
            print(f"Помилка при опитуванні API: {e}")

        await asyncio.sleep(POLL_INTERVAL)

# ===== Основний цикл =====
async def main():
    nest_asyncio.apply()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по області"), oblast_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)як там крим"), krym_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по києву"), kyiv_alerts))
    asyncio.create_task(poll_alerts(app))
    print("✅ Бот запущено...")
    await app.run_polling()

# ===== Запуск =====
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
