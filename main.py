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
# 🔹 Healthcheck-сервер
# ======================================================
class StubHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_http_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), StubHandler).serve_forever()
Thread(target=run_http_server, daemon=True).start()

# ======================================================
# 🔹 Класи і константи
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

KYIV_REGIONS = [
    "Бучанський район", "Вишгородський район", "Фастівський район",
    "Обухівський район", "Білоцерківський район", "Бориспільський район",
    "Броварський район", "м. Київ"
]

# ======================================================
# 🔹 Хелпери
# ======================================================
def norm(s: str) -> str:
    if not s: return ""
    s = s.lower().strip()
    for bad in ["’","'","–","—","‐","-",".",","]: s = s.replace(bad, " ")
    return " ".join(s.split())

def _dict_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "locations_dict.json")

def load_locations_dict() -> Dict:
    path = _dict_path()
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f: json.dump({}, f, ensure_ascii=False, indent=2)
    with open(path, "r", encoding="utf-8") as f: return json.load(f)

def save_locations_dict(data: Dict):
    with open(_dict_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

async def _get_api_data():
    headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
    async with aiohttp.ClientSession() as s:
        async with s.get(API_URL, headers=headers, timeout=10) as r:
            return await r.json()

async def send_photo_safe(bot, chat_id: Optional[int], image_path: str):
    if not chat_id: return
    try:
        with open(image_path, "rb") as ph:
            await bot.send_photo(chat_id=chat_id, photo=ph)
    except Exception:
        pass

# ======================================================
# 🔹 Моніторинг
# ======================================================
async def process_alerts(app, cache: RegionAlertCache):
    data = await _get_api_data()
    alerts = data.get("alerts", [])
    chat_id = app.bot_data.get("chat_id")

    relevant_kyiv = [a for a in alerts if a.get("location_oblast") in {"Київська область", "м. Київ"}]
    new_state_kyiv = {a["location_title"]: a["alert_type"] for a in relevant_kyiv}
    new_state_global = {f"{a['location_oblast']} — {a['location_title']}": a["alert_type"] for a in alerts}

    if not cache.initialized:
        cache.last_alerts = new_state_kyiv
        app.bot_data["last_global_alerts"] = new_state_global
        cache.initialized = True
        return

    # Київщина → група
    for r, t in new_state_kyiv.items():
        if cache.last_alerts.get(r) != t and chat_id:
            await send_photo_safe(app.bot, chat_id, "images/Alarm.jpg")
            await app.bot.send_message(chat_id=chat_id,
                text=f"🚨 *{r}* — *{ALERT_TYPES_UA.get(t, t)}*", parse_mode="Markdown")
    for r in list(cache.last_alerts.keys()):
        if r not in new_state_kyiv and chat_id:
            await app.bot.send_message(chat_id=chat_id, text=f"✅ Відбій тривоги у *{r}*", parse_mode="Markdown")
    if cache.last_alerts and not new_state_kyiv and chat_id:
        await app.bot.send_message(chat_id=chat_id, text=f"✅ Відбій тривоги у {REGION}")
        await send_photo_safe(app.bot, chat_id, "images/Clear.jpg")

    # Глобально → адміну
    last_global = app.bot_data.get("last_global_alerts", {})
    for key, t in new_state_global.items():
        if last_global.get(key) != t:
            await app.bot.send_message(chat_id=ADMIN_ID, text=f"🚨 {key}: {ALERT_TYPES_UA.get(t, t)}")
    for key in list(last_global.keys()):
        if key not in new_state_global:
            await app.bot.send_message(chat_id=ADMIN_ID, text=f"✅ Відбій тривоги: {key}")

    cache.last_alerts = new_state_kyiv
    app.bot_data["last_global_alerts"] = new_state_global

# ======================================================
# 🔹 Регіональні запити
# ======================================================
async def region_status_contains(keyword: str) -> bool:
    data = await _get_api_data()
    kw = keyword.lower()
    for a in data.get("alerts", []):
        if a.get("finished_at") is None:
            if kw in (a.get("location_oblast") or "").lower() or kw in (a.get("location_title") or "").lower():
                return True
    return False

async def _region_reply(update, kw, label):
    if await region_status_contains(kw):
        await update.message.reply_text(f"🚨 {label} триває тривога!")
    else:
        await update.message.reply_text(f"✅ {label} зараз все чисто.")

async def krym_alerts(u,c):       await _region_reply(u, "крим", "У Криму")
async def odesa_alerts(u,c):      await _region_reply(u, "одес", "В Одеській області")
async def oblast_alerts(u,c):     await _region_reply(u, "київська", "У Київській області")
async def kyiv_alerts(u,c):       await _region_reply(u, "київ", "У Києві")
async def lugansk_alerts(u,c):    await _region_reply(u, "луган", "У Луганській області")
async def chernihiv_alerts(u,c):  await _region_reply(u, "черніг", "У Чернігівській області")
async def frankivsk_alerts(u,c):  await _region_reply(u, "франк", "В Івано-Франківській області")

# ======================================================
# 🔹 /help команда
# ======================================================
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🧭 *Команди KytsjaAlarm Bot*\n\n"
        "📍 *Основні:*\n"
        "/start — запустити бота або перевірити стан\n"
        "/help — показати цей список команд\n"
        "/stopbot — зупинити бота (адміністратор)\n\n"
        "📡 *Моніторинг і запити:*\n"
        "/list_regions — показати області, які бачить API\n"
        "/export_dict — показати поточний словник назв (адміністратор)\n\n"
        "🗺 *Текстові запити:*\n"
        "«що по області» — Київська область\n"
        "«що по Києву» — м. Київ\n"
        "«як там Крим?» — Крим\n"
        "«що по Франику» — Івано-Франківська область\n"
        "«що по <назві>» — будь-який населений пункт зі словника\n\n"
        "📩 Якщо боту невідомий пункт — він запитає, чи надіслати адміну для додавання."
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

# ======================================================
# 🔹 Решта команд
# ======================================================
async def export_dict(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    data = ctx.application.bot_data.get("locations_dict", {})
    await update.message.reply_text(f"<pre>{json.dumps(data, ensure_ascii=False, indent=2)}</pre>", parse_mode="HTML")

async def list_regions(update, ctx):
    await update.message.reply_text("⏳ Отримую список областей...")
    data = await _get_api_data()
    regs = sorted(set(a["location_oblast"] for a in data.get("alerts", []) if a.get("location_oblast")))
    txt = "🧭 Список областей, які бачить API:\n\n" + "\n".join(f"• {r}" for r in regs) if regs else "❌ API не повернуло даних."
    await update.message.reply_text(txt)

async def start(update, ctx):
    ctx.application.bot_data["chat_id"] = update.effective_chat.id
    await update.message.reply_text("Привіт 🌸 Бот працює. Отримую поточні тривоги...")
    data = await _get_api_data()
    alerts = data.get("alerts", [])
    if not alerts:
        await ctx.bot.send_message(chat_id=ADMIN_ID, text="✅ В Україні все спокійно.")
    else:
        lines = [f"🚨 {a['location_oblast']} — {a['location_title']}: {ALERT_TYPES_UA.get(a['alert_type'],'')}" for a in alerts]
        await ctx.bot.send_message(chat_id=ADMIN_ID, text="🗺 Актуальні тривоги:\n" + "\n".join(lines))

async def stopbot(update, ctx):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔️ Лише адміністратор.")
        return
    await update.message.reply_text("🛑 Зупиняю роботу...")
    try:
        ctx.application.job_queue.stop()
        await ctx.application.shutdown()
        await ctx.application.stop()
        asyncio.get_event_loop().stop()
    except Exception as e:
        logging.error(f"Помилка при зупинці: {e}")

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

    # Команди
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stopbot", stopbot))
    app.add_handler(CommandHandler("list_regions", list_regions))
    app.add_handler(CommandHandler("export_dict", export_dict))

    # Регіональні запити
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по області"), oblast_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по києву"), kyiv_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)як там крим"), krym_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по одес"), odesa_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по луган"), lugansk_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по черніг"), chernihiv_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по франик|що по івано-франківськ"), frankivsk_alerts))

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
