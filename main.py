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

# ===== Змінні оточення =====
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
ALERTS_TOKEN = os.getenv("ALERTS_TOKEN")
REGION = os.getenv("REGION", "Київська область")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 25))
CHAT_ID = os.getenv("CHAT_ID")  # спочатку з env

if not TELEGRAM_TOKEN or not ALERTS_TOKEN or not CHAT_ID:
    raise RuntimeError("Не задано одну або кілька обов'язкових змінних оточення: BOT_TOKEN, ALERTS_TOKEN, CHAT_ID")

API_URL = "https://api.alerts.in.ua/v1/alerts/active.json"

# ===== Словник типів тривог =====
ALERT_TYPES_UA = {
    "air_raid": "Повітряна тривога!",
    "chemical": "Хімічна тривога",
    "radiation": "Радіаційна тривога",
    "other": "Інша тривога",
}

# ===== Хендлери =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CHAT_ID
    CHAT_ID = update.effective_chat.id  # отримуємо chat_id після першого старту
    await update.message.reply_text(
        f"Привіт 🌸\nНапиши «Що по області» щоб дізнатись, де зараз тривога у {REGION}."
    )

async def oblast_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alerts = await fetch_alerts(REGION)
    if not alerts:
        await update.message.reply_text(f"✅ {REGION} - зараз все чисто!")
        try:
            with open("images/Saefty.jpg", "rb") as photo:
                await update.message.reply_photo(photo=photo)
        except Exception as e:
            logging.error(f"Помилка при відправці картинки: {e}")
        return

    text = f"🚨 *Активні тривоги у {REGION}:*\n"
    for alert in alerts:
        raion = alert.get("location_title", "Невідомий район")
        alert_type = alert.get("alert_type", "невідомо")
        alert_type_ua = ALERT_TYPES_UA.get(alert_type, alert_type)
        text += f"• {raion} — {alert_type_ua}\n"
    await update.message.reply_markdown(text)

async def city_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE, city_name, city_label):
    alerts = await fetch_alerts(city_name, city_type="city")
    if not alerts:
        await update.message.reply_text(f"✅ У {city_label} зараз все чисто!")
        try:
            with open("images/Saefty.jpg", "rb") as photo:
                await update.message.reply_photo(photo=photo)
        except Exception as e:
            logging.error(f"Помилка при відправці картинки: {e}")
        return

    text = f"🚨 У {city_label} зафіксована тривога!\n"
    for alert in alerts:
        raion = alert.get("location_title", "Невідомий район")
        alert_type = alert.get("alert_type", "невідомо")
        alert_type_ua = ALERT_TYPES_UA.get(alert_type, alert_type)
        text += f"• {raion} — {alert_type_ua}\n"
    await update.message.reply_text(text)

async def krym_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await city_alerts(update, context, "Автономна Республіка Крим", "Крим")

async def kyiv_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await city_alerts(update, context, "м. Київ", "Київ")

async def odesa_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await city_alerts(update, context, "м. Одеса", "Одеса")

async def frankivsk_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await city_alerts(update, context, "м. Івано-Франківськ", "Івано-Франківськ")

# ===== Функція для отримання тривог =====
async def fetch_alerts(location_name, city_type="oblast"):
    headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(API_URL, headers=headers, timeout=10) as resp:
                data = await resp.json()
        if city_type == "oblast":
            return [a for a in data.get("alerts", []) if a.get("location_oblast") == location_name]
        else:
            return [a for a in data.get("alerts", []) if a.get("location_title") == location_name or a.get("location_oblast") == location_name]
    except Exception as e:
        logging.error(f"Помилка при запиті до API: {e}")
        return []

# ===== Фонове опитування API =====
current_region_alerts = {}  # {район: тип тривоги}

async def poll_alerts(app):
    global current_region_alerts, CHAT_ID
    first_run = True
    while True:
        alerts = await fetch_alerts(REGION)
        new_state = {a.get("location_title"): a.get("alert_type") for a in alerts}

        # ===== Нові тривоги по районах =====
        for raion, alert_type in new_state.items():
            if current_region_alerts.get(raion) != alert_type:
                try:
                    # Спочатку картинка
                    with open("images/Alarm.jpg", "rb") as photo:
                        await app.bot.send_photo(chat_id=int(CHAT_ID), photo=photo)
                    # Потім текст з червоною мигалкою та жирним
                    alert_text = ALERT_TYPES_UA.get(alert_type, alert_type)
                    await app.bot.send_message(
                        chat_id=int(CHAT_ID),
                        text=f"🚨 *{raion}* — *{alert_text}*",
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logging.error(f"Помилка при відправці тривоги: {e}")

        # ===== Відбої по районах =====
        for raion, old_type in current_region_alerts.items():
            if raion not in new_state:
                try:
                    await app.bot.send_message(
                        chat_id=int(CHAT_ID),
                        text=f"✅ Відбій тривоги у *{raion}*",
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logging.error(f"Помилка при відправці відбою по району: {e}")

        # ===== Загальний відбій по області =====
        if current_region_alerts and not new_state:
            try:
                await app.bot.send_message(chat_id=int(CHAT_ID), text=f"✅ Відбій тривоги у {REGION}")
                with open("images/Clear.jpg", "rb") as photo:
                    await app.bot.send_photo(chat_id=int(CHAT_ID), photo=photo)
            except Exception as e:
                logging.error(f"Помилка при відправці відбою по області: {e}")

        current_region_alerts = new_state
        first_run = False
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

    # ===== Хендлери команд і тексту =====
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по області"), oblast_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)як там крим"), krym_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по києву"), kyiv_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по одесі"), odesa_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по франику"), frankivsk_alerts))

    app.add_error_handler(error_handler)

    # ===== Фонові задачі =====
    asyncio.create_task(poll_alerts(app))

    print("✅ Бот запущено...")
    await app.run_polling()

# ===== Запуск =====
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
