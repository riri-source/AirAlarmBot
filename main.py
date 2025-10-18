import os
import asyncio
import nest_asyncio
import logging
from dataclasses import dataclass, field
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, Optional
from datetime import datetime
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

# ======================================================
# 🔹 Ініціалізація середовища
# ======================================================
load_dotenv()
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ALERTS_TOKEN = os.getenv("ALERTS_TOKEN")
REGION = os.getenv("REGION", "Київська область")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 25))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHAT_ID_ENV = os.getenv("CHAT_ID")
DEFAULT_CHAT_ID = int(CHAT_ID_ENV) if CHAT_ID_ENV else None

if not BOT_TOKEN or not ALERTS_TOKEN:
    raise RuntimeError("❌ Відсутні необхідні змінні BOT_TOKEN або ALERTS_TOKEN")

API_URL = "https://api.alerts.in.ua/v1/alerts/active.json"

# ======================================================
# 🔹 Локальний HTTP сервер (healthcheck)
# ======================================================
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

# ======================================================
# 🔹 Допоміжні структури
# ======================================================
@dataclass
class RegionAlertCache:
    last_alerts: Dict[str, str] = field(default_factory=dict)
    initialized: bool = False

ALERT_TYPES_UA = {
    "air_raid": "Повітряна тривога!",
    "chemical": "Хімічна тривога",
    "radiation": "Радіаційна тривога",
    "other": "Інша тривога",
}

# ======================================================
# 🔹 Допоміжні функції
# ======================================================
def get_chat_id(app) -> Optional[int]:
    chat_id = app.bot_data.get("chat_id")
    if chat_id:
        return int(chat_id)
    default_chat = app.bot_data.get("default_chat_id")
    return int(default_chat) if default_chat else None


async def send_photo_safe(bot, chat_id: Optional[int], image_path: str) -> None:
    if not chat_id:
        return
    try:
        with open(image_path, "rb") as photo:
            await bot.send_photo(chat_id=chat_id, photo=photo)
    except Exception:
        pass


async def _get_api_data():
    headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(API_URL, headers=headers, timeout=10) as resp:
            return await resp.json()

# ======================================================
# 🔹 МРЧ — моніторинг Київської області + м. Київ
# ======================================================
async def process_alerts(app, cache: RegionAlertCache):
    """Відстежує тільки Київську область і м. Київ"""
    data = await _get_api_data()
    alerts = data.get("alerts", [])
    relevant = [a for a in alerts if a.get("location_oblast") in {"Київська область", "м. Київ"}]

    new_state = {a["location_title"]: a["alert_type"] for a in relevant}
    chat_id = get_chat_id(app)

    if not cache.initialized:
        cache.last_alerts = new_state
        cache.initialized = True
        logging.info("Ініціалізовано стан МРЧ без сповіщень.")
        return

    # нові або змінені тривоги
    for raion, alert_type in new_state.items():
        if cache.last_alerts.get(raion) != alert_type:
            if chat_id:
                await send_photo_safe(app.bot, chat_id, "images/Alarm.jpg")
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=f"🚨 *{raion}* — *{ALERT_TYPES_UA.get(alert_type, alert_type)}*",
                    parse_mode="Markdown",
                )

    # відбої
    for raion in list(cache.last_alerts.keys()):
        if raion not in new_state:
            if chat_id:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=f"✅ Відбій тривоги у *{raion}*",
                    parse_mode="Markdown",
                )

    # загальний відбій
    if cache.last_alerts and not new_state:
        if chat_id:
            await app.bot.send_message(chat_id=chat_id, text=f"✅ Відбій тривоги у {REGION}")
            await send_photo_safe(app.bot, chat_id, "images/Clear.jpg")

    cache.last_alerts = new_state


# ======================================================
# 🔹 Ручні запити — окремо від МРЧ
# ======================================================
async def region_status(keyword: str) -> bool:
    """Повертає True, якщо знайдено тривогу для вказаного регіону."""
    data = await _get_api_data()
    kw = keyword.lower()
    for a in data.get("alerts", []):
        if a.get("finished_at") is None:
            oblast = (a.get("location_oblast") or "").lower()
            title = (a.get("location_title") or "").lower()
            if kw in oblast or kw in title:
                return True
    return False


async def krym_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = await region_status("крим")
    chat_id = update.effective_chat.id
    if active:
        await update.message.reply_text("🚨 У Криму триває тривога!")
        await send_photo_safe(context.application.bot, chat_id, "images/Alarm.jpg")
    else:
        await update.message.reply_text("✅ У Криму зараз все чисто.")
        await send_photo_safe(context.application.bot, chat_id, "images/Saefty.jpg")


async def lugansk_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = await region_status("луган")
    chat_id = update.effective_chat.id
    if active:
        await update.message.reply_text("🚨 У Луганській області триває тривога!")
        await send_photo_safe(context.application.bot, chat_id, "images/Alarm.jpg")
    else:
        await update.message.reply_text("✅ У Луганській області зараз все чисто.")
        await send_photo_safe(context.application.bot, chat_id, "images/Saefty.jpg")


async def chernihiv_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = await region_status("черніг")
    chat_id = update.effective_chat.id
    if active:
        await update.message.reply_text("🚨 У Чернігівській області триває тривога!")
        await send_photo_safe(context.application.bot, chat_id, "images/Alarm.jpg")
    else:
        await update.message.reply_text("✅ У Чернігівській області зараз все чисто.")
        await send_photo_safe(context.application.bot, chat_id, "images/Saefty.jpg")


# ======================================================
# 🔹 Команди користувача
# ======================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["chat_id"] = update.effective_chat.id
    await update.message.reply_text(
        f"Привіт 🌸\nЯ сповіщаю про тривоги в Київській області.\n"
        f"Напиши «що по Криму» або «що по Луганській» для ручних перевірок."
    )


async def stopbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔️ Команда доступна лише адміністратору.")
        return
    await update.message.reply_text("🛑 Зупиняю роботу...")
    asyncio.create_task(_shutdown_sequence(context.application))


async def _shutdown_sequence(app):
    try:
        app.job_queue.stop()
        await app.shutdown()
        await app.stop()
    except Exception:
        pass
    asyncio.get_event_loop().stop()


async def error_handler(update, context):
    logging.error("Помилка:", exc_info=context.error)
    if update and hasattr(update, "message") and update.message:
        await update.message.reply_text("⚠️ Виникла помилка, спробуй пізніше.")


# ======================================================
# 🔹 Основний цикл
# ======================================================
async def main():
    nest_asyncio.apply()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    if DEFAULT_CHAT_ID:
        app.bot_data["chat_id"] = DEFAULT_CHAT_ID
        app.bot_data["default_chat_id"] = DEFAULT_CHAT_ID

    cache = RegionAlertCache()
    app.bot_data["alert_cache"] = cache

    # Команди
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stopbot", stopbot))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по крим"), krym_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по луган"), lugansk_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по черніг"), chernihiv_alerts))

    app.add_error_handler(error_handler)

    # МРЧ-планувальник
    async def _poll(context: ContextTypes.DEFAULT_TYPE):
        await process_alerts(context.application, cache)

    app.job_queue.run_repeating(_poll, interval=POLL_INTERVAL, first=0)

    logging.info("✅ Бот запущено та готовий до роботи.")
    await app.run_polling(close_loop=False)


# ======================================================
# 🔹 Запуск
# ======================================================
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.create_task(main())
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for task in asyncio.all_tasks():
            task.cancel()
        loop.close()
        logging.info("🛑 Бот зупинено.")
