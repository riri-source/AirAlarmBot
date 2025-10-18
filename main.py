#!/usr/bin/env python3
import os
import asyncio
import logging
import time
from dataclasses import dataclass, field
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, Optional
from datetime import datetime

import aiohttp
import nest_asyncio
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ================== Конфіг та ініціалізація ==================
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BOT_TOKEN = os.getenv("BOT_TOKEN")
ALERTS_TOKEN = os.getenv("ALERTS_TOKEN")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "25"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHAT_ID_ENV = os.getenv("CHAT_ID")
DEFAULT_CHAT_ID = int(CHAT_ID_ENV) if CHAT_ID_ENV else None

if not BOT_TOKEN or not ALERTS_TOKEN:
    raise RuntimeError("Не задано BOT_TOKEN або ALERTS_TOKEN у змінних оточення.")

API_URL = "https://api.alerts.in.ua/v1/alerts/active.json"

# НАЗВИ, які ми моніторимо у МРЧ (жорстко)
MRC_OBLASTS = ("Київська область", "м. Київ")

# Словник human-friendly типів
ALERT_TYPES_UA = {
    "air_raid": "Повітряна тривога!",
    "chemical": "Хімічна тривога",
    "radiation": "Радіаційна тривога",
    "other": "Інша тривога",
}

# ================== Простий healthcheck HTTP (опційно) ==================
class StubHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_http_server():
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), StubHandler)
    server.serve_forever()

Thread(target=run_http_server, daemon=True).start()

# ================== Класи/структури ==================
@dataclass
class RegionAlertCache:
    last_alerts: Dict[str, str] = field(default_factory=dict)
    initialized: bool = False

# ================== Хелпери ==================
def get_chat_id(app) -> Optional[int]:
    cid = app.bot_data.get("chat_id") or app.bot_data.get("default_chat_id")
    return int(cid) if cid else None

async def send_photo_safe(bot, chat_id: Optional[int], path: str) -> bool:
    if not chat_id:
        return False
    try:
        with open(path, "rb") as f:
            await bot.send_photo(chat_id=chat_id, photo=f)
        return True
    except FileNotFoundError:
        logging.warning("send_photo_safe: файл не знайдено: %s", path)
    except Exception as e:
        logging.debug("send_photo_safe помилка: %s", e)
    return False

