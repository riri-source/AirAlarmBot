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
# 🔹 Службові функції
# ======================================================
async def send_all_oblasts_to_admin(bot, admin_id: int):
    """Надсилає адміну список усіх областей, які бачить API."""
    try:
        data = await _get_api_data()
        oblasts = sorted({a.get("location_oblast") for a in data.get("alerts", []) if a.get("location_oblast")})
        if not oblasts:
            await bot.send_message(chat_id=admin_id, text="⚠️ API не повернуло жодної області.")
            return
        text = "🧭 *Список областей, які бачить API:*\n\n" + "\n".join(f"• {o}" for o in oblasts)
        await bot.send_message(chat_id=admin_id, text=text, parse_mode="Markdown")
    except Exception as e:
        await bot.send_message(chat_id=admin_id, text=f"❌ Помилка при запиті до API:\n{e}")

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
# 🔹 Глобальний моніторинг (вся Україна)
# ======================================================
async def process_alerts(app, cache: RegionAlertCache):
    """Моніторинг усіх областей: Київщину -> у групу, решту -> адміну."""
    data = await _get_api_data()
    alerts = data.get("alerts", [])
    new_state = {f"{a['location_oblast']}::{a['location_title']}": a["alert_type"] for a in alerts}

    chat_id = get_chat_id(app)
    admin_chat = int(ADMIN_ID)
    now = datetime.now().strftime("%H:%M:%S")
    logging.info(f"⏱ Перевірка API @ {now} ({len(new_state)} активних тривог)")

    if not cache.initialized:
        cache.last_alerts = new_state
        cache.initialized = True
        return

    # Нові або змінені тривоги
    for key, alert_type in new_state.items():
        if cache.last_alerts.get(key) == alert_type:
            continue

        oblast, title = key.split("::")
        text = f"🚨 *{oblast}* — {title}: *{ALERT_TYPES_UA.get(alert_type, alert_type)}*"

        # адміністратор — усі області
        if admin_chat:
            await app.bot.send_message(chat_id=admin_chat, text=text, parse_mode="Markdown")

        # група — лише Київщина
        if chat_id and oblast in {"Київська область", "м. Київ"}:
            await send_photo_safe(app.bot, chat_id, "images/Alarm.jpg")
            await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")

    # Відбої
    for key in list(cache.last_alerts.keys()):
        if key not in new_state:
            oblast, title = key.split("::")
            text = f"✅ Відбій тривоги у *{oblast}* — {title}"
            if admin_chat:
                await app.bot.send_message(chat_id=admin_chat, text=text, parse_mode="Markdown")
            if chat_id and oblast in {"Київська область", "м. Київ"}:
                await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")

    cache.last_alerts = new_state

# ======================================================
# 🔹 Команда /list_regions
# ======================================================
async def list_regions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("⛔️ Ця команда лише для адміністратора.")
        return
    await update.message.reply_text("⏳ Отримую список областей...")
    await send_all_oblasts_to_admin(context.bot, ADMIN_ID)

# ======================================================
# 🔹 Ручні запити (залишились без змін)
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
    await update.message.reply_text("🚨 У Криму триває тривога!" if active else "✅ У Криму зараз все чисто.")

async def odesa_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = await region_status("одес")
    await update.message.reply_text("🚨 В Одеській області триває тривога!" if active else "✅ В Одеській області зараз все чисто.")

async def oblast_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = await region_status("київська")
    await update.message.reply_text("🚨 У Київській області триває тривога!" if active else "✅ У Київській області зараз все чисто.")

async def kyiv_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = await region_status("київ")
    await update.message.reply_text("🚨 У Києві триває тривога!" if active else "✅ У Києві зараз все чисто.")

async def lugansk_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = await region_status("луган")
    await update.message.reply_text("🚨 У Луганській області триває тривога!" if active else "✅ У Луганській області зараз все чисто.")

async def chernihiv_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = await region_status("черніг")
    await update.message.reply_text("🚨 У Чернігівській області триває тривога!" if active else "✅ У Чернігівській області зараз все чисто.")

async def frankivsk_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = await region_status("франк")
    await update.message.reply_text("🚨 В Івано-Франківській області триває тривога!" if active else "✅ В Івано-Франківській області зараз все чисто.")

# ======================================================
# 🔹 Хендлер словникових запитів (без змін)
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
    if any(region in k for k in active_alerts.keys()):
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
                                    "Адміністратор отримує сповіщення по всій Україні.")

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

    await send_all_oblasts_to_admin(app.bot, ADMIN_ID)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stopbot", stopbot))
    app.add_handler(CommandHandler("list_regions", list_regions))

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
