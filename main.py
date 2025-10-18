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
# env
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
# tiny http
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
# helpers & data
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

def norm(s: str) -> str:
    if not s:
        return ""
    s = s.lower().strip()
    for bad in ["’", "'", "–", "—", "‐", "-", ".", ","]:
        s = s.replace(bad, " ")
    s = s.replace("м .", "м.").replace("м. ", " ").replace("м ", " ")
    return " ".join(s.split())

def _dict_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "locations_dict.json")

def load_locations_dict() -> Dict:
    path = _dict_path()
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_locations_dict(data: Dict):
    path = _dict_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logging.info("💾 Словник оновлено на сервері.")

async def _get_api_data():
    headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(API_URL, headers=headers, timeout=10) as resp:
            return await resp.json()

def get_chat_id(app) -> Optional[int]:
    return int(app.bot_data.get("chat_id") or app.bot_data.get("default_chat_id") or 0) or None

async def send_photo_safe(bot, chat_id: Optional[int], path: str):
    if not chat_id:
        return
    try:
        with open(path, "rb") as ph:
            await bot.send_photo(chat_id=chat_id, photo=ph)
    except Exception:
        pass

# ======================================================
# monitoring
# ======================================================
async def process_alerts(app, cache: RegionAlertCache):
    data = await _get_api_data()
    alerts = data.get("alerts", [])
    chat_id = get_chat_id(app)

    relevant_kyiv = [a for a in alerts if a.get("location_oblast") in {"Київська область", "м. Київ"}]
    new_state_kyiv = {a["location_title"]: a["alert_type"] for a in relevant_kyiv}
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
            await app.bot.send_message(
                chat_id=chat_id,
                text=f"🚨 *{r}* — *{ALERT_TYPES_UA.get(t, t)}*",
                parse_mode="Markdown",
            )
    for r in list(cache.last_alerts.keys()):
        if r not in new_state_kyiv and chat_id:
            await app.bot.send_message(chat_id=chat_id, text=f"✅ Відбій тривоги у *{r}*", parse_mode="Markdown")
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

# ======================================================
# dynamic queries
# ======================================================
async def region_status_contains(keyword: str) -> bool:
    data = await _get_api_data()
    kw = keyword.lower()
    for a in data.get("alerts", []):
        if a.get("finished_at") is None:
            if kw in (a.get("location_oblast") or "").lower() or kw in (a.get("location_title") or "").lower():
                return True
    return False

async def handle_dynamic_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").lower().strip()
    if not text.startswith("що по"):
        return
    kw_raw = text.replace("що по", "", 1).strip().rstrip("?!,. ")
    kw = norm(kw_raw)
    locations = context.application.bot_data.get("locations_dict", {})
    found_oblast, found_region = None, None

    # точний
    for oblast, mapping in locations.items():
        for k, region in mapping.items():
            if kw == norm(k):
                found_oblast, found_region = oblast, region
                break
        if found_oblast: break
    # частковий
    if not found_oblast:
        for oblast, mapping in locations.items():
            for k, region in mapping.items():
                nk = norm(k)
                if kw in nk or nk in kw:
                    found_oblast, found_region = oblast, region
                    break
            if found_oblast: break

    if not found_oblast:
        context.user_data["pending_add"] = kw_raw
        await update.message.reply_text("🤔 Не знаю такого населеного пункту. Надіслати адміну для розгляду? (так/ні)")
        return

    cache: RegionAlertCache = context.application.bot_data.get("alert_cache", RegionAlertCache())
    active = cache.last_alerts or {}

    if found_oblast in {"Київська область", "м. Київ"}:
        is_active = found_region in active
        msg = f"🚨 В області *{found_oblast}* ({found_region}) триває тривога!" if is_active else f"✅ В області *{found_oblast}* ({found_region}) все тихо!"
    else:
        is_active = await region_status_contains(norm(found_oblast))
        msg = f"🚨 В області *{found_oblast}* триває тривога!" if is_active else f"✅ В області *{found_oblast}* все тихо!"
    await update.message.reply_text(msg, parse_mode="Markdown")

