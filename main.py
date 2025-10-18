import os
import asyncio
import logging
import json
import nest_asyncio
from dataclasses import dataclass, field
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, Optional, Set
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
# 🔹 Класи/словники/хелпери
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

KYIV_OBLAST_NAMES: Set[str] = {"Київська область", "м. Київ"}

OBLASTS_ALL = [
    "Автономна Республіка Крим",
    "Вінницька область",
    "Волинська область",
    "Дніпропетровська область",
    "Донецька область",
    "Житомирська область",
    "Закарпатська область",
    "Запорізька область",
    "Івано-Франківська область",
    "Київська область",
    "м. Київ",
    "Кіровоградська область",
    "Луганська область",
    "Львівська область",
    "Миколаївська область",
    "Одеська область",
    "Полтавська область",
    "Рівненська область",
    "Сумська область",
    "Тернопільська область",
    "Харківська область",
    "Херсонська область",
    "Хмельницька область",
    "Черкаська область",
    "Чернівецька область",
    "Чернігівська область",
]

KYIV_SUBREGIONS = [
    "Вишгородський район",
    "Бучанський район",
    "Фастівський район",
    "Броварський район",
    "Бориспільський район",
    "Обухівський район",
    "Білоцерківський район",
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
# 🔹 Словник локацій (JSON) — завантаження/збереження
# ======================================================
def dict_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "locations_dict.json")

def load_locations_dict() -> Dict:
    path = dict_path()
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"Київська область": {}}, f, ensure_ascii=False, indent=2)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_locations_dict(data: Dict):
    path = dict_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ======================================================
# 🔹 Службові: список областей → адміну
# ======================================================
async def send_all_oblasts_to_admin(bot, admin_id: int):
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
# 🔹 Глобальний МРЧ (вся Україна) + окремий МРЧ для Київщини
# ======================================================
async def process_alerts(app, cache_global: RegionAlertCache, cache_kyiv: RegionAlertCache):
    """
    - Адміну (ADMIN_ID): сповіщення про всі області (глобальний моніторинг).
    - Групі (CHAT_ID): лише Київська область і м. Київ + картинки на початок тривоги.
    - Окремий кеш для Київщини, щоб коректно відпрацьовувати "загальний відбій по області".
    """
    data = await _get_api_data()
    alerts = data.get("alerts", [])

    # Глобальний стан: ключ "<область>::<title>" -> type
    new_state_global = {f"{a['location_oblast']}::{a['location_title']}": a["alert_type"] for a in alerts}

    # Київський стан: лише по Київщині, ключ "<title>" -> type
    relevant_kyiv = [a for a in alerts if a.get("location_oblast") in KYIV_OBLAST_NAMES]
    new_state_kyiv = {a["location_title"]: a["alert_type"] for a in relevant_kyiv}

    chat_id = get_chat_id(app)
    admin_chat = int(ADMIN_ID)
    now = datetime.now().strftime("%H:%M:%S")
    logging.info(f"⏱ Перевірка API @ {now} (UA={len(new_state_global)}, KYIV={len(new_state_kyiv)})")

    # --- ПЕРШИЙ ЗАПУСК ---
    if not cache_global.initialized:
        cache_global.last_alerts = new_state_global
        cache_global.initialized = True
    if not cache_kyiv.initialized:
        cache_kyiv.last_alerts = new_state_kyiv
        cache_kyiv.initialized = True
        return  # перший цикл — без сповіщень, щоб не спамити старим станом

    # --- ГЛОБАЛЬНІ СПОВІЩЕННЯ (адміну) ---
    # Нові або змінені
    for key, alert_type in new_state_global.items():
        if cache_global.last_alerts.get(key) != alert_type and admin_chat:
            oblast, title = key.split("::")
            text = f"🚨 {oblast} — {title}: {ALERT_TYPES_UA.get(alert_type, alert_type)}"
            await app.bot.send_message(chat_id=admin_chat, text=text)
    # Відбої
    for key in list(cache_global.last_alerts.keys()):
        if key not in new_state_global and admin_chat:
            oblast, title = key.split("::")
            text = f"✅ Відбій тривоги у {oblast} — {title}"
            await app.bot.send_message(chat_id=admin_chat, text=text)

    # --- СПОВІЩЕННЯ ДЛЯ ГРУПИ (лише Київщина) ---
    # Нові або змінені
    for title, alert_type in new_state_kyiv.items():
        if cache_kyiv.last_alerts.get(title) != alert_type and chat_id:
            await send_photo_safe(app.bot, chat_id, "images/Alarm.jpg")
            text = f"🚨 *{title}* — *{ALERT_TYPES_UA.get(alert_type, alert_type)}*"
            await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")

    # Відбої по районах Київщини
    for title in list(cache_kyiv.last_alerts.keys()):
        if title not in new_state_kyiv and chat_id:
            await app.bot.send_message(chat_id=chat_id, text=f"✅ Відбій тривоги у *{title}*", parse_mode="Markdown")

    # Загальний відбій по Київській області (коли останній район погас)
    if cache_kyiv.last_alerts and not new_state_kyiv and chat_id:
        await app.bot.send_message(chat_id=chat_id, text=f"✅ Відбій тривоги у {REGION}")
        await send_photo_safe(app.bot, chat_id, "images/Clear.jpg")

    # Оновлюємо кеші
    cache_global.last_alerts = new_state_global
    cache_kyiv.last_alerts  = new_state_kyiv

