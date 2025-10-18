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
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
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
    last_alerts: Dict[str, str] = field(default_factory=dict)   # ключ: "Область::Район/Громада/Назва"
    initialized: bool = False

ALERT_TYPES_UA = {
    "air_raid": "Повітряна тривога!",
    "chemical": "Хімічна тривога",
    "radiation": "Радіаційна тривога",
    "other": "Інша тривога",
}

KYIV_SUBREGIONS = [
    "Вишгородський район", "Бучанський район", "Фастівський район",
    "Броварський район", "Бориспільський район",
    "Обухівський район", "Білоцерківський район"
]

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
# 🔹 Службові (для адміну)
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
# 🔹 Завантаження / збереження зовнішнього словника
# ======================================================
def _dict_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "locations_dict.json")

def load_locations_dict() -> Dict:
    fp = _dict_path()
    if not os.path.exists(fp):
        with open(fp, "w", encoding="utf-8") as f:
            json.dump({"Київська область": {}}, f, ensure_ascii=False, indent=2)
    with open(fp, "r", encoding="utf-8") as f:
        return json.load(f)

def save_locations_dict(data: Dict):
    fp = _dict_path()
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ======================================================
# 🔹 Глобальний моніторинг (вся Україна) + група лише Київщина
# ======================================================
async def process_alerts(app, cache: RegionAlertCache):
    data = await _get_api_data()
    alerts = data.get("alerts", [])
    new_state = {f"{a['location_oblast']}::{a['location_title']}": a["alert_type"] for a in alerts}

    chat_id = get_chat_id(app)
    admin_chat = int(ADMIN_ID)
    now = datetime.now().strftime("%H:%M:%S")
    logging.info(f"⏱ Перевірка API @ {now} ({len(new_state)} активних тривог)")

    # Для словникового аддону кеш Київщини зручно мати окремо
    app.bot_data["kyiv_current_keys"] = {k for k in new_state.keys() if k.startswith("Київська область::") or k.startswith("м. Київ::")}

    if not cache.initialized:
        cache.last_alerts = new_state
        cache.initialized = True
        return

    # Нові/змінені
    for key, alert_type in new_state.items():
        if cache.last_alerts.get(key) == alert_type:
            continue
        oblast, title = key.split("::", 1)
        text = f"🚨 *{oblast}* — {title}: *{ALERT_TYPES_UA.get(alert_type, alert_type)}*"

        # адміністратор — все
        if admin_chat:
            await app.bot.send_message(chat_id=admin_chat, text=text, parse_mode="Markdown")

        # група — лише Київщина (з картинкою)
        if chat_id and (oblast in {"Київська область", "м. Київ"}):
            await send_photo_safe(app.bot, chat_id, "images/Alarm.jpg")
            await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")

    # Відбої
    for key in list(cache.last_alerts.keys()):
        if key not in new_state:
            oblast, title = key.split("::", 1)
            text = f"✅ Відбій тривоги у *{oblast}* — {title}"
            if admin_chat:
                await app.bot.send_message(chat_id=admin_chat, text=text, parse_mode="Markdown")
            if chat_id and (oblast in {"Київська область", "м. Київ"}):
                await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")

    # Загальний відбій по області (картинка) — лише для Київщини
    # Якщо раніше були ключі Київщини, а тепер жодного
    if chat_id:
        had_kyiv = any(k.startswith("Київська область::") or k.startswith("м. Київ::") for k in cache.last_alerts.keys())
        has_kyiv = any(k.startswith("Київська область::") or k.startswith("м. Київ::") for k in new_state.keys())
        if had_kyiv and not has_kyiv:
            await app.bot.send_message(chat_id=chat_id, text=f"✅ Відбій тривоги у {REGION}")
            await send_photo_safe(app.bot, chat_id, "images/Clear.jpg")

    cache.last_alerts = new_state

# ======================================================
# 🔹 Ручні запити (як було у v6)
# ======================================================
async def _region_status(keyword: str) -> bool:
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
    active = await _region_status("крим")
    await update.message.reply_text("🚨 У Криму триває тривога!" if active else "✅ У Криму зараз все чисто.")

async def odesa_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = await _region_status("одес")
    await update.message.reply_text("🚨 В Одеській області триває тривога!" if active else "✅ В Одеській області зараз все чисто.")

async def oblast_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = await _region_status("київська")
    await update.message.reply_text("🚨 У Київській області триває тривога!" if active else "✅ У Київській області зараз все чисто.")

async def kyiv_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = await _region_status("київ")
    await update.message.reply_text("🚨 У Києві триває тривога!" if active else "✅ У Києві зараз все чисто.")

