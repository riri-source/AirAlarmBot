import os
from dataclasses import dataclass, field
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
import asyncio
import nest_asyncio
import logging
from typing import Dict, Optional

import time
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

# ===== Load .env (якщо використовується) =====
load_dotenv()

# ===== Легкий HTTP-сервер (healthcheck/ping) =====
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

# ===== Логування =====
logging.basicConfig(level=logging.INFO)

# ===== Змінні оточення (узгоджені назви) =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
ALERTS_TOKEN = os.getenv("ALERTS_TOKEN")
REGION = os.getenv("REGION", "Київська область")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 25))

# CHAT_ID може бути задано як ENV або встановлюватись після /start
CHAT_ID_ENV = os.getenv("CHAT_ID")
DEFAULT_CHAT_ID = int(CHAT_ID_ENV) if CHAT_ID_ENV else None

# Адмін для аварійної зупинки
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

if not BOT_TOKEN or not ALERTS_TOKEN:
    raise RuntimeError("Не задано одну або кілька обов'язкових змінних оточення: BOT_TOKEN, ALERTS_TOKEN")

API_URL = "https://api.alerts.in.ua/v1/alerts/active.json"

# ===== Мапа типів тривог (українські назви) =====
ALERT_TYPES_UA = {
    "air_raid": "Повітряна тривога!",
    "chemical": "Хімічна тривога",
    "radiation": "Радіаційна тривога",
    "other": "Інша тривога",
}

# ===== Хелпер нормалізації назв =====
def _norm(s: str) -> str:
    if not s:
        return ""
    s = s.lower()
    for bad in ["м.", "обл.", "область", ",", "’", "'", "‐", "-", "–", "—"]:
        s = s.replace(bad, " ")
    return " ".join(s.split())


@dataclass
class RegionAlertCache:
    """Зберігає останній стан тривог по районах."""
    last_alerts: Dict[str, str] = field(default_factory=dict)
    initialized: bool = False


def get_chat_id(app) -> Optional[int]:
    """Повертає актуальний chat_id з bot_data, якщо він відомий."""
    chat_id = app.bot_data.get("chat_id")
    if chat_id is not None:
        return int(chat_id)
    default_chat = app.bot_data.get("default_chat_id")
    return int(default_chat) if default_chat is not None else None


async def send_photo_safe(bot, chat_id: Optional[int], image_path: str) -> bool:
    """Надсилає зображення, якщо файл існує. Повертає True при успіху."""
    if not chat_id:
        return False
    try:
        with open(image_path, "rb") as photo:
            await bot.send_photo(chat_id=chat_id, photo=photo)
        return True
    except FileNotFoundError:
        logging.warning(f"Файл {image_path} не знайдено.")
    except Exception as exc:  # noqa: BLE001
        logging.debug(f"Не вдалося відправити {image_path}: {exc}")
    return False


# ===== Запит до API з анти-кешем та м'яким фільтром =====
async def fetch_alerts(location_name, city_type="oblast"):
    headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
    try:
        async with aiohttp.ClientSession() as session:
            # anti-cache параметр, щоб не ловити застарілу відповідь
            async with session.get(
                API_URL,
                headers=headers,
                timeout=10,
                params={"_": int(time.time())},
            ) as resp:
                status = resp.status
                try:
                    data = await resp.json()
                except Exception:
                    txt = await resp.text()
                    logging.error(f"API не повернуло JSON (status={status}): {txt[:300]}")
                    return []
        alerts = data.get("alerts", [])
        logging.info(
            f"API повернуло {len(alerts)} запис(ів). "
            f"Приклад oblast: {alerts[0].get('location_oblast') if alerts else '—'}"
        )

        loc_norm = _norm(location_name)

        if city_type == "oblast":
            filtered = []
            for a in alerts:
                ob = _norm(a.get("location_oblast"))
                # допускаємо різні варіанти написання/мови
                if loc_norm and (loc_norm in ob or ob in loc_norm):
                    filtered.append(a)
            return filtered
        else:
            filtered = []
            for a in alerts:
                t = _norm(a.get("location_title"))
                ob = _norm(a.get("location_oblast"))
                if loc_norm and (loc_norm in t or t in loc_norm or loc_norm in ob or ob in loc_norm):
                    filtered.append(a)
            return filtered

    except Exception as e:
        logging.error(f"Помилка при запиті до API: {e}")
        return []


