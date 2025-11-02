import os
import asyncio

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
)

from config import *

async def stopbot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔️ Лише адміністратор.")
        return

    await update.message.reply_text("🛑 Зупиняю роботу...")

    try:
        # Надійна зупинка JobQueue
        if ctx.application.job_queue:
            await ctx.application.job_queue.stop()

        # Основний метод завершення роботи (уникає NoneType помилок)
        await ctx.application.shutdown() 

        # Надсилаємо підтвердження
        await update.message.reply_text("✅ KytsjaAlarm повністю зупинено.")
        logging.info("🛑 Бот зупинено адміністратором.")

    except Exception as e:
        # Обробляємо помилки, якщо не вдалося коректно вимкнутися
        logging.error(f"Помилка при зупинці: {e}")
        await update.message.reply_text(f"⚠️ Не вдалося завершити повністю: {e}")

    # Примусово завершуємо процес, щоб вийти з loop.run_forever()
    os._exit(0)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🧭 <b>Команди KytsjaAlarm Bot</b>\n\n"
        "📍 <b>Основні:</b>\n"
        "<code>/start</code> — запустити бота або перевірити стан\n"
        "<code>/help</code> — показати цей список команд\n"
        "<code>/stopbot</code> — зупинити бота (адміністратор)\n\n"
        "📡 <b>Моніторинг і запити:</b>\n"
        "<code>/listregions</code> — показати області, які бачить API\n"
        "<code>/exportdict</code> — показати поточний словник назв (адміністратор)\n\n"
        "🗺 <b>Текстові запити:</b>\n"
        "«що по області» — Київська область\n"
        "«що по Києву» — м. Київ\n"
        "«як там Крим?» — Крим\n"
        "«що по Франику» — Івано-Франківська область\n"
        "«що по &lt;назві&gt;» — будь-який населений пункт зі словника\n\n"
        "📩 Якщо боту невідомий пункт — він запитає, чи надіслати адміну для додавання."
        "\n\n🐾 Версія: KytsjaAlarm v9.3.4 Final"
    )
    await update.message.reply_text(help_text, parse_mode="HTML")
