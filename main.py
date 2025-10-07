import os
import asyncio
import logging
import nest_asyncio
import aiohttp
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer

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

# ===== Keep-alive =====
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
CHAT_ID = int(os.getenv("CHAT_ID"))

if not TELEGRAM_TOKEN or not ALERTS_TOKEN or not CHAT_ID:
    raise RuntimeError("Не задано одну або кілька обов'язкових змінних оточення: BOT_TOKEN, ALERTS_TOKEN, CHAT_ID")

API_URL = "https://api.alerts.in.ua/v1/alerts/active.json"

# ===== Словник типів тривог =====
ALERT_TYPES_UA = {
    "air_raid": "Повітряна тривога!",
    "chemical": "Хімічна тривога!",
    "radiation": "Радіаційна тривога!",
    "other": "Інша тривога!",
}

# ===== Хендлери =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Привіт 🌸\nНапиши «Що по області» щоб дізнатись, де зараз тривога у {REGION}."
    )

async def oblast_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await city_or_region_alert(update, REGION, is_region=True)

async def kyiv_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await city_or_region_alert(update, "м. Київ", is_region=False)

async def krym_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await city_or_region_alert(update, "Автономна Республіка Крим", is_region=True)

async def odesa_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await city_or_region_alert(update, "м. Одеса", is_region=False)

async def franyk_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await city_or_region_alert(update, "м. Івано-Франківськ", is_region=False)

async def city_or_region_alert(update, location_name, is_region=True):
    headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(API_URL, headers=headers, timeout=10) as resp:
                data = await resp.json()

        alerts_list = [
            alert for alert in data.get("alerts", [])
            if (alert.get("location_oblast") == location_name if is_region else alert.get("location_title") == location_name)
        ]

        if not alerts_list:
            await update.message.reply_text(f"✅ {location_name} — зараз все чисто!")
            try:
                with open("images/Saefty.jpg", "rb") as photo:
                    await update.message.reply_photo(photo=photo)
            except Exception as e:
                logging.error(f"Помилка при відправці картинки: {e}")
            return

        text = f"🚨 *Активні тривоги у {location_name}:*\n"
        for alert in alerts_list:
            raion = alert.get("location_title", "Невідомий район")
            alert_type = alert.get("alert_type", "невідомо")
            alert_type_ua = ALERT_TYPES_UA.get(alert_type, alert_type)
            text += f"• {raion} — {alert_type_ua}\n"

        await update.message.reply_markdown(text)

    except Exception as e:
        logging.error(f"Помилка при запиті до API для {location_name}: {e}")
        await update.message.reply_text(f"Помилка отримання даних: {e}")

# ===== Фонове опитування API =====
current_state = {}  # зберігає активні тривоги по районах/містах

async def poll_alerts(app):
    global current_state
    headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
    first_run = True

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(API_URL, headers=headers, timeout=10) as resp:
                    data = await resp.json()

            region_alerts = [alert for alert in data.get("alerts", []) if alert.get("location_oblast") == REGION]

            new_state = {alert.get("location_title", "Невідомий район"): alert.get("alert_type") for alert in region_alerts}

            if first_run:
                current_state = new_state
                first_run = False
            else:
                # ===== Нові тривоги =====
                for loc, alert_type in new_state.items():
                    if loc not in current_state or current_state[loc] != alert_type:
                        try:
                            with open("images/Alarm.jpg", "rb") as photo:
                                await app.bot.send_photo(chat_id=CHAT_ID, photo=photo)
                            text = f"⚠️ *{loc} — {ALERT_TYPES_UA.get(alert_type, alert_type)}*"
                            await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")
                        except Exception as e:
                            logging.error(f"Помилка при відправці тривоги: {e}")

                # ===== Відбій тривоги =====
                for loc in list(current_state.keys()):
                    if loc not in new_state:
                        try:
                            with open("images/Clear.jpg", "rb") as photo:
                                await app.bot.send_photo(chat_id=CHAT_ID, photo=photo)
                            text = f"✅ Відбій тривоги у {loc}"
                            await app.bot.send_message(chat_id=CHAT_ID, text=text)
                        except Exception as e:
                            logging.error(f"Помилка при відправці відбою: {e}")

                current_state = new_state

        except Exception as e:
            logging.error(f"Помилка при опитуванні API: {e}")

        await asyncio.sleep(POLL_INTERVAL)

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
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)як там крим"), krym_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по києву"), kyiv_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по одесі"), odesa_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по франику"), franyk_alerts))

    app.add_error_handler(error_handler)

    asyncio.create_task(poll_alerts(app))
    asyncio.create_task(keep_alive(int(os.environ.get("PORT", 10000))))

    print("✅ Бот запущено...")
    await app.run_polling()

# ===== Запуск =====
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