# ================== Запити до API ==================
async def _get_api_data_with_retries(attempts: int = 3, timeout: int = 10):
    headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
    delay = 0.5
    for attempt in range(1, attempts + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(API_URL, headers=headers, timeout=timeout, params={"_": int(time.time())}) as resp:
                    status = resp.status
                    data = await resp.json()
                    return data
        except Exception as e:
            logging.warning("API request attempt %d/%d failed: %s", attempt, attempts, e)
            if attempt < attempts:
                await asyncio.sleep(delay)
                delay *= 2
            else:
                logging.error("API request failed after %d attempts.", attempts)
                return {}

# ------------------ МРЧ: Київська область + м. Київ ------------------
async def fetch_region_alerts():
    """
    Повертає list(alert) тільки ті, де location_oblast належить MRC_OBLASTS.
    Це функція, яку викликає МРЧ — вона жорстка, без нормалізацій.
    """
    data = await _get_api_data_with_retries()
    alerts = []
    for a in data.get("alerts", []):
        ob = a.get("location_oblast")
        if ob in MRC_OBLASTS:
            alerts.append(a)
    logging.debug("fetch_region_alerts -> %d", len(alerts))
    return alerts

# ------------------ Ручні точні запити (без нормалізації) ------------------
async def fetch_location_alerts(location_name: str):
    """
    Жорстке порівняння: шукаємо записи, де location_title == location_name
    або location_oblast == location_name.
    (Ця функція використовується для ручних команд — Крим, Луганська, Чернігівська, Одеса і т.д.)
    """
    data = await _get_api_data_with_retries()
    results = []
    for a in data.get("alerts", []):
        if a.get("location_title") == location_name or a.get("location_oblast") == location_name:
            results.append(a)
    logging.info("fetch_location_alerts('%s') -> %d", location_name, len(results))
    return results

# ================== Виведення результатів для ручних запитів ==================
async def show_alerts_for_update(update: Update, context: ContextTypes.DEFAULT_TYPE, alerts, label: str):
    if not alerts:
        await update.message.reply_text(f"✅ У {label} зараз все чисто.")
        await send_photo_safe(context.application.bot, update.effective_chat.id, "images/Saefty.jpg")
        return

    text = f"🚨 *У {label} зафіксована тривога:* \n"
    for a in alerts:
        title = a.get("location_title") or a.get("location_oblast") or "Невідомо"
        alert_type = ALERT_TYPES_UA.get(a.get("alert_type"), a.get("alert_type"))
        text += f"• {title} — {alert_type}\n"
    await update.message.reply_markdown(text)

# ================== Telegram хендлери (ручні) ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    context.application.bot_data["chat_id"] = cid
    await update.message.reply_text(
        "Привіт 🌸\n"
        "МРЧ моніторить Київську область + м.Київ в реальному часі.\n"
        "Можеш запитати вручну: «як там Крим», «що по Луганській», «що по Чернігівській», «що по Одесі», «що по франику»."
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cache: RegionAlertCache = context.application.bot_data.get("alert_cache") or RegionAlertCache()
    keys = ", ".join(cache.last_alerts.keys()) or "—"
    cid = get_chat_id(context.application)
    await update.message.reply_text(
        f"ℹ️ МРЧ: Київська область + м.Київ\n"
        f"CHAT_ID: {cid or 'нема'}\n"
        f"POLL_INTERVAL: {POLL_INTERVAL}s\n"
        f"Активні райони: {keys}"
    )

# Ручні команди — жорсткі назви (без нормалізації)
async def krym_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alerts = await fetch_location_alerts("Автономна Республіка Крим")
    await show_alerts_for_update(update, context, alerts, "Крим")

async def lugansk_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alerts = await fetch_location_alerts("Луганська область")
    await show_alerts_for_update(update, context, alerts, "Луганській області")

async def chernihiv_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alerts = await fetch_location_alerts("Чернігівська область")
    await show_alerts_for_update(update, context, alerts, "Чернігівській області")

async def odesa_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alerts = await fetch_location_alerts("м. Одеса")
    await show_alerts_for_update(update, context, alerts, "м. Одеса")

async def frankivsk_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alerts = await fetch_location_alerts("м. Івано-Франківськ")
    await show_alerts_for_update(update, context, alerts, "м. Івано-Франківськ")

async def kyiv_manual_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # якщо хочеш ручний запит по м. Київ:
    alerts = await fetch_location_alerts("м. Київ")
    await show_alerts_for_update(update, context, alerts, "м. Київ")

# ================== МРЧ: логіка процесу одного тіку ==================
async def process_alerts(app, cache: RegionAlertCache):
    """
    Одне виконання МРЧ: отримує alerts для Київської області + м.Київ,
    порівнює з cache.last_alerts (ключ = location_title), і розсилає:
      - повідомлення про появу тривоги в районі (з фото Alarm.jpg)
      - повідомлення про відбій у районі
      - якщо після оновлення залишилось 0 районів — загальний відбій + фото Clear.jpg
    """
    tick = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info("⏱ МРЧ перевірка @ %s", tick)

    alerts = await fetch_region_alerts()
    # Формуємо new_state: ключ = location_title (людська назва), value = alert_type
    new_state: Dict[str, str] = {}
    for a in alerts:
        title = a.get("location_title") or a.get("location_oblast") or "Невідомий район"
        new_state[title] = a.get("alert_type", "unknown")

    logging.debug("МРЧ new_state keys: %s", list(new_state.keys()))
    logging.debug("МРЧ last_alerts keys: %s", list(cache.last_alerts.keys()))

    # Ініціалізація (перший запуск) — не шлемо повідомлень
    if not cache.initialized:
        cache.last_alerts = new_state
        cache.initialized = True
        logging.info("МРЧ ініціалізовано (без сповіщень).")
        return

    chat_id = get_chat_id(app)
    # Якщо чат не заданий — логування і пропуск відправки
    if chat_id is None:
        logging.info("МРЧ: CHAT_ID не задано — пропускаємо відправки.")
        cache.last_alerts = new_state
        return

    # Виявляємо стартові і кінцеві райони
    started = []
    ended = []

    # Нові або змінені (якщо тип тривоги змінився)
    for r, t in new_state.items():
        old_t = cache.last_alerts.get(r)
        if old_t != t:
            # якщо раніше не було — це старт; якщо був інший тип — теж вважаємо як "оновлення"
            if old_t is None:
                started.append((r, ALERT_TYPES_UA.get(t, t)))
            else:
                # зміна типу теж вписуємо як "оновлення" (вважаємо сповіщення)
                started.append((r, ALERT_TYPES_UA.get(t, t)))

    # Відбої — райони, що були у last_alerts, а в new_state їх немає
    for r in list(cache.last_alerts.keys()):
        if r not in new_state:
            ended.append(r)

    # Якщо є запуски — надсилаємо картинку Alarm (один раз) і повідомлення
    if started:
        await send_photo_safe(app.bot, chat_id, "images/Alarm.jpg")

    # Надсилаємо зведення: перші старти, потім відбої
    messages = []
    if started:
        messages.append("🚨 *Нові/змінені тривоги:*")
        for r, text in started:
            messages.append(f"• *{r}* — {text}")
    if ended:
        if messages:
            messages.append("")  # рядок розділення
        messages.append("✅ *Відбій у районах:*")
        for r in ended:
            messages.append(f"• {r}")

    if messages:
        try:
            await app.bot.send_message(chat_id=chat_id, text="\n".join(messages), parse_mode="Markdown")
        except Exception as e:
            logging.error("Помилка при відправці повідомлення МРЧ: %s", e)

    # Якщо після оновлення НЕМАЄ жодного активного району — загальний відбій по області + картинка
    if cache.last_alerts and not new_state:
        try:
            await app.bot.send_message(chat_id=chat_id, text=f"✅ Відбій тривоги у Київській області")
            await send_photo_safe(app.bot, chat_id, "images/Clear.jpg")
        except Exception as e:
            logging.error("Помилка при відправці загального відбою: %s", e)

    # Оновлюємо кеш
    cache.last_alerts = new_state

# ================== Адмін-команди: зупинка ==================
async def stopbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔️ Ця команда доступна лише адміністратору.")
        return
    await update.message.reply_text("🛑 Отримано команду зупинки. Виконую вимкнення...")
    asyncio.create_task(_shutdown_sequence(context.application))

async def _shutdown_sequence(app):
    logging.info("🔻 Shutdown requested by admin")
    try:
        app.job_queue.stop()
    except Exception:
        pass
    try:
        await app.shutdown()
    except Exception:
        pass
    try:
        await app.stop()
    except Exception:
        pass
    logging.info("⚙️ Бот вимкнено адміністратором. Зупиняю event loop.")
    loop = asyncio.get_event_loop()
    loop.stop()

# ================== Основний цикл ==================
async def main():
    nest_asyncio.apply()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Відновлюємо chat_id, якщо заданий у ENV
    if DEFAULT_CHAT_ID:
        app.bot_data["chat_id"] = DEFAULT_CHAT_ID
        app.bot_data["default_chat_id"] = DEFAULT_CHAT_ID

    cache = RegionAlertCache()
    app.bot_data["alert_cache"] = cache

    # ================== Хендлери ==================
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("stopbot", stopbot))

    # Ручні текст-запити (жорсткі)
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)як там крим"), krym_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по луган"), lugansk_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по черніг"), chernihiv_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по одесі"), odesa_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по франику"), frankivsk_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по києву"), kyiv_manual_alerts))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по області"), lambda u, c: asyncio.create_task(oblast_manual_handler(u, c))))

    # Обробка помилок (базова)
    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logging.error("Виникла помилка у хендлері:", exc_info=context.error)
        if update and hasattr(update, "message") and update.message:
            try:
                await update.message.reply_text("⚠️ Виникла внутрішня помилка. Спробуйте пізніше.")
            except Exception:
                pass

    app.add_error_handler(error_handler)

    # ================== МРЧ JobQueue ==================
    async def _job(context: ContextTypes.DEFAULT_TYPE):
        await process_alerts(context.application, cache)

    app.job_queue.run_repeating(_job, interval=POLL_INTERVAL, first=0)

    logging.info("✅ Бот запущено — МРЧ: Київська область + м. Київ")
    await app.run_polling(close_loop=False)

# невелика обгортка: ручний хендлер для "що по області" (показує точні alert для Київської області по запиту)
async def oblast_manual_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alerts = await fetch_location_alerts("Київська область")
    await show_alerts_for_update(update, context, alerts, "Київська області")

# ================== Старт програми ==================
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
