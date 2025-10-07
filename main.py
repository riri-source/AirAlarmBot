import os
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
import asyncio
import nest_asyncio
import logging
import aiohttp
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

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

# ===== Будильник =====
async def keep_alive(port: int):
    url = f"http://localhost:{port}"
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    logging.info(f"Keep-alive ping відправлено, статус: {resp.status}")
        except Exception as e:
            logging.error(f"Помилка keep-alive ping: {e}")
        await asyncio.sleep(45)

# ===== Змінні оточення =====
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
ALERTS_TOKEN = os.getenv("ALERTS_TOKEN")
REGION = os.getenv("REGION", "Київська область")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 25))

if not TELEGRAM_TOKEN or not ALERTS_TOKEN:
    raise RuntimeError("Не задано одну або кілька обов'язкових змінних оточення: BOT_TOKEN, ALERTS_TOKEN")

API_URL = "https://api.alerts.in.ua/v1/alerts/active.json"

# ===== Словник типів тривог =====
ALERT_TYPES_UA = {
    "air_raid": "Повітряна тривога!",
    "chemical": "Хімічна тривога",
    "radiation": "Радіаційна тривога",
    "other": "Інша тривога",
}

# ===== Глобальні змінні =====
CHAT_ID = None
last_region_status = {}
last_city_status = {}

# ===== Хендлери =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CHAT_ID
    CHAT_ID = update.effective_chat.id
    await update.message.reply_text(
        f"Привіт 🌸\nНапиши «Що по області» щоб дізнатись, де зараз тривога у {REGION}."
    )

async def oblast_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CHAT_ID
    CHAT_ID = update.effective_chat.id
    await handle_region_alerts(update, REGION)

async def kyiv_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CHAT_ID
    CHAT_ID = update.effective_chat.id
    await handle_region_alerts(update, "м. Київ")

async def krym_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CHAT_ID
    CHAT_ID = update.effective_chat.id
    await handle_region_alerts(update, "Автономна Республіка Крим")

async def odessa_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CHAT_ID
    CHAT_ID = update.effective_chat.id
    await handle_city_alerts(update, "Одеса")

async def franuk_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CHAT_ID
    CHAT_ID = update.effective_chat.id
    await handle_city_alerts(update, "Івано-Франківськ")

# ===== Функції обробки =====
async def handle_region_alerts(update, region_name):
    headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(API_URL, headers=headers, timeout=10) as resp:
                data = await resp.json()

        region_alerts = [alert for alert in data.get("alerts", []) if alert.get("location_oblast") == region_name]

        if not region_alerts:
            await send_photo_and_message(update, "images/Saefty.jpg", f"✅ {region_name} - зараз все чисто!")
            return

        text = f"🚨 *Активні тривоги у {region_name}:*\n"
        for alert in region_alerts:
            raion = alert.get("location_title", "Невідомий район")
            alert_type = alert.get("alert_type", "невідомо")
            alert_type_ua = ALERT_TYPES_UA.get(alert_type, alert_type)
            text += f"• {raion} — {alert_type_ua}\n"

        await update.message.reply_markdown(text)

    except Exception as e:
        logging.error(f"Помилка при запиті до API для {region_name}: {e}")
        await update.message.reply_text(f"Помилка отримання даних: {e}")

async def handle_city_alerts(update, city_name):
    headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(API_URL, headers=headers, timeout=10) as resp:
                data = await resp.json()

        city_alerts = [alert for alert in data.get("alerts", []) if alert.get("location_title") == city_name]

        if not city_alerts:
            await send_photo_and_message(update, "images/Saefty.jpg", f"✅ У {city_name} зараз все чисто!")
            return

        text = f"🚨 *Активні тривоги у {city_name}:*\n"
        for alert in city_alerts:
            alert_type = alert.get("alert_type", "невідомо")
            alert_type_ua = ALERT_TYPES_UA.get(alert_type, alert_type)
            text += f"• {alert_type_ua}\n"

        await update.message.reply_markdown(text)

    except Exception as e:
        logging.error(f"Помилка при запиті до API для {city_name}: {e}")
        await update.message.reply_text(f"Помилка отримання даних: {e}")

async def send_photo_and_message(update, photo_path, text):
    try:
        with open(photo_path, "rb") as photo:
            await update.message.reply_photo(photo=photo)
        await update.message.reply_text(text)
    except Exception as e:
        logging.error(f"Помилка при відправці картинки: {e}")
        await update.message.reply_text(text)

# ===== Фонове опитування API =====
async def poll_alerts(app):
    global last_region_status, last_city_status, CHAT_ID
    headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(API_URL, headers=headers, timeout=10) as resp:
                    data = await resp.json()

            # ===== Області =====
            region_alerts = [a for a in data.get("alerts", []) if a.get("location_oblast") == REGION]
            await process_alert_changes(region_alerts, REGION, last_region_status, app)

            # ===== Міста =====
            for city_name in ["Одеса", "Івано-Франківськ"]:
                city_alerts = [a for a in data.get("alerts", []) if a.get("location_title") == city_name]
                await process_alert_changes(city_alerts, city_name, last_city_status, app, is_city=True)

        except Exception as e:
            logging.error(f"Помилка при опитуванні API: {e}")

        await asyncio.sleep(POLL_INTERVAL)

async def process_alert_changes(alert_list, area_name, last_status_dict, app, is_city=False):
    global CHAT_ID
    current_raions = {a.get("location_title", "Невідомий район"): a.get("alert_type", "невідомо") for a in alert_list}

    # Нові тривоги
    for raion, alert_type in current_raions.items():
        if last_status_dict.get(raion) != alert_type:
            photo = "images/Alarm.jpg"
            text = f"🚨 *{alert_type}* у {raion}" if not is_city else f"🚨 *{alert_type}* у {area_name}"
            await app.bot.send_photo(chat_id=CHAT_ID, photo=open(photo, "rb"))
            await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")

    # Відбій
    for raion in list(last_status_dict.keys()):
        if raion not in current_raions:
            photo = "images/Clear.jpg"
            text = f"✅ Відбій тривоги у {raion}" if not is_city else f"✅ Відбій тривоги у {area_name}"
            await app.bot.send_photo(chat_id=CHAT_ID, photo=open(photo, "rb"))
            await app.bot.send_message(chat_id=CHAT_ID, text=text)

    last_status_dict.clear()
    last_status_dict.update(current_raions)

# ===== Обробка помилок Telegram =====
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error(msg="Виникла помилка у хендлері:", exc_info=context.error)
    if update and hasattr(update, "message") and update.message:
        await update.message.reply_text("⚠️ Виникла внутрішня помилка бота. Спробуйте пізніше.")

# ===== Основний цикл =====
async def main():
    nest_asyncio.apply()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по області"), oblast_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по києву"), kyiv_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)як там крим"), krym_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по одесі"), odessa_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по франику"), franuk_alerts))

    app.add_error_handler(error_handler)

    asyncio.create_task(poll_alerts(app))
    asyncio.create_task(keep_alive(int(os.environ.get("PORT", 10000))))
    print("✅ Бот запущено...")
    await app.run_polling()

# ===== Запуск =====
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
