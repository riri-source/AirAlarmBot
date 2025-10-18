import os
import asyncio
import logging
import nest_asyncio
import time
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

# ===== Ініціалізація =====
load_dotenv()
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ALERTS_TOKEN = os.getenv("ALERTS_TOKEN")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 25))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHAT_ID_ENV = os.getenv("CHAT_ID")
DEFAULT_CHAT_ID = int(CHAT_ID_ENV) if CHAT_ID_ENV else None
API_URL = "https://api.alerts.in.ua/v1/alerts/active.json"

if not BOT_TOKEN or not ALERTS_TOKEN:
    raise RuntimeError("❌ Відсутні BOT_TOKEN або ALERTS_TOKEN")

# ===== Healthcheck HTTP =====
class StubHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_http_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), StubHandler).serve_forever()

Thread(target=run_http_server, daemon=True).start()

# ===== Структури =====
ALERT_TYPES_UA = {
    "air_raid": "Повітряна тривога!",
    "chemical": "Хімічна тривога",
    "radiation": "Радіаційна тривога",
    "other": "Інша тривога",
}

@dataclass
class RegionAlertCache:
    last_alerts: Dict[str, str] = field(default_factory=dict)
    initialized: bool = False

# ===== Хелпери =====
def get_chat_id(app) -> Optional[int]:
    cid = app.bot_data.get("chat_id") or app.bot_data.get("default_chat_id")
    return int(cid) if cid else None

async def send_photo_safe(bot, chat_id, path):
    if not chat_id:
        return False
    try:
        with open(path, "rb") as photo:
            await bot.send_photo(chat_id=chat_id, photo=photo)
        return True
    except FileNotFoundError:
        logging.warning(f"Нема файлу {path}")
    except Exception as e:
        logging.debug(f"send_photo_safe помилка: {e}")
    return False

# ====================================================
# 🟢  Функції для API
# ====================================================

async def fetch_region_alerts():
    """Моніторинг Київської області + м. Київ."""
    headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(API_URL, headers=headers, timeout=10, params={"_": int(time.time())}) as resp:
                data = await resp.json()
    except Exception as e:
        logging.error(f"API (МРЧ) помилка: {e}")
        return []

    alerts = [
        a for a in data.get("alerts", [])
        if a.get("location_oblast") in ("Київська область", "м. Київ")
    ]
    logging.info(f"МРЧ: {len(alerts)} запис(ів) для Київської області/м.Київ")
    return alerts


async def fetch_location_alerts(location_name):
    """Точний запит користувача (будь-яке місто/область)."""
    headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(API_URL, headers=headers, timeout=10, params={"_": int(time.time())}) as resp:
                data = await resp.json()
    except Exception as e:
        logging.error(f"API (ручний запит) помилка: {e}")
        return []

    alerts = []
    for a in data.get("alerts", []):
        if a.get("location_title") == location_name or a.get("location_oblast") == location_name:
            alerts.append(a)
    return alerts

# ====================================================
# 🟣  Telegram хендлери
# ====================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    context.application.bot_data["chat_id"] = cid
    await update.message.reply_text(
        "Привіт 🌸\n"
        "Бот моніторить Київську область та м.Київ у реальному часі.\n"
        "Напиши: «що по області», «як там Крим», «що по Одесі» — щоб дізнатись вручну."
    )

async def oblast_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alerts = await fetch_location_alerts("Київська область")
    if not alerts:
        await update.message.reply_text("✅ Київська область — зараз все чисто!")
        await send_photo_safe(context.application.bot, update.effective_chat.id, "images/Saefty.jpg")
        return
    text = "🚨 *Активні тривоги у Київській області:*\n"
    for a in alerts:
        text += f"• {a.get('location_title','—')} — {ALERT_TYPES_UA.get(a.get('alert_type'),'тривога')}\n"
    await update.message.reply_markdown(text)

async def city_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE, name, label):
    alerts = await fetch_location_alerts(name)
    if not alerts:
        await update.message.reply_text(f"✅ У {label} зараз все чисто!")
        await send_photo_safe(context.application.bot, update.effective_chat.id, "images/Saefty.jpg")
        return
    text = f"🚨 У {label} зафіксована тривога!\n"
    for a in alerts:
        text += f"• {a.get('location_title','—')} — {ALERT_TYPES_UA.get(a.get('alert_type'),'тривога')}\n"
    await update.message.reply_text(text)

