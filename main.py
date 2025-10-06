import os
import asyncio
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# 🔐 Твої токени та налаштування
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")  # токен бота
ALERTS_TOKEN = os.getenv("ALERTS_TOKEN")  # токен alerts.in.ua
REGION = os.getenv("REGION", "Київська область")  # за замовчуванням Київська область
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 25))  # інтервал опитування API

API_URL = "https://api.alerts.in.ua/v1/alerts/active.json"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Привіт 🌸\nНапиши «Що по області» щоб дізнатись, де зараз тривога у {REGION}."
    )

async def oblast_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
    try:
        response = requests.get(API_URL, headers=headers, timeout=10)
        data = response.json()

        region_alerts = [
            alert for alert in data.get("alerts", [])
            if alert.get("location_oblast") == REGION
        ]

        if not region_alerts:
            await update.message.reply_text(f"✅ У {REGION} зараз тихо.")
            return

        text = f"🚨 *Активні тривоги у {REGION}:*\n"
        for alert in region_alerts:
            raion = alert.get("location_title", "Невідомий район")
            alert_type = alert.get("alert_type", "невідомо")
            text += f"• {raion} — {alert_type}\n"

        await update.message.reply_markdown(text)

    except Exception as e:
        await update.message.reply_text(f"Помилка отримання даних: {e}")

async def poll_alerts(app):
    """Перевірка тривог у REGION кожні POLL_INTERVAL секунд"""
    headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
    while True:
        try:
            response = requests.get(API_URL, headers=headers, timeout=10)
            data = response.json()
            region_alerts = [
                alert for alert in data.get("alerts", [])
                if alert.get("location_oblast") == REGION
            ]

            if region_alerts:
                text = f"🚨 *Активні тривоги у {REGION}:*\n"
                for alert in region_alerts:
                    raion = alert.get("location_title", "Невідомий район")
                    alert_type = alert.get("alert_type", "невідомо")
                    text += f"• {raion} — {alert_type}\n"

                # Надіслати у всі чат-и, де бот активний
                for chat_id in app.bot_data.get("chats", []):
                    await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")

        except Exception as e:
            print(f"Помилка при опитуванні API: {e}")

        await asyncio.sleep(POLL_INTERVAL)

async def register_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Зберігаємо chat_id для автоматичних повідомлень"""
    chat_id = update.effective_chat.id
    if "chats" not in context.application.bot_data:
        context.application.bot_data["chats"] = set()
    context.application.bot_data["chats"].add(chat_id)

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по області"), oblast_alerts))
    app.add_handler(MessageHandler(filters.TEXT, register_chat))  # щоб додавати нові чати

    print("✅ Бот запущено...")

    # Запускаємо фоновий таск для опитування API
    app.job_queue.run_repeating(lambda ctx: asyncio.create_task(poll_alerts(app)), interval=POLL_INTERVAL, first=5)

    app.run_polling()

if __name__ == "__main__":
    main()