# ===== Хендлери =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    context.application.bot_data["chat_id"] = chat_id  # зберігаємо chat_id після першої команди /start
    await update.message.reply_text(
        f"Привіт 🌸\nНапиши «Що по області» щоб дізнатись, де зараз тривога у {REGION}."
    )


async def oblast_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alerts = await fetch_alerts(REGION)
    if not alerts:
        await update.message.reply_text(f"✅ {REGION} — зараз все чисто!")
        await send_photo_safe(context.application.bot, update.effective_chat.id, "images/Saefty.jpg")
        return

    text = f"🚨 *Активні тривоги у {REGION}:*\n"
    for alert in alerts:
        raion = alert.get("location_title", "Невідомий район")
        alert_type = alert.get("alert_type", "невідомо")
        alert_type_ua = ALERT_TYPES_UA.get(alert_type, alert_type)
        text += f"• {raion} — {alert_type_ua}\n"
    await update.message.reply_markdown(text)


async def city_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE, city_name, city_label):
    alerts = await fetch_alerts(city_name, city_type="city")
    if not alerts:
        await update.message.reply_text(f"✅ У {city_label} зараз все чисто!")
        await send_photo_safe(context.application.bot, update.effective_chat.id, "images/Saefty.jpg")
        return

    text = f"🚨 У {city_label} зафіксована тривога!\n"
    for alert in alerts:
        raion = alert.get("location_title", "Невідомий район")
        alert_type = alert.get("alert_type", "невідомо")
        alert_type_ua = ALERT_TYPES_UA.get(alert_type, alert_type)
        text += f"• {raion} — {alert_type_ua}\n"
    await update.message.reply_text(text)


async def krym_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await city_alerts(update, context, "Автономна Республіка Крим", "Крим")


async def kyiv_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await city_alerts(update, context, "м. Київ", "Київ")


async def odesa_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await city_alerts(update, context, "м. Одеса", "Одеса")


async def frankivsk_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await city_alerts(update, context, "м. Івано-Франківськ", "Івано-Франківськ")


# ===== Діагностика =====
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cache: RegionAlertCache = context.application.bot_data.get("alert_cache") or RegionAlertCache()
    keys = ", ".join(cache.last_alerts.keys()) or "—"
    chat_id = get_chat_id(context.application)
    await update.message.reply_text(
        f"ℹ️ REGION: {REGION}\n"
        f"CHAT_ID: {chat_id or 'нема'}\n"
        f"POLL_INTERVAL: {POLL_INTERVAL}s\n"
        f"Поточні райони (ключі): {keys}"
    )


