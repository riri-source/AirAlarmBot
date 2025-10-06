import os
import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# 🔐 Твої токени та налаштування
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")  # токен бота
ALERTS_TOKEN = os.getenv("ALERTS_TOKEN")  # токен alerts.in.ua
REGION = os.getenv("REGION", "Київська область")  # регіон для перевірки
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 25))  # інтервал опитування API
CHAT_ID = int(os.getenv("CHAT_ID", 177475616))  # конкретний чат для повідомлень

API_URL = "https://api.alerts.in.ua/v1/alerts/active.json"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id == CHAT_ID or update.effective_chat.type == "private":
        await update.message.reply_text(
            f"Привіт 🌸\nНапиши «Що по області» щоб дізнатись, де зараз тривога у {REGION}."
        )


async def oblast_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Відповідаємо лише у приваті або в CHAT_ID
    if update.effective_chat.id != CHAT_ID and update.effective_chat.type != "private":
        return

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


async def poll_alerts(context: ContextTypes.DEFAULT_TYPE):
    """Перевірка тривог у REGION кожні POLL_INTERVAL секунд і надсилання у конкретний чат"""
    headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
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

            await context.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")

    except Exception as e:
        print(f"Помилка при опитуванні API: {e}")


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Обробники команд та повідомлень
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по області"), oblast_alerts))

    # Фоновий таск для опитування API
    app.job_queue.run_repeating(poll_alerts, interval=POLL_INTERVAL, first=5)

    print("✅ Бот запущено...")
    app.run_polling()


if __name__ == "__main__":
    main()