async def krym_alerts(update, context): await city_alerts(update, context, "Автономна Республіка Крим", "Крим")
async def kyiv_alerts(update, context): await city_alerts(update, context, "м. Київ", "Київ")
async def odesa_alerts(update, context): await city_alerts(update, context, "м. Одеса", "Одеса")
async def frankivsk_alerts(update, context): await city_alerts(update, context, "м. Івано-Франківськ", "Івано-Франківськ")

# ====================================================
# 🟠  Моніторинг реального часу (МРЧ)
# ====================================================

async def process_alerts(app, cache: RegionAlertCache):
    tick = datetime.now().strftime("%H:%M:%S")
    logging.info(f"⏱ МРЧ перевірка @ {tick}")
    alerts = await fetch_region_alerts()
    new_state = {a.get("location_title"): a.get("alert_type") for a in alerts}

    if not cache.initialized:
        cache.last_alerts = new_state
        cache.initialized = True
        return

    chat_id = get_chat_id(app)
    if not chat_id:
        logging.info("МРЧ: CHAT_ID не задано")
        return

    started = [(r, ALERT_TYPES_UA.get(t, t)) for r, t in new_state.items() if cache.last_alerts.get(r) != t]
    ended = [r for r in cache.last_alerts.keys() if r not in new_state]

    if not started and not ended:
        cache.last_alerts = new_state
        return

    if started:
        await send_photo_safe(app.bot, chat_id, "images/Alarm.jpg")
    lines = []
    if started:
        lines.append("🚨 *Нові тривоги:*")
        for r, t in started: lines.append(f"• *{r}* — {t}")
    if ended:
        lines.append("")
        lines.append("✅ *Відбій у:*")
        for r in ended: lines.append(f"• {r}")
    await app.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown")

    cache.last_alerts = new_state

# ====================================================
# 🛠  Службові команди
# ====================================================

async def status(update, context):
    cache: RegionAlertCache = context.application.bot_data.get("alert_cache") or RegionAlertCache()
    keys = ", ".join(cache.last_alerts.keys()) or "—"
    cid = get_chat_id(context.application)
    await update.message.reply_text(
        f"ℹ️ МРЧ-зона: Київська область + м.Київ\n"
        f"CHAT_ID: {cid or 'нема'}\n"
        f"POLL_INTERVAL: {POLL_INTERVAL}s\n"
        f"Райони: {keys}"
    )

async def stopbot(update, context):
    if update.effective_user.id != ADMIN_ID:
        return await update.message.reply_text("⛔️ Лише адміністратор.")
    await update.message.reply_text("🛑 Вимикаю бота...")
    asyncio.create_task(_shutdown_sequence(context.application))

async def _shutdown_sequence(app):
    logging.info("🔻 Зупинка МРЧ")
    try: app.job_queue.stop()
    except: pass
    try: await app.shutdown()
    except: pass
    try: await app.stop()
    except: pass
    asyncio.get_event_loop().stop()

# ====================================================
# 🔹  Основний цикл
# ====================================================

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
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("stopbot", stopbot))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по області"), oblast_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)як там крим"), krym_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по києву"), kyiv_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по одесі"), odesa_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по франику"), frankivsk_alerts))

    # МРЧ JobQueue
    async def _job(context: ContextTypes.DEFAULT_TYPE):
        await process_alerts(context.application, cache)

    app.job_queue.run_repeating(_job, interval=POLL_INTERVAL, first=0)

    logging.info("✅ Бот запущено (МРЧ Київ + область)...")
    await app.run_polling(close_loop=False)

# ====================================================
# 🚀  Запуск
# ====================================================

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.create_task(main())
        loop.run_forever()
    except KeyboardInterrupt:
        logging.info("🛑 Зупинка вручну")
    finally:
        for t in asyncio.all_tasks():
            t.cancel()
        loop.close()
        logging.info("Loop closed. Exit.")
