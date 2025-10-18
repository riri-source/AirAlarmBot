import os
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
import asyncio
import nest_asyncio
import logging
import aiohttp
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ===== Load .env (якщо використовується) =====
load_dotenv()

# ===== Фейковий HTTP сервер для Render =====
class StubHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_http_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), StubHandler)
    server.serve_forever()

Thread(target=run_http_server, daemon=True).start()

# ===== Логування =====
logging.basicConfig(level=logging.INFO)

# ===== Змінні оточення =====
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
ALERTS_TOKEN = os.getenv("ALERTS_TOKEN")
REGION = os.getenv("REGION", "Київська область")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 25))

# CHAT_ID може бути задано як ENV або встановлюватись після /start
CHAT_ID_ENV = os.getenv("CHAT_ID")
CHAT_ID = int(CHAT_ID_ENV) if CHAT_ID_ENV else None

# Адмін для аварійної зупинки
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

if not TELEGRAM_TOKEN or not ALERTS_TOKEN:
    raise RuntimeError("Не задано одну або кілька обов'язкових змінних оточення: BOT_TOKEN, ALERTS_TOKEN")

API_URL = "https://api.alerts.in.ua/v1/alerts/active.json"

# ===== Словник типів тривог (українські назви) =====
ALERT_TYPES_UA = {
    "air_raid": "Повітряна тривога!",
    "chemical": "Хімічна тривога",
    "radiation": "Радіаційна тривога",
    "other": "Інша тривога",
}

# ===== Глобальні змінні для стану та тасків =====
current_region_alerts = {}  # {район: тип тривоги}
alerts_initialized = False


# ===== Допоміжні функції =====
async def fetch_alerts(location_name, city_type="oblast"):
    headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(API_URL, headers=headers, timeout=10) as resp:
                data = await resp.json()
        if city_type == "oblast":
            return [a for a in data.get("alerts", []) if a.get("location_oblast") == location_name]
        else:
            return [
                a
                for a in data.get("alerts", [])
                if a.get("location_title") == location_name or a.get("location_oblast") == location_name
            ]
    except Exception as e:
        logging.error(f"Помилка при запиті до API: {e}")
        return []


# ===== Хендлери =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CHAT_ID
    CHAT_ID = update.effective_chat.id  # зберігаємо chat_id після першої команди /start
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


# ===== Фонове опитування API =====
async def process_alerts(app):
    """Завантажує актуальні тривоги та розсилає оновлення у чат."""
    global current_region_alerts, alerts_initialized

    alerts = await fetch_alerts(REGION)
    new_state = {a.get("location_title"): a.get("alert_type") for a in alerts}

    # Перший запуск: просто запам'ятовуємо поточний стан, щоб не дублювати "старі" тривоги
    if not alerts_initialized:
        current_region_alerts = new_state
        alerts_initialized = True
        logging.debug("Ініціалізовано стан тривог без сповіщень.")
        return

    # Нові тривоги по районах
    for raion, alert_type in new_state.items():
        if current_region_alerts.get(raion) == alert_type:
            continue

        try:
            if CHAT_ID:
                try:
                    with open("images/Alarm.jpg", "rb") as photo:
                        await app.bot.send_photo(chat_id=int(CHAT_ID), photo=photo)
                except Exception as e:
                    logging.debug(f"Не вдалося відправити картинку: {e}")

                alert_text = ALERT_TYPES_UA.get(alert_type, alert_type)
                await app.bot.send_message(
                    chat_id=int(CHAT_ID),
                    text=f"🚨 *{raion}* — *{alert_text}*",
                    parse_mode="Markdown",
                )
            else:
                logging.info(f"[НОТИФ] {raion} — {alert_type} (CHAT_ID не задано)")
        except Exception as e:
            logging.error(f"Помилка при відправці тривоги: {e}")

    # Відбої по районах
    for raion, old_type in current_region_alerts.items():
        if raion in new_state:
            continue

        try:
            if CHAT_ID:
                await app.bot.send_message(
                    chat_id=int(CHAT_ID),
                    text=f"✅ Відбій тривоги у *{raion}*",
                    parse_mode="Markdown",
                )
            else:
                logging.info(f"[ОБВІД] Відбій у {raion} (CHAT_ID не задано)")
        except Exception as e:
            logging.error(f"Помилка при відправці відбою по району: {e}")

    # Загальний відбій по області
    if current_region_alerts and not new_state:
        try:
            if CHAT_ID:
                await app.bot.send_message(chat_id=int(CHAT_ID), text=f"✅ Відбій тривоги у {REGION}")
                try:
                    with open("images/Clear.jpg", "rb") as photo:
                        await app.bot.send_photo(chat_id=int(CHAT_ID), photo=photo)
                except Exception as e:
                    logging.debug(f"Не вдалося відправити картинку Clear: {e}")
            else:
                logging.info(f"[ОБВІД ОБЛАСТІ] Відбій у {REGION} (CHAT_ID не задано)")
        except Exception as e:
            logging.error(f"Помилка при відправці відбою по області: {e}")

    current_region_alerts = new_state


# ===== Команда аварійної зупинки (тільки для ADMIN_ID) =====
async def stopbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("⛔️ Ця команда доступна лише адміністратору.")
        return

    await update.message.reply_text("🛑 Отримано команду зупинки. Виконую вимкнення...")
    # Запускаємо shutdown як окрему задачу, щоб не блокувати хендлер
    asyncio.create_task(_shutdown_sequence(context.application))


async def _shutdown_sequence(app):
    logging.info("🔻 Shutdown requested by admin")

    # 1) зупиняємо job_queue, щоб не залишити повторювані задачі
    try:
        app.job_queue.stop()
    except Exception as e:
        logging.debug(f"Проблема під час job_queue.stop(): {e}")

    # 2) зупиняємо та шутдаун додатку (аккуратно)
    try:
        await app.shutdown()
    except Exception as e:
        logging.debug(f"Проблема під час app.shutdown(): {e}")
    try:
        await app.stop()
    except Exception as e:
        logging.debug(f"Проблема під час app.stop(): {e}")

    logging.info("⚙️ Бот вимкнено адміністратором. Зупиняю event loop.")
    # 3) зупиняємо event loop (це припинить run_forever у __main__)
    loop = asyncio.get_event_loop()
    loop.stop()


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
    app.add_handler(CommandHandler("stopbot", stopbot))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по області"), oblast_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)як там крим"), krym_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по києву"), kyiv_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по одесі"), odesa_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по франику"), frankivsk_alerts))

    app.add_error_handler(error_handler)

    # ===== Фонові задачі =====
    async def _job_callback(context: ContextTypes.DEFAULT_TYPE):
        await process_alerts(context.application)

    app.job_queue.run_repeating(_job_callback, interval=POLL_INTERVAL, first=0)

    logging.info("✅ Бот запущено...")
    # Запуск polling без автоматичного закриття loop (close_loop=False)
    await app.run_polling(close_loop=False)


# ===== Запуск =====
if __name__ == "__main__":
    # Використовуємо поточний event loop: запускаємо main як таску і тримаємо loop.run_forever()
    loop = asyncio.get_event_loop()
    try:
        loop.create_task(main())
        loop.run_forever()
    except KeyboardInterrupt:
        logging.info("🛑 Зупинка вручну (KeyboardInterrupt)")
    finally:
        # Далі коректно завершуємо всі таски
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        try:
            loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        except Exception:
            pass
        loop.close()
        logging.info("Loop closed. Exit.")
