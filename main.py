import os
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer

class StubHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_http_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), StubHandler)
    server.serve_forever()

Thread(target=run_http_server, daemon=True).start()

# 🔐 Твої токени та налаштування
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
ALERTS_TOKEN = os.getenv("ALERTS_TOKEN")
REGION = os.getenv("REGION", "Київська область")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 25))
CHAT_ID = int(os.getenv("CHAT_ID", "177475616"))

if not TELEGRAM_TOKEN or not ALERTS_TOKEN or not CHAT_ID:
    raise RuntimeError("Не задано одну або кілька обов'язкових змінних оточення: BOT_TOKEN, ALERTS_TOKEN, CHAT_ID")

API_URL = "https://api.alerts.in.ua/v1/alerts/active.json"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Привіт 🌸\nНапиши «Що по області» щоб дізнатись, де зараз тривога у {REGION}."
    )


async def oblast_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(API_URL, headers=headers, timeout=10) as resp:
                data = await resp.json()

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
    while True:
        headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(API_URL, headers=headers, timeout=10) as resp:
                    data = await resp.json()

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

                await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")

        except Exception as e:
            print(f"Помилка при опитуванні API: {e}")

        await asyncio.sleep(POLL_INTERVAL)


async def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Обробники команд
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("(?i)що по області"), oblast_alerts))

    # Фоновий таск
    asyncio.create_task(poll_alerts(app))

    print("✅ Бот запущено...")
    await app.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
