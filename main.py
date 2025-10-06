import os
import asyncio
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# 🔐 Твої токени та налаштування
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")  # токен бота
ALERTS_TOKEN = os.getenv("ALERTS_TOKEN")  # токен alerts.in.ua
REGION = os.getenv("REGION", "Київська область")  # регіон для перевірки
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 25))  # інтервал опитування API
CHAT_ID = int(os.getenv("CHAT_ID", 177475616))  # фіксований чат

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

                # Надсилаємо тільки в фіксований чат
                await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")

        except Exception as e:
            print(f"Помилка при опитуванні API: {e}")

        await asyncio.sleep(POLL_INTERVAL)


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Обробники команд та повідомлень
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по області"), oblast_alerts))

    print("✅ Бот запущено...")

    # Запускаємо фоновий таск через asyncio
    asyncio.create_task(poll_alerts(app))

    app.run_polling()


if __name__ == "__main__":
    main()