# ===== Один «тік» опитування API =====
async def process_alerts(app, cache: RegionAlertCache):
    """Завантажує актуальні тривоги та розсилає оновлення у чат."""
    tick = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"⏱ Перевірка {REGION} @ {tick}")

    alerts = await fetch_alerts(REGION)
    new_state = {a.get("location_title"): a.get("alert_type") for a in alerts}

    logging.info(f"Ключі new_state: {list(new_state.keys())}")
    logging.info(f"Ключі last_alerts: {list(cache.last_alerts.keys())}")

    # Перший запуск: просто запам'ятовуємо поточний стан, щоб не дублювати "старі" тривоги
    if not cache.initialized:
        cache.last_alerts = new_state
        cache.initialized = True
        logging.debug("Ініціалізовано стан тривог без сповіщень.")
        return

    chat_id = get_chat_id(app)

    # Нові/змінені тривоги по районах
    for raion, alert_type in new_state.items():
        if cache.last_alerts.get(raion) == alert_type:
            continue
        try:
            if chat_id:
                await send_photo_safe(app.bot, chat_id, "images/Alarm.jpg")
                alert_text = ALERT_TYPES_UA.get(alert_type, alert_type)
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=f"🚨 *{raion}* — *{alert_text}*",
                    parse_mode="Markdown",
                )
            else:
                alert_text = ALERT_TYPES_UA.get(alert_type, alert_type)
                logging.info(f"[НОТИФ] {raion} — {alert_text} (CHAT_ID не задано)")
        except Exception as e:  # noqa: BLE001
            logging.error(f"Помилка при відправці тривоги: {e}")

    # Відбої по районах
    for raion, _old_type in cache.last_alerts.items():
        if raion in new_state:
            continue
        try:
            if chat_id:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=f"✅ Відбій тривоги у *{raion}*",
                    parse_mode="Markdown",
                )
            else:
                logging.info(f"[ОБВІД] Відбій у {raion} (CHAT_ID не задано)")
        except Exception as e:  # noqa: BLE001
            logging.error(f"Помилка при відправці відбою по району: {e}")

    # Загальний відбій по області
    if cache.last_alerts and not new_state:
        try:
            if chat_id:
                await app.bot.send_message(chat_id=chat_id, text=f"✅ Відбій тривоги у {REGION}")
                await send_photo_safe(app.bot, chat_id, "images/Clear.jpg")
            else:
                logging.info(f"[ОБВІД ОБЛАСТІ] Відбій у {REGION} (CHAT_ID не задано)")
        except Exception as e:  # noqa: BLE001
            logging.error(f"Помилка при відправці відбою по області: {e}")

    cache.last_alerts = new_state


# ===== Команда аварійної зупинки (тільки для ADMIN_ID) =====
async def stopbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("⛔️ Ця команда доступна лише адміністратору.")
        return

    await update.message.reply_text("🛑 Отримано команду зупинки. Виконую вимкнення...")
    asyncio.create_task(_shutdown_sequence(context.application))


async def _shutdown_sequence(app):
    logging.info("🔻 Shutdown requested by admin")
    # 1) зупиняємо job_queue
    try:
        app.job_queue.stop()
    except Exception as e:
        logging.debug(f"Проблема під час job_queue.stop(): {e}")

    # 2) коректно зупиняємо додаток
    try:
        await app.shutdown()
    except Exception as e:
        logging.debug(f"Проблема під час app.shutdown(): {e}")
    try:
        await app.stop()
    except Exception as e:
        logging.debug(f"Проблема під час app.stop(): {e}")

    logging.info("⚙️ Бот вимкнено адміністратором. Зупиняю event loop.")
    loop = asyncio.get_event_loop()
    loop.stop()


# ===== Обробка помилок Telegram =====
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error(msg="Виникла помилка у хендлері:", exc_info=context.error)
    if update and hasattr(update, "message") and update.message:
        await update.message.reply_text("⚠️ Виникла внутрішня помилка бота. Спробуйте пізніше.")


# ===== Основний цикл =====
async def main():
    nest_asyncio.apply()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    if DEFAULT_CHAT_ID is not None:
        app.bot_data["chat_id"] = DEFAULT_CHAT_ID
        app.bot_data["default_chat_id"] = DEFAULT_CHAT_ID

    alert_cache = RegionAlertCache()
    app.bot_data["alert_cache"] = alert_cache

    # ===== Хендлери команд і тексту =====
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("stopbot", stopbot))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по області"), oblast_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)як там крим"), krym_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по києву"), kyiv_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по одесі"), odesa_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по франику"), frankivsk_alerts))

    app.add_error_handler(error_handler)

    # ===== Фонові задачі через JobQueue =====
    async def _job_callback(context: ContextTypes.DEFAULT_TYPE):
        cache: RegionAlertCache = context.application.bot_data.setdefault("alert_cache", RegionAlertCache())
        await process_alerts(context.application, cache)

    app.job_queue.run_repeating(_job_callback, interval=POLL_INTERVAL, first=0)

    logging.info("✅ Бот запущено...")
    await app.run_polling(close_loop=False)


# ===== Запуск =====
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.create_task(main())
        loop.run_forever()
    except KeyboardInterrupt:
        logging.info("🛑 Зупинка вручну (KeyboardInterrupt)")
    finally:
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        try:
            loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        except Exception:
            pass
        loop.close()
        logging.info("Loop closed. Exit.")