# ======================================================
# 🔹 Ручні запити по областях/містах
# ======================================================
async def _region_status_contains(keyword: str) -> bool:
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
    active = await _region_status_contains("крим")
    await update.message.reply_text("🚨 У Криму триває тривога!" if active else "✅ У Криму зараз все чисто.")

async def odesa_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = await _region_status_contains("одес")
    await update.message.reply_text("🚨 В Одеській області триває тривога!" if active else "✅ В Одеській області зараз все чисто.")

async def oblast_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = await _region_status_contains("київська")
    await update.message.reply_text("🚨 У Київській області триває тривога!" if active else "✅ У Київській області зараз все чисто.")

async def kyiv_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = await _region_status_contains("київ")
    await update.message.reply_text("🚨 У Києві триває тривога!" if active else "✅ У Києві зараз все чисто.")

async def lugansk_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = await _region_status_contains("луган")
    await update.message.reply_text("🚨 У Луганській області триває тривога!" if active else "✅ У Луганській області зараз все чисто.")

async def chernihiv_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = await _region_status_contains("черніг")
    await update.message.reply_text("🚨 У Чернігівській області триває тривога!" if active else "✅ У Чернігівській області зараз все чисто.")

async def frankivsk_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = await _region_status_contains("франк")
    await update.message.reply_text("🚨 В Івано-Франківській області триває тривога!" if active else "✅ В Івано-Франківській області зараз все чисто.")

# ======================================================
# 🔹 СЛОВНИКОВІ ЗАПИТИ + АДДОН “Соціальне навчання”
# ======================================================
def get_kyiv_dict(app) -> Dict[str, str]:
    """Повертає мапу alias -> 'Район Київщини' (лише розділ Київської області)."""
    return app.bot_data.get("locations_dict", {}).get("Київська область", {})

