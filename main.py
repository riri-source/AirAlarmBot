import os
import asyncio
import json
import logging
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
    last_alerts: Dict[str, str] = field(default_factory=dict)  # {location_title: alert_type}
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
# 🔹 Хелпери (словник, API, зображення)
# ======================================================
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
    with open(_dict_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

async def _get_api_data():
    headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
    async with aiohttp.ClientSession() as s:
        async with s.get(API_URL, headers=headers, timeout=10) as r:
            return await r.json()

async def send_photo_safe(bot, chat_id: Optional[int], image_path: str):
    if not chat_id:
        return
    try:
        with open(image_path, "rb") as ph:
            await bot.send_photo(chat_id=chat_id, photo=ph)
    except Exception:
        pass

# ======================================================
# 🔹 Обробник помилок
# ======================================================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.error("⚠️ Помилка:", exc_info=context.error)
    try:
        if update and hasattr(update, "message") and update.message:
            await update.message.reply_text("⚠️ Виникла непередбачена помилка. Спробуй пізніше.")
    except Exception:
        pass

# ======================================================
# 🔹 Моніторинг тривог (МРЧ Київщина + Глобально адміну)
# ======================================================
async def process_alerts(app, cache: RegionAlertCache):
    data = await _get_api_data()
    alerts = data.get("alerts", []) or []
    chat_id = app.bot_data.get("chat_id")

    # Київщина + Київ -> група
    relevant_kyiv = [a for a in alerts if a.get("location_oblast") in {"Київська область", "м. Київ"}]
    new_state_kyiv = {a.get("location_title"): a.get("alert_type") for a in relevant_kyiv}

    # Вся Україна -> адміну
    new_state_global = {f"{a.get('location_oblast')} — {a.get('location_title')}": a.get("alert_type") for a in alerts}

    # Перший запуск: лише запам'ятати стан, не сповіщати
    if not cache.initialized:
        cache.last_alerts = new_state_kyiv
        app.bot_data["last_global_alerts"] = new_state_global
        cache.initialized = True
        return

    # Київщина → група (нові/змінені)
    for r, t in new_state_kyiv.items():
        if cache.last_alerts.get(r) != t and chat_id:
            await send_photo_safe(app.bot, chat_id, "images/Alarm.jpg")
            await app.bot.send_message(
                chat_id=chat_id,
                text=f"🚨 *{r}* — *{ALERT_TYPES_UA.get(t or 'air_raid', 'Повітряна тривога!')}*",
                parse_mode="Markdown"
            )
    # Київщина → група (відбої)
    for r in list(cache.last_alerts.keys()):
        if r not in new_state_kyiv and chat_id:
            await app.bot.send_message(chat_id=chat_id, text=f"✅ Відбій тривоги у *{r}*", parse_mode="Markdown")
    # Загальний відбій у Київській
    if cache.last_alerts and not new_state_kyiv and chat_id:
        await app.bot.send_message(chat_id=chat_id, text=f"✅ Відбій тривоги у {REGION}")
        await send_photo_safe(app.bot, chat_id, "images/Clear.jpg")

    # Вся Україна → адміну (нові/змінені)
    last_global = app.bot_data.get("last_global_alerts", {})
    for key, t in new_state_global.items():
        if last_global.get(key) != t:
            await app.bot.send_message(chat_id=ADMIN_ID, text=f"🚨 {key}: {ALERT_TYPES_UA.get(t or 'air_raid', 'Повітряна тривога!')}")
    # Вся Україна → адміну (відбої)
    for key in list(last_global.keys()):
        if key not in new_state_global:
            await app.bot.send_message(chat_id=ADMIN_ID, text=f"✅ Відбій тривоги: {key}")

    cache.last_alerts = new_state_kyiv
    app.bot_data["last_global_alerts"] = new_state_global

# ======================================================
# 🔹 Ручні текстові запити (області / міста)
# ======================================================
async def region_status(keyword: str) -> bool:
    """Повертає True, якщо в API є активна тривога по ключовому слову (частковий збіг в області/локації)."""
    data = await _get_api_data()
    kw = (keyword or "").lower()
    for a in data.get("alerts", []) or []:
        if a.get("finished_at") is None:
            oblast = (a.get("location_oblast") or "").lower()
            title = (a.get("location_title") or "").lower()
            if kw in oblast or kw in title:
                return True
    return False

async def krym_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚨 У Криму триває тривога!" if await region_status("крим")
                                    else "✅ У Криму зараз все чисто.")

async def odesa_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚨 В Одеській області триває тривога!" if await region_status("одес")
                                    else "✅ В Одеській області зараз все чисто.")

async def oblast_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚨 У Київській області триває тривога!" if await region_status("київська")
                                    else "✅ У Київській області зараз все чисто.")

async def kyiv_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚨 У Києві триває тривога!" if await region_status("київ")
                                    else "✅ У Києві зараз все чисто.")

async def lugansk_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚨 У Луганській області триває тривога!" if await region_status("луган")
                                    else "✅ У Луганській області зараз все чисто.")

