import os
import asyncio
import logging
import json
import nest_asyncio
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
# 🔹 Завантаження середовища
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
API_URL = "https://api.alerts.in.ua/v1/alerts/active.json"

if not BOT_TOKEN or not ALERTS_TOKEN:
    raise RuntimeError("❌ Відсутні BOT_TOKEN або ALERTS_TOKEN")

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
# 🔹 Класи та хелпери
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

def get_chat_id(app) -> Optional[int]:
    chat_id = app.bot_data.get("chat_id")
    if chat_id:
        return int(chat_id)
    default_chat = app.bot_data.get("default_chat_id")
    return int(default_chat) if default_chat else None

async def send_photo_safe(bot, chat_id: Optional[int], image_path: str):
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
# 🔹 Завантаження зовнішнього словника
# ======================================================
def load_locations_dict(file_path: str = None) -> Dict:
    if file_path is None:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(base_dir, "locations_dict.json")

    if not os.path.exists(file_path):
        logging.warning("⚠️ Файл словника не знайдено, створюю порожній шаблон.")
        return {}
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        logging.info(f"📘 Завантажено словник запитів: {len(data.get('Київська область', {}))} пунктів.")
        return data
    except Exception as e:
        logging.error(f"Помилка при завантаженні словника: {e}")
        return {}

# ======================================================
# 🔹 МРЧ — моніторинг Київської області + Тестового регіону
# ======================================================
async def process_alerts(app, cache: RegionAlertCache):
    data = await _get_api_data()
    alerts = data.get("alerts", [])
    relevant = [
        a for a in alerts
        if a.get("location_oblast") in {"Київська область", "м. Київ", "Тестовий регіон"}
    ]
    new_state = {a["location_title"]: a["alert_type"] for a in relevant}
    chat_id = get_chat_id(app)
    admin_chat = int(ADMIN_ID)
    logging.info(f"⏱ Перевірка Київська область @ {datetime.now().strftime('%H:%M:%S')}")

    if not cache.initialized:
        cache.last_alerts = new_state
        cache.initialized = True
        return

    for raion, alert_type in new_state.items():
        oblast = next((a.get("location_oblast") for a in alerts if a.get("location_title") == raion), "")
        is_test_region = "тестов" in oblast.lower()
        target_chat = admin_chat if is_test_region else chat_id
        prefix = "🧪 [ТЕСТ]" if is_test_region else "🚨"

        if cache.last_alerts.get(raion) != alert_type and target_chat:
            await send_photo_safe(app.bot, target_chat, "images/Alarm.jpg")
            await app.bot.send_message(
                chat_id=target_chat,
                text=f"{prefix} *{raion}* — *{ALERT_TYPES_UA.get(alert_type, alert_type)}*",
                parse_mode="Markdown",
            )

    # Відбої
    for raion in list(cache.last_alerts.keys()):
        if raion not in new_state:
            oblast = next((a.get("location_oblast") for a in alerts if a.get("location_title") == raion), "")
            is_test_region = "тестов" in oblast.lower()
            target_chat = admin_chat if is_test_region else chat_id
            prefix = "🧪 [ТЕСТ]" if is_test_region else "✅"
            if target_chat:
                await app.bot.send_message(
                    chat_id=target_chat,
                    text=f"{prefix} Відбій тривоги у *{raion}*",
                    parse_mode="Markdown",
                )

    if cache.last_alerts and not new_state and chat_id:
        await app.bot.send_message(chat_id=chat_id, text=f"✅ Відбій тривоги у {REGION}")
        await send_photo_safe(app.bot, chat_id, "images/Clear.jpg")

    cache.last_alerts = new_state

# ======================================================
# 🔹 Ручні запити по областях / містах
# ======================================================
async def region_status(keyword: str) -> bool:
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
    if active:
        await update.message.reply_text("🚨 У Криму триває тривога!")
    else:
        await update.message.reply_text("✅ У Криму зараз все чисто.")

async def odesa_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = await region_status("одес")
    if active:
        await update.message.reply_text("🚨 В Одеській області триває тривога!")
    else:
        await update.message.reply_text("✅ В Одеській області зараз все чисто.")

async def oblast_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = await region_status("київська")
    if active:
        await update.message.reply_text("🚨 У Київській області триває тривога!")
    else:
        await update.message.reply_text("✅ У Київській області зараз все чисто.")

async def kyiv_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = await region_status("київ")
    if active:
        await update.message.reply_text("🚨 У Києві триває тривога!")
    else:
        await update.message.reply_text("✅ У Києві зараз все чисто.")

async def lugansk_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = await region_status("луган")
    if active:
        await update.message.reply_text("🚨 У Луганській області триває тривога!")
    else:
        await update.message.reply_text("✅ У Луганській області зараз все чисто.")

async def chernihiv_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = await region_status("черніг")
    if active:
        await update.message.reply_text("🚨 У Чернігівській області триває тривога!")
    else:
        await update.message.reply_text("✅ У Чернігівській області зараз все чисто.")

async def frankivsk_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = await region_status("франк")
    if active:
        await update.message.reply_text("🚨 В Івано-Франківській області триває тривога!")
    else:
        await update.message.reply_text("✅ В Івано-Франківській області зараз все чисто.")

# ======================================================
# 🔹 Хендлер словникових запитів
# ======================================================
async def handle_dynamic_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").lower().strip()
    if any(x in text for x in ["що по області", "що по києву", "що по крим", "що по одес", "що по луган", "що по франик", "що по черніг"]):
        return

    locations = context.application.bot_data.get("locations_dict", {}).get("Київська область", {})
    keyword = text.replace("що по", "").replace("?", "").strip().lower()

    region = None
    for key, val in locations.items():
        if keyword == key.lower() or keyword in key.lower():
            region = val
            break

    if not region:
        await update.message.reply_text("🤔 Я ще не знаю такого населеного пункту. Його можна додати до словника.")
        return

    cache: RegionAlertCache = context.application.bot_data.get("alert_cache", RegionAlertCache())
    active_alerts = cache.last_alerts or {}
    if region in active_alerts:
        await update.message.reply_text(f"🚨 У {region} триває тривога!")
    else:
        await update.message.reply_text(f"✅ У {region} зараз все спокійно.")

# ======================================================
# 🔹 Базові команди
# ======================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["chat_id"] = update.effective_chat.id
    await update.message.reply_text("Привіт 🌸\n"
                                    "Я повідомляю про тривоги у Київській області.\n"
                                    "Можеш спробувати: «що по ірпеню?» або «що по борисполю?»")

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

    locations_dict = load_locations_dict()
    app.bot_data["locations_dict"] = locations_dict

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stopbot", stopbot))

    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по області"), oblast_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по києву"), kyiv_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)як там крим"), krym_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по одес"), odesa_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по франику|що по івано-франківську|що по франківську"), frankivsk_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по луган"), lugansk_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по черніг"), chernihiv_alerts))

    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)^що по "), handle_dynamic_query))
    app.add_error_handler(error_handler)

    async def _poll(context: ContextTypes.DEFAULT_TYPE):
        await process_alerts(context.application, cache)

    app.job_queue.run_repeating(_poll, interval=POLL_INTERVAL, first=0)
    app.job_queue.start()

    logging.info("✅ Бот запущено й готовий до роботи.")
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
