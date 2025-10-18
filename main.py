import os
import asyncio
import json
import logging
import nest_asyncio
from dataclasses import dataclass, field
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime
from typing import Dict, Optional
import aiohttp
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
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
    raise RuntimeError("❌ BOT_TOKEN або ALERTS_TOKEN не задано")

# ======================================================
# 🔹 Локальний HTTP-сервер для healthcheck
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
# 🔹 Основні класи та хелпери
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
    return app.bot_data.get("chat_id") or app.bot_data.get("default_chat_id")

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
# 🔹 Завантаження словника
# ======================================================
def load_locations_dict() -> Dict:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locations_dict.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"❌ Помилка словника: {e}")
        return {}

def save_locations_dict(data: Dict):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locations_dict.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ======================================================
# 🔹 Моніторинг Київщини
# ======================================================
async def process_alerts(app, cache: RegionAlertCache):
    data = await _get_api_data()
    alerts = data.get("alerts", [])
    relevant = [a for a in alerts if a.get("location_oblast") in {"Київська область", "м. Київ"}]
    new_state = {a["location_title"]: a["alert_type"] for a in relevant}
    chat_id = get_chat_id(app)

    if not cache.initialized:
        cache.last_alerts = new_state
        cache.initialized = True
        return

    # нові тривоги
    for raion, alert_type in new_state.items():
        if cache.last_alerts.get(raion) != alert_type and chat_id:
            await send_photo_safe(app.bot, chat_id, "images/Alarm.jpg")
            await app.bot.send_message(chat_id=chat_id,
                text=f"🚨 *{raion}* — *{ALERT_TYPES_UA.get(alert_type, alert_type)}*",
                parse_mode="Markdown")

    # відбої
    for raion in list(cache.last_alerts.keys()):
        if raion not in new_state and chat_id:
            await app.bot.send_message(chat_id=chat_id,
                text=f"✅ Відбій тривоги у *{raion}*", parse_mode="Markdown")

    # загальний відбій
    if cache.last_alerts and not new_state and chat_id:
        await app.bot.send_message(chat_id=chat_id,
            text=f"✅ Відбій тривоги у {REGION}")
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
            if kw in (a.get("location_oblast") or "").lower() or kw in (a.get("location_title") or "").lower():
                return True
    return False

async def krym_alerts(update, ctx): await _region_reply(update, "крим", "У Криму")
async def odesa_alerts(update, ctx): await _region_reply(update, "одес", "В Одеській області")
async def oblast_alerts(update, ctx): await _region_reply(update, "київська", "У Київській області")
async def kyiv_alerts(update, ctx): await _region_reply(update, "київ", "У Києві")
async def lugansk_alerts(update, ctx): await _region_reply(update, "луган", "У Луганській області")
async def chernihiv_alerts(update, ctx): await _region_reply(update, "черніг", "У Чернігівській області")
async def frankivsk_alerts(update, ctx): await _region_reply(update, "франк", "В Івано-Франківській області")

async def _region_reply(update, keyword, label):
    if await region_status(keyword):
        await update.message.reply_text(f"🚨 {label} триває тривога!")
    else:
        await update.message.reply_text(f"✅ {label} зараз все чисто.")