async def chernihiv_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚨 У Чернігівській області триває тривога!" if await region_status("черніг")
                                    else "✅ У Чернігівській області зараз все чисто.")

async def frankivsk_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚨 В Івано-Франківській області триває тривога!" if await region_status("франк")
                                    else "✅ В Івано-Франківській області зараз все чисто.")

# ======================================================
# 🔹 Словникові запити + флоу додавання НП
# ======================================================
def _norm(s: str) -> str:
    if not s: return ""
    s = s.lower().strip()
    for bad in ["’","'","–","—","‐","-",".",","]: s = s.replace(bad, " ")
    return " ".join(s.split())

async def handle_dynamic_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка запитів виду 'що по <назві>' згідно зі словником."""
    text = (update.message.text or "").lower().strip()

    # не чіпати спеціальні фрази
    special = [
        "що по області", "що по києву", "як там крим",
        "що по одес", "що по луган", "що по франик", "що по франківськ", "що по івано-франківськ",
        "що по черніг"
    ]
    if any(x in text for x in special):
        return
    if not text.startswith("що по"):
        return

    kw_raw = text.replace("що по", "", 1).strip().rstrip("?!,. ")
    kw = _norm(kw_raw)
    locations = context.application.bot_data.get("locations_dict", {})

    found_oblast, found_region = None, None
    # точний збіг
    for oblast, mapping in locations.items():
        for k, region in mapping.items():
            if kw == _norm(k):
                found_oblast, found_region = oblast, region
                break
        if found_oblast: break
    # частковий збіг
    if not found_oblast:
        for oblast, mapping in locations.items():
            for k, region in mapping.items():
                nk = _norm(k)
                if kw in nk or nk in kw:
                    found_oblast, found_region = oblast, region
                    break
            if found_oblast: break

    if not found_oblast:
        # запросити дозвіл на відправку адміну
        context.user_data["pending_add"] = kw_raw
        await update.message.reply_text("🤔 Не знаю такого населеного пункту. Надіслати адміну для розгляду? (так/ні)")
        return

    # Формуємо відповідь
    cache: RegionAlertCache = context.application.bot_data.get("alert_cache", RegionAlertCache())
    active_kyiv = cache.last_alerts or {}
    if found_oblast in {"Київська область", "м. Київ"}:
        is_active = found_region in active_kyiv
        msg = (f"🚨 В області *{found_oblast}* ({found_region}) триває тривога!"
               if is_active else
               f"✅ В області *{found_obласт}* ({found_region}) все тихо!")
        await update.message.reply_text(msg, parse_mode="Markdown")
    else:
        is_active = await region_status(found_oblast.lower())
        msg = (f"🚨 В області *{found_oblast}* триває тривога!"
               if is_active else
               f"✅ В області *{found_oblast}* все тихо!")
        await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_user_yes_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Користувач підтверджує / відхиляє надсилання адміну невідомого НП."""
    txt = (update.message.text or "").strip().lower()
    if txt not in {"так", "ні"}:
        return
    if "pending_add" not in context.user_data:
        return

    if txt == "ні":
        context.user_data.pop("pending_add", None)
        await update.message.reply_text("👌 Добре, не додаємо.")
        return

    kw = context.user_data.pop("pending_add")
    app_data = context.application.bot_data
    app_data["pending_kw"] = kw
    app_data["awaiting_oblast_choice"] = True

    locs = app_data.get("locations_dict", {})
    oblasts = list(locs.keys())
    msg = f"📍 Вкажи номер області для «{kw}»:\n\n"
    for i, o in enumerate(oblasts, 1):
        msg += f"{i}. {o}\n"
    await context.bot.send_message(chat_id=ADMIN_ID, text=msg)

