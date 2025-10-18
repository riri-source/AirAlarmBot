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

# ======================= ENV / SETUP =======================
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

# Healthcheck HTTP (якщо потрібно)
class StubHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
def run_http_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), StubHandler).serve_forever()
Thread(target=run_http_server, daemon=True).start()

# ======================= DATA / HELPERS =======================
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

def norm(s: str) -> str:
    if not s: return ""
    s = s.lower().strip()
    for bad in ["’","'","–","—","‐","-",".",","]: s = s.replace(bad, " ")
    s = s.replace("м .","м.").replace("м. "," ").replace("м "," ")
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

def get_chat_id(app) -> Optional[int]:
    return app.bot_data.get("chat_id") or app.bot_data.get("default_chat_id")

async def send_photo_safe(bot, chat_id: Optional[int], image_path: str):
    if not chat_id: return
    try:
        with open(image_path, "rb") as ph:
            await bot.send_photo(chat_id=chat_id, photo=ph)
    except Exception:
        pass

async def _get_api_data():
    headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(API_URL, headers=headers, timeout=10) as resp:
            return await resp.json()

# ======================= MONITORING =======================
async def process_alerts(app, cache: RegionAlertCache):
    data = await _get_api_data()
    alerts = data.get("alerts", [])
    chat_id = get_chat_id(app)

    # Київщина + Київ для групи
    relevant_kyiv = [a for a in alerts if a.get("location_oblast") in {"Київська область", "м. Київ"}]
    new_state_kyiv = {a["location_title"]: a["alert_type"] for a in relevant_kyiv}

    # Глобально для адміну
    new_state_global = {f"{a['location_oblast']} — {a['location_title']}": a["alert_type"] for a in alerts}

    if not cache.initialized:
        cache.last_alerts = new_state_kyiv
        cache.initialized = True
        app.bot_data["last_global_alerts"] = new_state_global
        return

    # Київщина → група
    for r, t in new_state_kyiv.items():
        if cache.last_alerts.get(r) != t and chat_id:
            await send_photo_safe(app.bot, chat_id, "images/Alarm.jpg")
            await app.bot.send_message(chat_id=chat_id,
                text=f"🚨 *{r}* — *{ALERT_TYPES_UA.get(t, t)}*", parse_mode="Markdown")
    for r in list(cache.last_alerts.keys()):
        if r not in new_state_kyiv and chat_id:
            await app.bot.send_message(chat_id=chat_id,
                text=f"✅ Відбій тривоги у *{r}*", parse_mode="Markdown")
    if cache.last_alerts and not new_state_kyiv and chat_id:
        await app.bot.send_message(chat_id=chat_id, text=f"✅ Відбій тривоги у {REGION}")
        await send_photo_safe(app.bot, chat_id, "images/Clear.jpg")

    # Україна → адміну
    last_global = app.bot_data.get("last_global_alerts", {})
    for key, t in new_state_global.items():
        if last_global.get(key) != t:
            await app.bot.send_message(chat_id=ADMIN_ID, text=f"🚨 {key}: {ALERT_TYPES_UA.get(t, t)}")
    for key in list(last_global.keys()):
        if key not in new_state_global:
            await app.bot.send_message(chat_id=ADMIN_ID, text=f"✅ Відбій тривоги: {key}")

    cache.last_alerts = new_state_kyiv
    app.bot_data["last_global_alerts"] = new_state_global

# ======================= MANUAL QUERIES =======================
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

