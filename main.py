async def stopbot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔️ Лише адміністратор.")
        return
    
    await update.message.reply_text("🛑 Зупиняю роботу...")
    
    try:
        # 1. Зупиняємо JobQueue
        if ctx.application.job_queue:
            await ctx.application.job_queue.stop()
            
        # 2. Викликаємо основний метод завершення роботи. 
        # Він закриє всі внутрішні підключення і цикл поллінгу.
        await ctx.application.shutdown() 
        
        # 3. Надсилаємо підтвердження
        await update.message.reply_text("✅ KytsjaAlarm повністю зупинено.")
        logging.info("🛑 Бот зупинено адміністратором.")
        
    except Exception as e:
        # Обробляємо помилки, якщо не вдалося коректно вимкнутися
        logging.error(f"Помилка при зупинці: {e}")
        await update.message.reply_text(f"⚠️ Не вдалося завершити повністю: {e}")
        
    # 4. Примусово завершуємо процес, щоб вийти з loop.run_forever()
    os._exit(0)