async def handle_dynamic_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка 'що по <х>' зі словника; якщо НП невідомий — пропозиція надіслати адміну."""
    text_raw = update.message.text or ""
    text = text_raw.lower().strip()
    if not text.startswith("що по"):
        return

    # Відсікаємо спеціальні фрази (ручні хендлери)
    guard_phrases = [
        "що по області", "що по києву", "як там крим", "що по одес", "що по луган",
        "що по франику", "що по івано-франківську", "що по франківську", "що по черніг"
    ]
    if any(p in text for p in guard_phrases):
        return

    kyiv_map = get_kyiv_dict(context.application)
    keyword = text.replace("що по", "").replace("?", "").strip().lower()

    # спроба точного/часткового збігу по словнику Київщини
    region = None
    for alias, subregion in kyiv_map.items():
        if keyword == alias.lower() or keyword in alias.lower():
            region = subregion
            break

    if region:
        # відповідаємо за МРЧ-станом Київщини
        cache_kyiv: RegionAlertCache = context.application.bot_data.get("cache_kyiv", RegionAlertCache())
        active_kyiv = cache_kyiv.last_alerts or {}
        if region in active_kyiv:
            await update.message.reply_text(f"🚨 У {region} триває тривога!")
        else:
            await update.message.reply_text(f"✅ У {region} зараз все спокійно.")
        return

    # --- Невідомий НП: пропонуємо надіслати адміну ---
    markup = ReplyKeyboardMarkup(
        [[KeyboardButton("Так"), KeyboardButton("Ні")]],
        one_time_keyboard=True, resize_keyboard=True
    )
    await update.message.reply_text(
        "🤔 Не знаю такого населеного пункту. Надіслати на розгляд адміну групи щоб додати? (так/ні)",
        reply_markup=markup,
    )
    # Запам'ятовуємо пропозицію для цього користувача
    context.user_data["pending_location"] = keyword

async def user_yes_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Користувацька відповідь Так/Ні щодо пропозиції нового НП."""
    ans = (update.message.text or "").strip().lower()
    if ans not in {"так", "ні"}:
        return
    keyword = context.user_data.get("pending_location")
    if not keyword:
        return

    if ans == "ні":
        await update.message.reply_text("🙂 Добре, не надсилатиму адміну.")
        context.user_data.pop("pending_location", None)
        return

    # "так": відправляємо адміну з кнопками, вшиваємо ключове слово в текст
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"📩 Новий населений пункт запропоновано користувачем:\n<b>{keyword.title()}</b>\nДодати до словника?",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton(f"✅ Додати: {keyword}"), KeyboardButton(f"❌ Ігнорувати: {keyword}")]],
            resize_keyboard=True
        ),
    )
    await update.message.reply_text("✅ Надіслано адміну на розгляд.")
    context.user_data.pop("pending_location", None)

async def admin_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Адмін підтверджує/ігнорує пропозицію, далі — вибір області/району."""
    if update.effective_user.id != ADMIN_ID:
        return
    text = (update.message.text or "").strip()

    # Очікуємо формат "✅ Додати: <keyword>" або "❌ Ігнорувати: <keyword>"
    if text.startswith("❌ Ігнорувати:"):
        await update.message.reply_text("🚫 Пропозицію відхилено.")
        return

    if not text.startswith("✅ Додати:"):
        return

    keyword = text.split(":", 1)[1].strip()
    # Зберігаємо в bot_data, з ким працюємо
    context.application.bot_data["pending_keyword"] = keyword

    # Показуємо клавіатуру з областями (статичний список)
    rows = [[KeyboardButton(o)] for o in OBLASTS_ALL]
    await update.message.reply_text(
        f"🌍 Оберіть область для «{keyword.title()}»:",
        reply_markup=ReplyKeyboardMarkup(rows, resize_keyboard=True)
    )
    context.application.bot_data["await_region"] = True

async def region_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Адмін обирає область; якщо це Київська — просимо ще район, інакше додаємо одразу."""
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.application.bot_data.get("await_region"):
        return

    region = (update.message.text or "").strip()
    keyword = context.application.bot_data.get("pending_keyword")
    if not keyword or not region:
        return

    context.application.bot_data["await_region"] = False
    context.application.bot_data["chosen_region"] = region

    if region == "Київська область":
        rows = [[KeyboardButton(r)] for r in KYIV_SUBREGIONS]
        await update.message.reply_text(
            f"🏞️ До якого району належить «{keyword.title()}»?",
            reply_markup=ReplyKeyboardMarkup(rows, resize_keyboard=True)
        )
        context.application.bot_data["await_subregion"] = True
        return

    # Інша область: додаємо у відповідний розділ словника (для майбутнього використання)
    data = load_locations_dict()
    if region not in data:
        data[region] = {}
    # зберігаємо як просту відповідність alias->область
    data[region][keyword] = region
    save_locations_dict(data)
    context.application.bot_data["locations_dict"] = data

    await update.message.reply_text(f"🆕 Додано: {keyword.title()} → {region}")
    # Чистимо стан
    context.application.bot_data.pop("pending_keyword", None)
    context.application.bot_data.pop("chosen_region", None)

