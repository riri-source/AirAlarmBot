import os
import asyncio
import nest_asyncio
import logging
import aiohttp
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
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

# ===== Автоматичне збереження chat_id =====
CHAT_ID_FILE = "chat_id.txt"
CHAT_ID = None

def get_saved_chat_id():
    global CHAT_ID
    if os.path.exists(CHAT_ID_FILE):
        try:
            CHAT_ID = int(open(CHAT_ID_FILE).read().strip())
        except:
            CHAT_ID = None

async def register_chat_id(update: Update):
    global CHAT_ID
    chat_id = update.effective_chat.id
    if CHAT_ID is None:
        CHAT_ID = chat_id
        with open(CHAT_ID_FILE, "w") as f:
            f.write(str(CHAT_ID))
        logging.info(f"Збережено chat_id групи: {CHAT_ID}")
        await update.message.reply_text("✅ Chat_id групи збережено, тепер тривоги будуть надходити сюди.")

# ===== Хендлери =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await register_chat_id(update)
    await update.message.reply_text(
        f"Привіт 🌸\nНапиши «Що по області» щоб дізнатись, де зараз тривога у {REGION}."
    )

async def oblast_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await register_chat_id(update)
    await send_region_alerts(update, REGION)

async def kyiv_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await register_chat_id(update)
    await send_city_alerts(update, "м. Київ")

async def krym_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await register_chat_id(update)
    await send_region_alerts(update, "Автономна Республіка Крим")

async def odesa_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await register_chat_id(update)
    await send_city_alerts(update, "м. Одеса")

async def frankivsk_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await register_chat_id(update)
    await send_city_alerts(update, "м. Івано-Франківськ")

# ===== Функції для надсилання =====
async def fetch_alerts():
    headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(API_URL, headers=headers, timeout=10) as resp:
            return await resp.json()

async def send_region_alerts(update, region_name):
    data = await fetch_alerts()
    region_alerts = [a for a in data.get("alerts", []) if a.get("location_oblast") == region_name]

    if not region_alerts:
        try:
            with open("images/Saefty.jpg", "rb") as photo:
                await update.message.reply_photo(photo=photo)
            await update.message.reply_text(f"✅ {region_name} зараз все чисто!")
        except Exception as e:
            logging.error(f"Помилка при відправці картинки: {e}")
        return

    text = f"🚨 *Активні тривоги у {region_name}:*\n"
    for alert in region_alerts:
        raion = alert.get("location_title", "Невідомий район")
        alert_type = alert.get("alert_type", "невідомо")
        alert_type_ua = ALERT_TYPES_UA.get(alert_type, alert_type)
        text += f"• {raion} — {alert_type_ua}\n"

    await update.message.reply_text(text, parse_mode="Markdown")

async def send_city_alerts(update, city_name):
    data = await fetch_alerts()
    city_alerts = [a for a in data.get("alerts", []) if a.get("location_oblast") == city_name or a.get("location_title") == city_name]

    if not city_alerts:
        try:
            with open("images/Saefty.jpg", "rb") as photo:
                await update.message.reply_photo(photo=photo)
            await update.message.reply_text(f"✅ У {city_name} зараз все чисто!")
        except Exception as e:
            logging.error(f"Помилка при відправці картинки: {e}")
        return

    text = f"🚨 *Тривога у {city_name}:*\n"
    for alert in city_alerts:
        raion = alert.get("location_title", "Невідомий район")
        alert_type = alert.get("alert_type", "невідомо")
        alert_type_ua = ALERT_TYPES_UA.get(alert_type, alert_type)
        text += f"• {raion} — {alert_type_ua}\n"

    await update.message.reply_text(text, parse_mode="Markdown")

# ===== Фонове опитування API по районах/містах =====
last_alerts_state = {}  # {location: set(alert_type)}

async def poll_alerts(app):
    global last_alerts_state
    while True:
        try:
            data = await fetch_alerts()
            region_alerts = [a for a in data.get("alerts", []) if a.get("location_oblast") == REGION]

            # Мапимо тривоги по районах
            current_state = {}
            for alert in region_alerts:
                loc = alert.get("location_title", "Невідомий район")
                atype = alert.get("alert_type", "невідомо")
                current_state.setdefault(loc, set()).add(atype)

            # ===== Виявляємо нові тривоги =====
            for loc, types in current_state.items():
                if loc not in last_alerts_state or types != last_alerts_state.get(loc, set()):
                    # Надсилаємо пост тривоги
                    try:
                        with open("images/Alarm.jpg", "rb") as photo:
                            await app.bot.send_photo(chat_id=CHAT_ID, photo=photo)
                        text = f"🚨 *Тривога у {loc}:*\n"
                        for t in types:
                            text += f"• {ALERT_TYPES_UA.get(t,t)}\n"
                        await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")
                    except Exception as e:
                        logging.error(f"Помилка при відправці тривоги: {e}")

            # ===== Виявляємо відбої =====
            for loc, types in last_alerts_state.items():
                if loc not in current_state or current_state[loc] != types:
                    try:
                        with open("images/Clear.jpg", "rb") as photo:
                            await app.bot.send_photo(chat_id=CHAT_ID, photo=photo)
                        await app.bot.send_message(chat_id=CHAT_ID,
                                                   text=f"💚 Відбій тривоги у {loc}")
                    except Exception as e:
                        logging.error(f"Помилка при відправці відбою: {e}")

            last_alerts_state = current_state

        except Exception as e:
            logging.error(f"Помилка при опитуванні API: {e}")

        await asyncio.sleep(POLL_INTERVAL)

# ===== Keep-alive для Render =====
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

# ===== Обробка помилок Telegram =====
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error(msg="Виникла помилка у хендлері:", exc_info=context.error)
    if update and hasattr(update, "message") and update.message:
        await update.message.reply_text("⚠️ Виникла внутрішня помилка бота. Спробуйте пізніше.")

# ===== Основний цикл =====
async def main():
    global CHAT_ID
    nest_asyncio.apply()
    get_saved_chat_id()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по області"), oblast_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)як там крим"), krym_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по києву"), kyiv_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по одесі"), odesa_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по франику"), frankivsk_alerts))

    # Будемо реєструвати chat_id на будь-яке повідомлення
    app.add_handler(MessageHandler(filters.ALL, register_chat_id))

    app.add_error_handler(error_handler)

    asyncio.create_task(poll_alerts(app))
    asyncio.create_task(keep_alive(int(os.environ.get("PORT", 10000))))

    print("✅ Бот запущено...")
    await app.run_polling()

# ===== Запуск =====
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
