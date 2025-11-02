import os
import json
import asyncio
import aiohttp

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
)
from config import *

async def get_api_data():
    headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
    async with aiohttp.ClientSession() as s:
        async with s.get(API_URL, headers=headers, timeout=10) as r:
            return await r.json()

async def startbot_command(update, ctx):
    """Пуск і коротке зведення актуальних тривог адміну."""
    ctx.application.bot_data["chat_id"] = update.effective_chat.id
    await update.message.reply_text("Привіт 🌸 KytsjaAlarm запущено.\n\
            Отримую поточні тривоги...")

    data = await get_api_data()
    alerts = data.get("alerts", []) or []
    if not alerts:
        msg = "✅ Зараз по всій Україні спокійно."
    else:
        lines = []
        for a in alerts:
            t = a.get("alert_type") or "air_raid"
            lines.append(
                f"🚨 {a.get('location_oblast')} — {a.get('location_title')}: "
                f"{ALERT_TYPES_UA.get(t, 'Повітряна тривога!')}"
            )
        msg = "🗺 <b>Актуальні тривоги:</b>\n" + "\n".join(lines)

    await ctx.bot.send_message(chat_id=ADMIN_ID, text=msg, parse_mode="HTML")

    if update.effective_chat.type in ("group", "supergroup"):
        await update.message.reply_text("✅ Бот активний. Моніторю Київську область.")

async def stopbot_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
        print("🛑 Бот зупинено адміністратором.")

    except Exception as e:
        # Обробляємо помилки, якщо не вдалося коректно вимкнутися
        print(f"Помилка при зупинці: {e}")

    # Примусово завершуємо процес, щоб вийти з loop.run_forever()
    os._exit(0)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🧭 <b>Команди KytsjaAlarm Bot</b>\n\n"
        "📍 <b>Основні:</b>\n"
        "<code>/start</code> — запустити бота або перевірити стан\n"
        "<code>/help</code> — показати цей список команд\n"
        "<code>/stop</code> — зупинити бота (адміністратор)\n\n"
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

async def listregions_command(update, ctx):
    await update.message.reply_text("⏳ Отримую список областей...")

    data = await get_api_data()
    regs = sorted(set(a.get("location_oblast") for a in (
        data.get("alerts", []) or []) if a.get("location_oblast")))
    txt = "🧭 Список областей, які бачить API:\n\n" + "\n".join(
            f"• {r}" for r in regs) if regs else "❌ API не повернуло даних."

    await update.message.reply_text(txt)

async def exportdict_command(update, ctx):
    if update.effective_user.id != ADMIN_ID:
        return
    data = ctx.application.bot_data.get("locations_dict", {})
    await update.message.reply_text(
            f"<pre>{json.dumps(data, ensure_ascii=False, indent=2)}</pre>",
            parse_mode="HTML")