async def subregion_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Адмін обирає район Київської області — запис у словник Київщини."""
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.application.bot_data.get("await_subregion"):
        return

    subregion = (update.message.text or "").strip()
    if subregion not in KYIV_SUBREGIONS:
        return

    keyword = context.application.bot_data.get("pending_keyword")
    if not keyword:
        return

    data = load_locations_dict()
    if "Київська область" not in data:
        data["Київська область"] = {}
    data["Київська область"][keyword] = subregion
    save_locations_dict(data)
    context.application.bot_data["locations_dict"] = data

    await update.message.reply_text(f"🆕 Додано: {keyword.title()} → {subregion}")

    # Чистимо стан
    context.application.bot_data.pop("await_subregion", None)
    context.application.bot_data.pop("pending_keyword", None)
    context.application.bot_data.pop("chosen_region", None)

# ======================================================
# 🔹 Команди /start /stopbot /list_regions
# ======================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["chat_id"] = update.effective_chat.id
    await update.message.reply_text(
        "Привіт 🌸\n"
        "Група отримує тривоги по Київській області, адміністратор — по всій Україні.\n"
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

async def list_regions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔️ Ця команда лише для адміністратора.")
        return
    await update.message.reply_text("⏳ Отримую список областей...")
    await send_all_oblasts_to_admin(context.bot, ADMIN_ID)

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

    # Кеші: глобальний та Київщина — окремо!
    cache_global = RegionAlertCache()
    cache_kyiv   = RegionAlertCache()
    app.bot_data["cache_kyiv"] = cache_kyiv

    # Словник
    app.bot_data["locations_dict"] = load_locations_dict()

    # Одноразово — список областей адміну
    await send_all_oblasts_to_admin(app.bot, ADMIN_ID)

    # Команди
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stopbot", stopbot))
    app.add_handler(CommandHandler("list_regions", list_regions))

    # Ручні запити (специфічні) — ДО загального словникового
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по області"), oblast_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по києву"), kyiv_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)як там крим"), krym_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по одес"), odesa_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по франику|що по івано-франківську|що по франківську"), frankivsk_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по луган"), lugansk_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по черніг"), chernihiv_alerts))

    # Словниковий запит
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)^що по "), handle_dynamic_query))
    # Соціальне навчання: відповідь користувача “так/ні”
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)^(так|ні)$"), user_yes_no))
    # Підтвердження адміном
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^(✅ Додати: .+|❌ Ігнорувати: .+)$"), admin_choice))
    # Вибір області адміном
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^(" + "|".join(map(lambda s: s.replace('.', r'\.'), OBLASTS_ALL)) + r")$"), region_selected))
    # Вибір району Київщини адміном
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^(" + "|".join(KYIV_SUBREGIONS) + r")$"), subregion_selected))

    app.add_error_handler(error_handler)

    async def _poll(context: ContextTypes.DEFAULT_TYPE):
        await process_alerts(context.application, cache_global, cache_kyiv)

    app.job_queue.run_repeating(_poll, interval=POLL_INTERVAL, first=0)
    app.job_queue.start()

    logging.info("✅ KytsjaAlarm Stable_v6 + SocialLearning аддон — запущено.")
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