async def handle_admin_number_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Адмін обирає область (крок 1) або район Київщини (крок 2)."""
    if update.effective_user.id != ADMIN_ID:
        return
    t = (update.message.text or "").strip()
    if not t.isdigit():
        return
    idx = int(t) - 1
    app_data = context.application.bot_data

    # Крок 2: район Київщини
    if app_data.get("awaiting_kyiv_region_choice"):
        if idx < 0 or idx >= len(KYIV_REGIONS):
            await update.message.reply_text("❌ Недійсний номер району.")
            return
        region = KYIV_REGIONS[idx]
        kw = app_data.pop("pending_region_add", None)
        if not kw:
            await update.message.reply_text("⚠️ Немає збереженого ключового слова.")
            return
        loc = app_data.get("locations_dict", {})
        loc.setdefault("Київська область", {})[kw.lower()] = region
        save_locations_dict(loc)
        app_data["locations_dict"] = load_locations_dict()
        app_data.pop("awaiting_kyiv_region_choice", None)
        await update.message.reply_text(f"✅ Додано «{kw}» до {region} Київської області.")
        return

    # Крок 1: область
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
            await update.message.reply_text("⚠️ Немає збереженого ключового слова.")
            return

        if chosen == "Київська область":
            msg = "🏙 Обрано Київщину. Вибери район:\n\n"
            for i, r in enumerate(KYIV_REGIONS, 1):
                msg += f"{i}. {r}\n"
            await update.message.reply_text(msg)
            app_data["awaiting_kyiv_region_choice"] = True
            app_data["pending_region_add"] = kw
            return

        # Інша область — ключ -> назва області
        locs.setdefault(chosen, {})[kw.lower()] = chosen
        save_locations_dict(locs)
        app_data["locations_dict"] = load_locations_dict()
        await update.message.reply_text(f"✅ Додано «{kw}» до області {chosen}.")
        return

# ======================================================
# 🔹 Команди /start, /help, /listregions, /exportdict, /stopbot
# ======================================================
async def start(update, ctx):
    """Пуск і коротке зведення актуальних тривог адміну."""
    ctx.application.bot_data["chat_id"] = update.effective_chat.id
    await update.message.reply_text("Привіт 🌸 KytsjaAlarm запущено.\nОтримую поточні тривоги...")

    data = await _get_api_data()
    alerts = data.get("alerts", []) or []
    if not alerts:
        msg = "✅ Зараз по всій Україні спокійно."
    else:
        lines = []
        for a in alerts:
            t = a.get("alert_type") or "air_raid"
            lines.append(
                f"🚨 {a.get('location_oblast')} — {a.get('location_title')}: "
                f"{ALERT_TYPES_UA.get(t, 'Повітряна тривога!')}"
            )
        msg = "🗺 <b>Актуальні тривоги:</b>\n" + "\n".join(lines)

    await ctx.bot.send_message(chat_id=ADMIN_ID, text=msg, parse_mode="HTML")

    if update.effective_chat.type in ("group", "supergroup"):
        await update.message.reply_text("✅ Бот активний. Моніторю Київську область.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🧭 <b>Команди KytsjaAlarm Bot</b>\n\n"
        "📍 <b>Основні:</b>\n"
        "<code>/start</code> — запустити бота або перевірити стан\n"
        "<code>/help</code> — показати цей список команд\n"
        "<code>/stopbot</code> — зупинити бота (адміністратор)\n\n"
        "📡 <b>Моніторинг і запити:</b>\n"
        "<code>/listregions</code> — показати області, які бачить API\n"
        "<code>/exportdict</code> — показати поточний словник назв (адміністратор)\n\n"
        "🗺 <b>Текстові запити:</b>\n"
        "«що по області» — Київська область\n"
        "«що по Києву» — м. Київ\n"
        "«як там Крим?» — Крим\n"
        "«що по Франику» — Івано-Франківська область\n"
        "«що по &lt;назві&gt;» — будь-який населений пункт зі словника\n\n"
        "📩 Якщо боту невідомий пункт — він запитає, чи надіслати адміну для додавання."
        "\n\n🐾 Версія: KytsjaAlarm v9.3.3 RC Final"
    )
    await update.message.reply_text(help_text, parse_mode="HTML")

async def list_regions(update, ctx):
    await update.message.reply_text("⏳ Отримую список областей...")
    data = await _get_api_data()
    regs = sorted(set(a.get("location_oblast") for a in (data.get("alerts", []) or []) if a.get("location_oblast")))
    txt = "🧭 Список областей, які бачить API:\n\n" + "\n".join(f"• {r}" for r in regs) if regs else "❌ API не повернуло даних."
    await update.message.reply_text(txt)

async def export_dict(update, ctx):
    if update.effective_user.id != ADMIN_ID:
        return
    data = ctx.application.bot_data.get("locations_dict", {})
    await update.message.reply_text(f"<pre>{json.dumps(data, ensure_ascii=False, indent=2)}</pre>", parse_mode="HTML")

async def stopbot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔️ Лише адміністратор.")
        return
    await update.message.reply_text("🛑 Зупиняю роботу...")
    try:
        await ctx.application.job_queue.stop()
        await ctx.application.stop_running()
        await ctx.application.shutdown()
        await ctx.application.stop()
        await update.message.reply_text("✅ KytsjaAlarm повністю зупинено.")
        logging.info("🛑 Бот зупинено адміністратором.")
        os._exit(0)
    except Exception as e:
        logging.error(f"Помилка при зупинці: {e}")
        await update.message.reply_text(f"⚠️ Не вдалося завершити повністю: {e}")

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
    app.add_handler(CommandHandler("listregions", list_regions))
    app.add_handler(CommandHandler("exportdict", export_dict))

    # Ручні запити (фрази)
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по області"), oblast_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по києву"), kyiv_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)як там крим"), krym_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по одес"), odesa_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по франик|що по франківськ|що по івано-франківськ"), frankivsk_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по луган"), lugansk_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по черніг"), chernihiv_alerts))

    # Словниковий аддон
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)^що по "), handle_dynamic_query))
    # Підтвердження користувача (так/ні)
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)^(так|ні)$"), handle_user_yes_no))
    # Вибір числом (адмін)
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^[0-9]+$"), handle_admin_number_choice))

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
