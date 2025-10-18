import os
import asyncio
import logging
import json
from dataclasses import dataclass, field
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, Optional
from datetime import datetime
import aiohttp
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
import nest_asyncio

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
# 🔹 Основні класи та допоміжні функції
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
# 🔹 Завантаження та оновлення словника
# ======================================================
def load_locations_dict() -> Dict:
    file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locations_dict.json")
    if not os.path.exists(file_path):
        logging.warning("⚠️ Словник не знайдено, створюю порожній файл.")
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump({"Київська область": {}}, f, ensure_ascii=False, indent=2)
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_locations_dict(data: Dict):
    file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locations_dict.json")
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ======================================================
# 🔹 Моніторинг Київщини (МРЧ)
# ======================================================
async def process_alerts(app, cache: RegionAlertCache):
    data = await _get_api_data()
    alerts = data.get("alerts", [])
    relevant = [a for a in alerts if a.get("location_oblast") in {"Київська область", "м. Київ"}]
    new_state = {a["location_title"]: a["alert_type"] for a in relevant}
    chat_id = get_chat_id(app)
    logging.info(f"⏱ Перевірка Київська область @ {datetime.now().strftime('%H:%M:%S')}")

    if not cache.initialized:
        cache.last_alerts = new_state
        cache.initialized = True
        return

    # нові або змінені
    for raion, alert_type in new_state.items():
        if cache.last_alerts.get(raion) != alert_type and chat_id:
            await send_photo_safe(app.bot, chat_id, "images/Alarm.jpg")
            await app.bot.send_message(
                chat_id=chat_id,
                text=f"🚨 *{raion}* — *{ALERT_TYPES_UA.get(alert_type, alert_type)}*",
                parse_mode="Markdown",
            )

    # відбої
    for raion in list(cache.last_alerts.keys()):
        if raion not in new_state and chat_id:
            await app.bot.send_message(chat_id=chat_id, text=f"✅ Відбій тривоги у *{raion}*", parse_mode="Markdown")

    # загальний відбій
    if cache.last_alerts and not new_state and chat_id:
        await app.bot.send_message(chat_id=chat_id, text=f"✅ Відбій тривоги у {REGION}")
        await send_photo_safe(app.bot, chat_id, "images/Clear.jpg")

    cache.last_alerts = new_state

# ======================================================
# 🔹 Хендлер словникових запитів
# ======================================================
async def handle_dynamic_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").lower().strip()
    if not text.startswith("що по"):
        return

    locations = context.application.bot_data.get("locations_dict", {}).get("Київська область", {})
    keyword = text.replace("що по", "").replace("?", "").strip().lower()

    region = None
    for key, val in locations.items():
        if keyword in key.lower():
            region = val
            break

    if not region:
        # надсилаємо користувачу питання
        markup = ReplyKeyboardMarkup([[KeyboardButton("Так"), KeyboardButton("Ні")]], one_time_keyboard=True, resize_keyboard=True)
        await update.message.reply_text(
            "🤔 Не знаю такого населеного пункту. Надіслати на розгляд адміну групи щоб додати? (так/ні)",
            reply_markup=markup,
        )
        context.user_data["pending_location"] = keyword
        return

    cache: RegionAlertCache = context.application.bot_data.get("alert_cache", RegionAlertCache())
    active_alerts = cache.last_alerts or {}
    if region in active_alerts:
        await update.message.reply_text(f"🚨 У {region} триває тривога!")
    else:
        await update.message.reply_text(f"✅ У {region} зараз все спокійно.")

# ======================================================
# 🔹 Обробка відповіді користувача “Так/Ні”
# ======================================================
async def user_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.lower() not in ["так", "ні"]:
        return
    choice = update.message.text.lower()
    keyword = context.user_data.get("pending_location")
    if not keyword:
        return
    if choice == "ні":
        await update.message.reply_text("🙂 Добре, не надсилатиму адміну.")
        context.user_data.pop("pending_location", None)
        return
    # якщо "так" — надсилаємо адміну
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"📩 Новий населений пункт запропоновано користувачем:\n<b>{keyword.title()}</b>\nДодати до словника?",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("✅ Додати"), KeyboardButton("❌ Ігнорувати")]], resize_keyboard=True),
    )
    await update.message.reply_text("✅ Надіслано адміну на розгляд.")
    context.user_data.pop("pending_location", None)

# ======================================================
# 🔹 Адмін-підтвердження
# ======================================================
async def admin_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    text = update.message.text.strip()
    if text not in ["✅ Додати", "❌ Ігнорувати"]:
        return
    if text == "❌ Ігнорувати":
        await update.message.reply_text("🚫 Пропозицію відхилено.")
        return

    # якщо “Додати” — просимо вибрати область
    regions = [
        "Київська область", "Львівська область", "Чернігівська область",
        "Донецька область", "Запорізька область", "Харківська область",
    ]
    markup = ReplyKeyboardMarkup([[KeyboardButton(r)] for r in regions], resize_keyboard=True)
    await update.message.reply_text("🌍 Оберіть область для нового населеного пункту:", reply_markup=markup)
    context.user_data["awaiting_region_selection"] = True

# ======================================================
# 🔹 Обробка вибору області
# ======================================================
async def region_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    region = update.message.text.strip()
    if not context.user_data.get("awaiting_region_selection"):
        return
    context.user_data["awaiting_region_selection"] = False

    if region == "Київська область":
        subregions = [
            "Вишгородський район", "Бучанський район", "Фастівський район",
            "Броварський район", "Бориспільський район",
            "Обухівський район", "Білоцерківський район"
        ]
        markup = ReplyKeyboardMarkup([[KeyboardButton(r)] for r in subregions], resize_keyboard=True)
        await update.message.reply_text("🏞️ Оберіть район:", reply_markup=markup)
        context.user_data["awaiting_subregion_selection"] = True
        return

    # якщо область не Київська
    locations = load_locations_dict()
    keyword = "новий населений пункт"
    locations.setdefault(region, {})
    locations[region][keyword] = region
    save_locations_dict(locations)
    await update.message.reply_text(f"✅ Додано новий пункт у {region}.")
    context.application.bot_data["locations_dict"] = locations

# ======================================================
# 🔹 Основні команди
# ======================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["chat_id"] = update.effective_chat.id
    await update.message.reply_text("Привіт 🌸 Я повідомляю про тривоги у Київській області.\n"
                                    "Можеш спробувати: «що по ірпеню?» або «що по борисполю?»")

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
    app.bot_data["locations_dict"] = load_locations_dict()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)^що по "), handle_dynamic_query))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)^(так|ні)$"), user_response))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)^✅ Додати|❌ Ігнорувати$"), admin_choice))
    app.add_handler(MessageHandler(filters.TEXT, region_selected))

    async def _poll(context: ContextTypes.DEFAULT_TYPE):
        await process_alerts(context.application, cache)

    app.job_queue.run_repeating(_poll, interval=POLL_INTERVAL, first=0)
    app.job_queue.start()

    logging.info("✅ KytsjaAlarm v7 Adaptive готовий до роботи.")
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
        logging.info("🛑 KytsjaAlarm завершив роботу.")