async def lugansk_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = await _region_status("луган")
    await update.message.reply_text("🚨 У Луганській області триває тривога!" if active else "✅ У Луганській області зараз все чисто.")

async def chernihiv_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = await _region_status("черніг")
    await update.message.reply_text("🚨 У Чернігівській області триває тривога!" if active else "✅ У Чернігівській області зараз все чисто.")

async def frankivsk_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = await _region_status("франк")
    await update.message.reply_text("🚨 В Івано-Франківській області триває тривога!" if active else "✅ В Івано-Франківській області зараз все чисто.")

# ======================================================
# 🔹 Словниковий запит + соціальне навчання (аддон)
# ======================================================
def _normalize(s: str) -> str:
    return " ".join((s or "").lower().replace("?", "").replace("!", "").strip().split())

async def handle_dynamic_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'що по <назві>' — шукаємо у словнику Київщини; якщо не знайдено — пропонуємо надіслати адміну."""
    text = (update.message.text or "")
    if not text.lower().startswith("що по"):
        return

    keyword = _normalize(text.replace("що по", "", 1))
    if not keyword:
        return

    locations = context.application.bot_data.get("locations_dict", {}).get("Київська область", {})

    # пошук точний або частковий
    region = None
    for k, v in locations.items():
        if keyword == _normalize(k) or keyword in _normalize(k):
            region = v
            break

    if region:
        # використовуємо поточний стан із кешу Київщини (без додзапитів)
        active_keys = context.application.bot_data.get("kyiv_current_keys", set())
        # регіон в new_state має вигляд "Київська область::Бучанський район" тощо
        is_active = any(f"Київська область::{region}" in key or f"м. Київ::{region}" in key for key in active_keys)
        await update.message.reply_text(f"🚨 У {region} триває тривога!" if is_active else f"✅ У {region} зараз все спокійно.")
        return

    # Не знайшли — питаємо користувача, чи надсилати адміну
    markup = ReplyKeyboardMarkup([[KeyboardButton("Так"), KeyboardButton("Ні")]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "🤔 Не знаю такого населеного пункту. Надіслати на розгляд адміну групи щоб додати? (так/ні)",
        reply_markup=markup,
    )
    # збережемо запит саме за цим користувачем
    context.user_data["pending_location"] = keyword

async def user_send_to_admin_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Користувач відповідає 'так/ні' на пропозицію надіслати адміну."""
    txt = (update.message.text or "").strip().lower()
    if txt not in {"так", "ні"}:
        return
    pending = context.user_data.pop("pending_location", None)
    if not pending:
        return
    if txt == "ні":
        await update.message.reply_text("🙂 Добре, не надсилатиму адміну.")
        return

    # надсилаємо адміну й фіксуємо останню пропозицію у bot_data (спрощено — остання активна)
    context.application.bot_data["admin_pending_name"] = pending
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"📩 Новий населений пункт запропоновано користувачем:\n<b>{pending.title()}</b>\nДодати до словника?",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("✅ Додати"), KeyboardButton("❌ Ігнорувати")]],
            resize_keyboard=True
        ),
    )
    await update.message.reply_text("✅ Надіслано адміну на розгляд.")