# ======================= DICTIONARY QUERIES =======================
async def handle_dynamic_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").lower().strip()
    # не чіпати специфічні фрази
    special = ["що по області","що по києву","як там крим","що по одес","що по луган","що по франик","що по черніг"]
    if any(x in text for x in special): return
    if not text.startswith("що по"): return

    kw_raw = text.replace("що по", "", 1).strip().rstrip("?!,. ")
    kw = norm(kw_raw)
    locations = context.application.bot_data.get("locations_dict", {})

    found_oblast, found_region = None, None
    # точний
    for oblast, mapping in locations.items():
        for k, region in mapping.items():
            if kw == norm(k):
                found_oblast, found_region = oblast, region; break
        if found_oblast: break
    # частковий
    if not found_oblast:
        for oblast, mapping in locations.items():
            for k, region in mapping.items():
                nk = norm(k)
                if kw in nk or nk in kw:
                    found_oblast, found_region = oblast, region; break
            if found_oblast: break

    if not found_oblast:
        # запускаємо флоу підтвердження користувача
        context.user_data["pending_add"] = kw_raw
        await update.message.reply_text("🤔 Не знаю такого населеного пункту. Надіслати адміну для розгляду? (так/ні)")
        return

    # відповіді
    cache: RegionAlertCache = context.application.bot_data.get("alert_cache", RegionAlertCache())
    active = cache.last_alerts or {}
    if found_oblast in {"Київська область", "м. Київ"}:
        is_active = found_region in active
        msg = f"🚨 В області *{found_oblast}* ({found_region}) триває тривога!" if is_active else f"✅ В області *{found_oblast}* ({found_region}) все тихо!"
    else:
        is_active = await region_status_contains(norm(found_oblast))
        msg = f"🚨 В області *{found_oblast}* триває тривога!" if is_active else f"✅ В області *{found_oblast}* все тихо!"
    await update.message.reply_text(msg, parse_mode="Markdown")

# ===== “так/ні” від користувача → запит адміну =====
async def handle_user_yes_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip().lower()
    if txt not in {"так","ні"}: return
    if "pending_add" not in context.user_data: return

    if txt == "ні":
        context.user_data.pop("pending_add", None)
        await update.message.reply_text("👌 Добре, не додаємо.")
        return

    # “так”: шлемо адміну список областей і зберігаємо стан
    kw = context.user_data.pop("pending_add")
    app_data = context.application.bot_data
    app_data["pending_kw"] = kw
    app_data["awaiting_oblast_choice"] = True

    locs = app_data.get("locations_dict", {})
    oblasts = list(locs.keys())
    msg = f"📍 Вкажи номер області для «{kw}»:\n\n"
    for i, o in enumerate(oblasts, 1): msg += f"{i}. {o}\n"
    await context.bot.send_message(chat_id=ADMIN_ID, text=msg)

# ===== універсальний числовий вибір адміну =====
async def handle_admin_number_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    t = (update.message.text or "").strip()
    if not t.isdigit(): return
    idx = int(t) - 1
    app_data = context.application.bot_data

    # КРОК 2: район Київщини
    if app_data.get("awaiting_kyiv_region_choice"):
        if idx < 0 or idx >= len(KYIV_REGIONS):
            await update.message.reply_text("❌ Недійсний номер району."); return
        region = KYIV_REGIONS[idx]
        kw = app_data.pop("pending_region_add", None)
        if not kw:
            await update.message.reply_text("⚠️ Немає збереженого ключового слова."); return
        loc = app_data.get("locations_dict", {})
        loc.setdefault("Київська область", {})[kw.lower()] = region
        save_locations_dict(loc)
        app_data["locations_dict"] = load_locations_dict()
        app_data.pop("awaiting_kyiv_region_choice", None)
        await update.message.reply_text(f"✅ Додано «{kw}» до {region} Київської області.")
        return

    # КРОК 1: область
    if app_data.get("awaiting_oblast_choice"):
        locs = app_data.get("locations_dict", {})
        oblasts = list(locs.keys())
        if idx < 0 or idx >= len(oblasts):
            await update.message.reply_text("❌ Недійсний номер області."); return
        chosen = oblasts[idx]
        kw = app_data.pop("pending_kw", None)
        app_data.pop("awaiting_oblast_choice", None)
        if not kw:
            await update.message.reply_text("⚠️ Немає збереженого ключового слова."); return

        if chosen == "Київська область":
            msg = "🏙 Обрано Київщину. Вибери район:\n\n"
            for i, r in enumerate(KYIV_REGIONS, 1): msg += f"{i}. {r}\n"
            await update.message.reply_text(msg)
            app_data["awaiting_kyiv_region_choice"] = True
            app_data["pending_region_add"] = kw
            return

        # інша область — кладемо ключ у групу області (значення: назва області)
        locs.setdefault(chosen, {})[kw.lower()] = chosen
        save_locations_dict(locs)
        app_data["locations_dict"] = load_locations_dict()
        await update.message.reply_text(f"✅ Додано «{kw}» до області {chosen}.")
        return

