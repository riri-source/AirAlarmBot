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
CHAT_ID = int(os.getenv("CHAT_ID"))

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
            try:
                with open("images/Saefty.jpg", "rb") as photo:
                    await update.message.reply_photo(photo=photo)
            except Exception as e:
                logging.error(f"Помилка при відправці картинки: {e}")
            return

        text = f"🚨 *Активні тривоги у {REGION}:*\n"
        for alert in region_alerts:
            raion = alert.get("location_title", "Невідомий район")
            alert_type = alert.get("alert_type", "невідомо")
            alert_type_ua = ALERT_TYPES_UA.get(alert_type, alert_type)
            text += f"• {raion} — {alert_type_ua}\n"

        await update.message.reply_markdown(text)

    except Exception as e:
        logging.error(f"Помилка при запиті до API для області: {e}")
        await update.message.reply_text(f"Помилка отримання даних: {e}")

async def krym_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(API_URL, headers=headers, timeout=10) as resp:
                data = await resp.json()

        krym_alerts_list = [
            alert for alert in data.get("alerts", [])
            if alert.get("location_oblast") == "Автономна Республіка Крим"
        ]

        if krym_alerts_list:
            text = "🚨 У Криму зафіксована тривога!\n"
            for alert in krym_alerts_list:
                raion = alert.get("location_title", "Невідомий район")
                alert_type = alert.get("alert_type", "невідомо")
                alert_type_ua = ALERT_TYPES_UA.get(alert_type, alert_type)
                text += f"• {raion} — {alert_type_ua}\n"
        else:
            text = "✅ У Криму зараз все спокійно (нема активних тривог)."

        await update.message.reply_text(text)

    except Exception as e:
        logging.error(f"Помилка при запиті до API для Криму: {e}")
        await update.message.reply_text(f"Помилка при запиті до API: {e}")

async def kyiv_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(API_URL, headers=headers, timeout=10) as resp:
                data = await resp.json()

        kyiv_alerts_list = [
            alert for alert in data.get("alerts", [])
            if alert.get("location_oblast") == "м. Київ"
        ]

        if kyiv_alerts_list:
            text = "🚨 У м.Київ зафіксована тривога!\n"
            for alert in kyiv_alerts_list:
                raion = alert.get("location_title", "Невідомий район")
                alert_type = alert.get("alert_type", "невідомо")
                alert_type_ua = ALERT_TYPES_UA.get(alert_type, alert_type)
                text += f"• {raion} — {alert_type_ua}\n"
        else:
            text = "✅ У м.Київ зараз все чисто!"
            try:
                with open("images/Saefty.jpg", "rb") as photo:
                    await update.message.reply_photo(photo=photo)
            except Exception as e:
                logging.error(f"Помилка при відправці картинки: {e}")

        await update.message.reply_text(text)

    except Exception as e:
        logging.error(f"Помилка при запиті до API для Києва: {e}")
        await update.message.reply_text(f"Помилка при запиті до API: {e}")

# ===== Фонове опитування API =====
last_region_alerts_count = 0  # для відбою тривоги

async def poll_alerts(app):
    global last_region_alerts_count
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

            # ===== Якщо є тривоги =====
            if region_alerts:
                text = f"🚨 *Активні тривоги у {REGION}:*\n"
                for alert in region_alerts:
                    raion = alert.get("location_title", "Невідомий район")
                    alert_type = alert.get("alert_type", "невідомо")
                    alert_type_ua = ALERT_TYPES_UA.get(alert_type, alert_type)
                    text += f"• {raion} — {alert_type_ua}\n"
                await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")

            # ===== Відбій тривоги =====
            if last_region_alerts_count > 0 and len(region_alerts) == 0:
                try:
                    await app.bot.send_message(chat_id=CHAT_ID,
                                               text=f"✅ Відбій тривоги у {REGION}")
                    with open("images/Clear.jpg", "rb") as photo:
                        await app.bot.send_photo(chat_id=CHAT_ID, photo=photo)
                except Exception as e:
                    logging.error(f"Помилка при відправці картинки відбою: {e}")

            last_region_alerts_count = len(region_alerts)

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

    app.add_error_handler(error_handler)

    asyncio.create_task(poll_alerts(app))
    print("✅ Бот запущено...")
    await app.run_polling()

# ===== Запуск =====
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
