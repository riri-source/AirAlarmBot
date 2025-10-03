import os
from telegram import Bot
from telegram.ext import CommandHandler, ApplicationBuilder

# --- Встав сюди свій токен ---
TOKEN = "8227778859:AAHbbt6aRNrHw-nOSLXKAU2W8DTs6HdbbCM"
CHAT_ID = 177475616  # твій chat_id

bot = Bot(token=TOKEN)

# Команди для тесту
async def alarm(update, context):
    bot.send_photo(chat_id=CHAT_ID, photo=open("images/alarm.jpg", "rb"))

async def clear(update, context):
    bot.send_photo(chat_id=CHAT_ID, photo=open("images/clear.jpg", "rb"))

app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler("alarm", alarm))
app.add_handler(CommandHandler("clear", clear))

print("Bot is running...")
app.run_polling()