# ======================= EXPORT / LIST / START / STOP =======================
async def export_dict(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔️ Недостатньо прав."); return
    data = context.application.bot_data.get("locations_dict", {})
    await update.message.reply_text(f"<pre>{json.dumps(data, ensure_ascii=False, indent=2)}</pre>", parse_mode="HTML")

async def list_regions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Отримую список областей...")
    data = await _get_api_data()
    regions = sorted(set(a["location_oblast"] for a in data.get("alerts", []) if a.get("location_oblast")))
    if regions:
        await update.message.reply_text("🧭 Список областей, які бачить API:\n\n" + "\n".join(f"• {r}" for r in regions))
    else:
        await update.message.reply_text("❌ API не повернуло список областей.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["chat_id"] = update.effective_chat.id
    await update.message.reply_text("Привіт 🌸 Бот працює. Отримую поточні тривоги...")

    # при старті — зведення адміну по всій Україні
    data = await _get_api_data()
    alerts = data.get("alerts", [])
    if not alerts:
        await context.bot.send_message(chat_id=ADMIN_ID, text="✅ Зараз в Україні все спокійно.")
    else:
        lines = [f"🚨 {a['location_oblast']} — {a['location_title']}: {ALERT_TYPES_UA.get(a['alert_type'],'')}" for a in alerts]
        await context.bot.send_message(chat_id=ADMIN_ID, text="🗺 Актуальні тривоги:\n" + "\n".join(lines))

async def stopbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔️ Команда лише для адміністратора."); return
    await update.message.reply_text("🛑 Зупиняю роботу...")
    try:
        context.application.job_queue.stop()
        await context.application.stop()
        await context.application.shutdown()
        asyncio.get_event_loop().stop()
    except Exception as e:
        logging.error(f"Помилка при зупинці: {e}")

async def error_handler(update, context):
    logging.error("Помилка:", exc_info=context.error)

# ======================= MAIN =======================
async def main():
    nest_asyncio.apply()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    if DEFAULT_CHAT_ID:
        app.bot_data["chat_id"] = DEFAULT_CHAT_ID
        app.bot_data["default_chat_id"] = DEFAULT_CHAT_ID

    cache = RegionAlertCache()
    app.bot_data["alert_cache"] = cache
    app.bot_data["locations_dict"] = load_locations_dict()

    # команди
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stopbot", stopbot))
    app.add_handler(CommandHandler("list_regions", list_regions))
    app.add_handler(CommandHandler("export_dict", export_dict))

    # ручні запити-ярлики
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по області"), oblast_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по києву"), kyiv_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)як там крим"), krym_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по одес"), odesa_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по луган"), lugansk_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по черніг"), chernihiv_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по франик|що по івано-франківськ"), frankivsk_alerts))

    # словниковий запит
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)^що по "), handle_dynamic_query))
    # підтвердження користувача
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)^(так|ні)$"), handle_user_yes_no))
    # один універсальний числовий вибір для адміну
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^[0-9]+$"), handle_admin_number_choice))

    # моніторинг
    async def _poll(ctx: ContextTypes.DEFAULT_TYPE):
        await process_alerts(ctx.application, cache)

    app.job_queue.run_repeating(_poll, interval=POLL_INTERVAL, first=0)
    app.add_error_handler(error_handler)
    await app.run_polling(close_loop=False)

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(main())
    loop.run_forever()
