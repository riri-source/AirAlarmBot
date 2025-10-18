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
# 🔹 Завантаження та збереження словника
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
            return json.load(f)
    except Exception as e:
        logging.error(f"Помилка при завантаженні словника: {e}")
        return {}

def save_locations_dict(data: Dict):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(base_dir, "locations_dict.json")
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logging.info("💾 Словник оновлено на сервері.")
    except Exception as e:
        logging.error(f"Помилка при записі словника: {e}")

# ======================================================
# 🔹 МРЧ — моніторинг Київщини + глобальний моніторинг
# ======================================================
async def process_alerts(app, cache: RegionAlertCache):
    data = await _get_api_data()
    alerts = data.get("alerts", [])
    chat_id = get_chat_id(app)
    now = datetime.now().strftime("%H:%M:%S")

    # --- Київська область + м.Київ (група)
    relevant_kyiv = [a for a in alerts if a.get("location_oblast") in {"Київська область", "м. Київ"}]
    new_state_kyiv = {a["location_title"]: a["alert_type"] for a in relevant_kyiv}

    # --- Глобальний моніторинг (для ADMIN_ID)
    new_state_global = {f"{a['location_oblast']} — {a['location_title']}": a["alert_type"] for a in alerts}

    logging.info(f"⏱ Перевірка @ {now}: {len(alerts)} тривог")

    # перший запуск — ініціалізація
    if not cache.initialized:
        cache.last_alerts = new_state_kyiv
        cache.initialized = True
        return

    # ---- Сповіщення для Київщини (група)
    for raion, alert_type in new_state_kyiv.items():
        if cache.last_alerts.get(raion) != alert_type and chat_id:
            await send_photo_safe(app.bot, chat_id, "images/Alarm.jpg")
            await app.bot.send_message(
                chat_id=chat_id,
                text=f"🚨 *{raion}* — *{ALERT_TYPES_UA.get(alert_type, alert_type)}*",
                parse_mode="Markdown",
            )

    for raion in list(cache.last_alerts.keys()):
        if raion not in new_state_kyiv and chat_id:
            await app.bot.send_message(chat_id=chat_id, text=f"✅ Відбій тривоги у *{raion}*", parse_mode="Markdown")

    if cache.last_alerts and not new_state_kyiv and chat_id:
        await app.bot.send_message(chat_id=chat_id, text=f"✅ Відбій тривоги у {REGION}")
        await send_photo_safe(app.bot, chat_id, "images/Clear.jpg")

    # ---- Сповіщення для ADMIN_ID по всій Україні
    last_global = app.bot_data.get("last_global_alerts", {})
    for key, alert_type in new_state_global.items():
        if last_global.get(key) != alert_type:
            await app.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"🚨 {key}: {ALERT_TYPES_UA.get(alert_type, alert_type)}",
            )

    for key in list(last_global.keys()):
        if key not in new_state_global:
            await app.bot.send_message(chat_id=ADMIN_ID, text=f"✅ Відбій тривоги: {key}")

    cache.last_alerts = new_state_kyiv
    app.bot_data["last_global_alerts"] = new_state_global

# ======================================================
# 🔹 Ручні запити
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

# ======================================================
# 🔹 Обробка словникових запитів
# ======================================================
async def handle_dynamic_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").lower().strip()
    if not text.startswith("що по"):
        return

    locations = context.application.bot_data.get("locations_dict", {})
    keyword = text.replace("що по", "").replace("?", "").strip().lower()

    found_region = None
    for oblast, mapping in locations.items():
        for key, region in mapping.items():
            if keyword == key:
                found_region = (oblast, region)
                break
        if found_region:
            break

    if not found_region:
        await update.message.reply_text(
            "🤔 Не знаю такого населеного пункту. Надіслати адміну для розгляду? (так/ні)"
        )
        context.user_data["pending_add"] = keyword
        return

    oblast, region = found_region
    cache: RegionAlertCache = context.application.bot_data.get("alert_cache", RegionAlertCache())
    active_alerts = cache.last_alerts or {}

    if region in active_alerts:
        await update.message.reply_text(f"🚨 В області *{oblast}* ({region}) триває тривога!", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"✅ В області *{oblast}* ({region}) все спокійно.", parse_mode="Markdown")

# ======================================================
# 🔹 Відповідь на “так/ні” після незнайомого НП
# ======================================================
async def handle_admin_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").lower().strip()
    if "pending_add" not in context.user_data:
        return

    if text == "так":
        keyword = context.user_data.pop("pending_add")
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🆕 Запит на додавання нового НП: «{keyword}».\n"
                 f"Вкажи, будь ласка, номер області для нього.",
        )
        context.application.bot_data["pending_admin_add"] = keyword
    elif text == "ні":
        await update.message.reply_text("👌 Добре, не додаємо.")
        context.user_data.pop("pending_add", None)

# ======================================================
# 🔹 Команда /export_dict — для адміністраторки
# ======================================================
async def export_dict(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔️ Недостатньо прав.")
        return
    data = context.application.bot_data.get("locations_dict", {})
    formatted = json.dumps(data, ensure_ascii=False, indent=2)
    await update.message.reply_text(f"📘 Поточний словник:\n\n<pre>{formatted}</pre>", parse_mode="HTML")

# ======================================================
# 🔹 Базові команди
# ======================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["chat_id"] = update.effective_chat.id
    await update.message.reply_text(
        "Привіт 🌸\nЯ повідомляю про тривоги у Київській області та по Україні.\n"
        "Можеш спробувати: «що по ірпеню?» або «що по житомиру?»"
    )

async def stopbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔️ Команда доступна лише адміністраторці.")
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
    app.add_handler(CommandHandler("export_dict", export_dict))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)^що по "), handle_dynamic_query))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)^(так|ні)$"), handle_admin_confirmation))

    async def _poll(context: ContextTypes.DEFAULT_TYPE):
        await process_alerts(context.application, cache)

    app.job_queue.run_repeating(_poll, interval=POLL_INTERVAL, first=0)
    app.job_queue.start()

    logging.info("✅ Бот запущено й готовий до роботи.")
    await app.run_polling(close_loop=False)

# ======================================================
# 🔹 Запускаємо
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
