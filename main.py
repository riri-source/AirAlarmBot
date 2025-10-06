import os
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# 🔐 Твої токени
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")  # встав свій токен сюди, якщо не через env
ALERTS_TOKEN = os.getenv("ALERTS_TOKEN")  # токен з alerts.in.ua

API_URL = "https://api.alerts.in.ua/v1/alerts/active.json"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привіт 🌸\nНапиши «Що по області» щоб дізнатись, де зараз тривога в Київській області.")

async def oblast_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
    try:
        response = requests.get(API_URL, headers=headers, timeout=10)
        data = response.json()

        kyiv_alerts = [
            alert for alert in data.get("alerts", [])
            if alert.get("location_oblast") == "Київська область"
        ]

        if not kyiv_alerts:
            await update.message.reply_text("✅ У Київській області зараз тихо.")
            return

        text = "🚨 *Активні тривоги у Київській області:*\n"
        for alert in kyiv_alerts:
            raion = alert.get("location_title", "Невідомий район")
            alert_type = alert.get("alert_type", "невідомо")
            text += f"• {raion} — {alert_type}\n"

        await update.message.reply_markdown(text)

    except Exception as e:
        await update.message.reply_text(f"Помилка отримання даних: {e}")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по області"), oblast_alerts))
    print("✅ Бот запущено...")
    app.run_polling()

if __name__ == "__main__":
    main()