async def admin_add_ignore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Адмін обирає: додати / ігнорувати."""
    if update.effective_user.id != ADMIN_ID:
        return
    txt = (update.message.text or "").strip()
    if txt not in {"✅ Додати", "❌ Ігнорувати"}:
        return
    name = context.application.bot_data.get("admin_pending_name")
    if not name:
        await update.message.reply_text("ℹ️ Немає активної пропозиції до додавання.")
        return
    if txt == "❌ Ігнорувати":
        context.application.bot_data.pop("admin_pending_name", None)
        await update.message.reply_text("🚫 Пропозицію відхилено.")
        return

    # ✅ Додати — питаємо область (зі списку, що реально приходить з API)
    try:
        data = await _get_api_data()
        oblasts = sorted({a.get("location_oblast") for a in data.get("alerts", []) if a.get("location_oblast")})
        # гарантуємо Київську, навіть якщо тимчасово немає у фіді
        if "Київська область" not in oblasts:
            oblasts.insert(0, "Київська область")
    except Exception:
        oblasts = ["Київська область"]
    markup = ReplyKeyboardMarkup([[KeyboardButton(o)] for o in oblasts], resize_keyboard=True)
    await update.message.reply_text(f"🌍 Оберіть область для «{name.title()}»:", reply_markup=markup)
    context.application.bot_data["await_region_for"] = name

async def admin_region_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Адмін вибирає область; якщо Київська — додатково просимо район."""
    if update.effective_user.id != ADMIN_ID:
        return
    region_txt = (update.message.text or "").strip()
    name = context.application.bot_data.get("await_region_for")
    if not name:
        return

    if region_txt == "Київська область":
        # обрати район
        markup = ReplyKeyboardMarkup([[KeyboardButton(r)] for r in KYIV_SUBREGIONS], resize_keyboard=True)
        await update.message.reply_text(f"🏞️ До якого району належить «{name.title()}»?", reply_markup=markup)
        context.application.bot_data["await_subregion_for"] = name
        context.application.bot_data.pop("await_region_for", None)
        return

    # інша область: записуємо "назва НП -> область" як базовий таргет
    locations = load_locations_dict()
    # Структура словника: для Київщини — мапа псевдонімів у район.
    # Для інших областей збережемо під ключем "Інші області" просту мапу до області.
    # Щоб не ламати існуючу логіку, просто заведемо секцію з назвою області теж як розділ.
    section = region_txt
    locations.setdefault(section, {})
    locations[section][name] = section  # базово відповідаємо на рівні області
    save_locations_dict(locations)
    context.application.bot_data["locations_dict"] = locations
    context.application.bot_data.pop("await_region_for", None)
    context.application.bot_data.pop("admin_pending_name", None)
    await update.message.reply_text(f"✅ Додано: {name.title()} → {section}")

async def admin_subregion_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Адмін вибирає район Київської області — дописуємо у словник секції 'Київська область'."""
    if update.effective_user.id != ADMIN_ID:
        return
    subr = (update.message.text or "").strip()
    name = context.application.bot_data.get("await_subregion_for")
    if not name:
        return
    if subr not in KYIV_SUBREGIONS:
        return  # ігноруємо сторонні репліки

    locations = load_locations_dict()
    locations.setdefault("Київська область", {})
    locations["Київська область"][name] = subr
    save_locations_dict(locations)
    context.application.bot_data["locations_dict"] = locations

    context.application.bot_data.pop("await_subregion_for", None)
    context.application.bot_data.pop("admin_pending_name", None)
    await update.message.reply_text(f"🆕 Додано: {name.title()} → {subr}")

# ======================================================
# 🔹 /list_regions
# ======================================================
async def list_regions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("⛔️ Ця команда лише для адміністратора.")
        return
    await update.message.reply_text("⏳ Отримую список областей...")
    await send_all_oblasts_to_admin(context.bot, ADMIN_ID)

# ======================================================
# 🔹 Базові команди
# ======================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["chat_id"] = update.effective_chat.id
    await update.message.reply_text(
        "Привіт 🌸\n"
        "Я повідомляю про тривоги у Київській області (група) та по всій Україні (адміну).\n"
        "Можеш спробувати: «що по ірпеню?» або «що по борисполю?»"
    )

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
    app.bot_data["locations_dict"] = load_locations_dict()

    # Одноразово: покажемо адміну список областей
    await send_all_oblasts_to_admin(app.bot, ADMIN_ID)

    # Команди
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stopbot", stopbot))
    app.add_handler(CommandHandler("list_regions", list_regions))

    # Ручні запити (як раніше)
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по області"), oblast_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по києву"), kyiv_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)як там крим"), krym_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по одес"), odesa_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по франику|що по івано-франківську|що по франківську"), frankivsk_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по луган"), lugansk_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по черніг"), chernihiv_alerts))

    # Словниковий запит
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)^що по "), handle_dynamic_query))
    # Відповідь користувача так/ні
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)^(так|ні)$"), user_send_to_admin_choice))
    # Адмін: додати/ігнорувати
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^(✅ Додати|❌ Ігнорувати)$"), admin_add_ignore))
    # Адмін: вибір області
    app.add_handler(MessageHandler(filters.TEXT & (~filters.Regex(r"^(✅ Додати|❌ Ігнорувати)$") & ~filters.Regex("(?i)^(так|ні)$")), admin_region_selected))
    # Адмін: вибір району Київщини
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("|".join(KYIV_SUBREGIONS)), admin_subregion_selected))

    app.add_error_handler(error_handler)

    async def _poll(context: ContextTypes.DEFAULT_TYPE):
        await process_alerts(context.application, cache)

    app.job_queue.run_repeating(_poll, interval=POLL_INTERVAL, first=0)
    app.job_queue.start()

    logging.info("✅ KytsjaAlarm v6 + SocialDictionary Add-on запущено.")
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