# ======================================================
# admin add flow
# ======================================================
async def handle_user_yes_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip().lower()
    if txt not in {"так", "ні"} or "pending_add" not in context.user_data:
        return
    if txt == "ні":
        await update.message.reply_text("👌 Добре, не додаємо.")
        context.user_data.pop("pending_add", None)
        return
    kw = context.user_data.pop("pending_add")
    await context.bot.send_message(chat_id=ADMIN_ID, text=f"📩 Новий запит на додавання НП: «{kw}»")
    app_data = context.application.bot_data
    app_data["pending_kw"] = kw
    app_data["awaiting_oblast_choice"] = True

    # показати нумерований список областей
    locs = app_data.get("locations_dict", {})
    oblasts = list(locs.keys())
    msg = f"📍 Вкажи номер області для «{kw}»:\n\n"
    for i, o in enumerate(oblasts, 1):
        msg += f"{i}. {o}\n"
    await context.bot.send_message(chat_id=ADMIN_ID, text=msg)

async def handle_admin_number_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Єдиний числовий хендлер: спочатку перевіряє, чого чекаємо (район чи область)."""
    if update.effective_user.id != ADMIN_ID:
        return
    txt = (update.message.text or "").strip()
    if not txt.isdigit():
        return
    idx = int(txt) - 1
    app_data = context.application.bot_data

    # КРОК 2: очікуємо вибір району Київщини
    if app_data.get("awaiting_kyiv_region_choice"):
        if idx < 0 or idx >= len(KYIV_REGIONS):
            await update.message.reply_text("❌ Недійсний номер району.")
            return
        region = KYIV_REGIONS[idx]
        kw = app_data.pop("pending_region_add", None)
        if not kw:
            await update.message.reply_text("⚠️ Немає збереженого ключового слова. Спробуй додати ще раз.")
            app_data.pop("awaiting_kyiv_region_choice", None)
            return
        # зберігаємо
        loc = app_data.get("locations_dict", {})
        loc.setdefault("Київська область", {})[kw.lower()] = region
        save_locations_dict(loc)
        # перечитати з диску і оновити кеш
        app_data["locations_dict"] = load_locations_dict()
        app_data.pop("awaiting_kyiv_region_choice", None)
        await update.message.reply_text(f"✅ Додано «{kw}» до {region} Київської області.")
        return

    # КРОК 1: очікуємо вибір області
    if app_data.get("awaiting_oblast_choice"):
        locs = app_data.get("locations_dict", {})
        oblasts = list(locs.keys())
        if idx < 0 or idx >= len(oblasts):
            await update.message.reply_text("❌ Недійсний номер області.")
            return
        chosen = oblasts[idx]
        kw = app_data.pop("pending_kw", None)
        app_data.pop("awaiting_oblast_choice", None)
        if not kw:
            await update.message.reply_text("⚠️ Немає збереженого ключового слова. Спробуй додати ще раз.")
            return
        if chosen == "Київська область":
            # показати райони
            msg = "🏙 Обрано Київщину. Вибери район:\n\n"
            for i, r in enumerate(KYIV_REGIONS, 1):
                msg += f"{i}. {r}\n"
            await update.message.reply_text(msg)
            app_data["awaiting_kyiv_region_choice"] = True
            app_data["pending_region_add"] = kw
            return
        # інша область — одразу додаємо ключ у групу області (значенням є назва області)
        locs.setdefault(chosen, {})[kw.lower()] = chosen
        save_locations_dict(locs)
        app_data["locations_dict"] = load_locations_dict()
        await update.message.reply_text(f"✅ Додано «{kw}» до області {chosen}.")
        return

# ======================================================
# export dict
# ======================================================
async def export_dict(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔️ Недостатньо прав.")
        return
    data = context.application.bot_data.get("locations_dict", {})
    await update.message.reply_text(f"<pre>{json.dumps(data, ensure_ascii=False, indent=2)}</pre>", parse_mode="HTML")

# ======================================================
# base commands
# ======================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["chat_id"] = update.effective_chat.id
    await update.message.reply_text(
        "Привіт 🌸\nЯ повідомляю про тривоги у Київській області та по Україні.\n"
        "Можеш спробувати: «що по бучі?» або «що по житомиру?»"
    )

# ======================================================
# main
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

    # handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("export_dict", export_dict))

    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)^що по "), handle_dynamic_query))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)^(так|ні)$"), handle_user_yes_no))
    # один універсальний числовий хендлер
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^[0-9]+$"), handle_admin_number_choice))

    async def _poll(context: ContextTypes.DEFAULT_TYPE):
        await process_alerts(context.application, cache)

    app.job_queue.run_repeating(_poll, interval=POLL_INTERVAL, first=0)
    app.job_queue.start()

    await app.run_polling(close_loop=False)

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(main())
    loop.run_forever()