# ======================================================
# 🔹 Хендлер словникових запитів
# ======================================================
async def handle_dynamic_query(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").lower().strip()
    if any(x in text for x in [
        "що по області","що по києву","як там крим","що по одес",
        "що по луган","що по франик","що по черніг"
    ]): return

    keyword = text.replace("що по", "").replace("?", "").strip().lower()
    locations = ctx.application.bot_data.get("locations_dict", {})

    found_region = None
    for oblast, places in locations.items():
        if keyword in places.keys():
            found_region = oblast
            break

    if not found_region:
        await update.message.reply_text(
            "🤔 Не знаю такого населеного пункту. Надіслати адміну для розгляду? (так/ні)"
        )
        ctx.application.bot_data["pending_add"] = keyword
        ctx.application.bot_data["pending_user"] = update.effective_user.id
        return

    cache: RegionAlertCache = ctx.application.bot_data.get("alert_cache", RegionAlertCache())
    active_alerts = cache.last_alerts or {}
    region_active = any(found_region in a for a in active_alerts.keys())

    if region_active:
        await update.message.reply_text(f"🚨 В області *{found_region}* триває тривога!", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"✅ В області *{found_region}* все тихо!", parse_mode="Markdown")

# ======================================================
# 🔹 Додавання нового населеного пункту
# ======================================================
async def handle_admin_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = ctx.application.bot_data
    if user_id != ADMIN_ID and data.get("pending_add"):
        if update.message.text.lower().startswith("так"):
            keyword = data.pop("pending_add")
            await ctx.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"📬 Користувач пропонує додати: *{keyword}*\n\n"
                     f"Введи номер області, куди додати.\n" +
                     "\n".join([f"{i+1}. {r}" for i, r in enumerate(data['locations_dict'].keys())]),
                parse_mode="Markdown")
        else:
            await update.message.reply_text("👌 Добре, не додаємо.")
        data.pop("pending_user", None)
        return

    # якщо відповідь від тебе — вибір області/району
    if user_id == ADMIN_ID and data.get("pending_add"):
        keyword = data.pop("pending_add")
        locations = data["locations_dict"]
        text = update.message.text.strip()
        if text.isdigit():
            idx = int(text) - 1
            oblasts = list(locations.keys())
            if 0 <= idx < len(oblasts):
                region = oblasts[idx]
                # якщо Київська — уточнення району
                if region == "Київська область":
                    await ctx.bot.send_message(chat_id=ADMIN_ID,
                        text="Вибери район:\n1. Бучанський\n2. Вишгородський\n3. Фастівський\n"
                             "4. Обухівський\n5. Білоцерківський\n6. Бориспільський\n7. Броварський")
                    data["pending_region_choice"] = (keyword, region)
                    return
                locations[region][keyword] = region
                save_locations_dict(locations)
                await ctx.bot.send_message(chat_id=ADMIN_ID, text=f"✅ Додано *{keyword}* → {region}", parse_mode="Markdown")
                data["locations_dict"] = locations
                return
        elif data.get("pending_region_choice"):
            keyword, region = data.pop("pending_region_choice")
            mapping = {
                "1":"Бучанський район","2":"Вишгородський район","3":"Фастівський район",
                "4":"Обухівський район","5":"Білоцерківський район",
                "6":"Бориспільський район","7":"Броварський район"
            }
            if text in mapping:
                locations["Київська область"][keyword] = mapping[text]
                save_locations_dict(locations)
                await ctx.bot.send_message(chat_id=ADMIN_ID,
                    text=f"✅ Додано *{keyword}* → {mapping[text]}", parse_mode="Markdown")
                data["locations_dict"] = locations

# ======================================================
# 🔹 Базові команди
# ======================================================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.application.bot_data["chat_id"] = update.effective_chat.id
    await update.message.reply_text("Привіт 🌸 Бот працює.\nОтримую поточні тривоги...")

    data = await _get_api_data()
    alerts = data.get("alerts", [])
    if not alerts:
        await ctx.bot.send_message(chat_id=ADMIN_ID, text="✅ Зараз в Україні все спокійно.")
    else:
        lines = [f"🚨 {a['location_oblast']} — {a['location_title']}: {ALERT_TYPES_UA.get(a['alert_type'],'')}" for a in alerts]
        await ctx.bot.send_message(chat_id=ADMIN_ID, text="🗺 Актуальні тривоги:\n" + "\n".join(lines))

async def stopbot(update, ctx):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔️ Команда лише для адміністратора.")
        return
    await update.message.reply_text("🛑 Зупиняю роботу...")
    asyncio.create_task(_shutdown_sequence(ctx.application))

async def _shutdown_sequence(app):
    try:
        app.job_queue.stop()
        await app.shutdown()
        await app.stop()
    except Exception:
        pass
    asyncio.get_event_loop().stop()

async def list_regions(update, ctx):
    await update.message.reply_text("⏳ Отримую список областей...")
    data = await _get_api_data()
    regions = sorted(set(a["location_oblast"] for a in data.get("alerts", []) if a.get("location_oblast")))
    if regions:
        await update.message.reply_text("🧭 Список областей, які бачить API:\n\n" + "\n".join([f"• {r}" for r in regions]))
    else:
        await update.message.reply_text("❌ API не повернуло список областей.")

async def export_dict(update, ctx):
    if update.effective_user.id != ADMIN_ID:
        return
    locations = ctx.application.bot_data.get("locations_dict", {})
    text = json.dumps(locations, ensure_ascii=False, indent=2)
    await ctx.bot.send_message(chat_id=ADMIN_ID, text=f"📄 Актуальний словник:\n\n<pre>{text}</pre>", parse_mode="HTML")

async def error_handler(update, ctx):
    logging.error("Помилка:", exc_info=ctx.error)

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
    app.add_handler(CommandHandler("stopbot", stopbot))
    app.add_handler(CommandHandler("list_regions", list_regions))
    app.add_handler(CommandHandler("export_dict", export_dict))

    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по області"), oblast_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по києву"), kyiv_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)як там крим"), krym_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по одес"), odesa_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по луган"), lugansk_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по черніг"), chernihiv_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по франик|івано-франківськ"), frankivsk_alerts))

    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)^що по "), handle_dynamic_query))
    app.add_handler(MessageHandler(filters.TEXT, handle_admin_reply))
    app.add_error_handler(error_handler)

    async def _poll(ctx: ContextTypes.DEFAULT_TYPE):
        await process_alerts(ctx.application, cache)

    app.job_queue.run_repeating(_poll, interval=POLL_INTERVAL, first=0)
    await app.run_polling(close_loop=False)

# ======================================================
# 🔹 Запуск
# ======================================================
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(main())
    loop.run_forever()
