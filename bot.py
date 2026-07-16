import os
import sys
import asyncio
from telegram.ext import Application, CallbackQueryHandler, CommandHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from src.config import CONFIG, Style
from src.database import QuizEngine, db_get_user_profile, db_get_weekly_leaderboard
from src.rendering import get_grade_mastery_title
from src.callbacks import handle_callback
from src.cli import admin_panel

# Global Application Orchestrator
engine = QuizEngine()

async def start_command(update: Update, context):
    """Greets the student and launches inline grade selection onboarding."""
    keyboard = [
        [InlineKeyboardButton("🎒 Grade 6", callback_data="set_grade|6"),
         InlineKeyboardButton("🎒 Grade 8", callback_data="set_grade|8")],
        [InlineKeyboardButton("🎒 Grade 10", callback_data="set_grade|10"),
         InlineKeyboardButton("🎒 Grade 12", callback_data="set_grade|12")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "👋 <b>Welcome to Quiz Master Pro!</b>\n\n"
        "To customize your study experience, unlock early bird rewards, and compare "
        "scores inside fair rank tables, select your academic grade level below:",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )

async def leaderboard_command(update: Update, context):
    """Pulls current user's profile and compiles the Grade-Level Weekly Top 10."""
    user_id = update.effective_user.id
    profile = db_get_user_profile(user_id)
    
    if not profile:
        await update.message.reply_text("⚠️ Please register your grade first by typing /start.")
        return
        
    grade = profile['grade']
    user_marks = profile['total_marks']
    mastery = get_grade_mastery_title(user_marks)
    
    # Compile the top 10 weekly responders for this specific grade
    weekly_top = db_get_weekly_leaderboard(grade)
    
    leaderboard_text = [
        f"🏆 <b>GRADE {grade} WEEKLY LEADERBOARD</b> 🏆\n",
        f"🏅 <b>Your Rank Status:</b>",
        f"├─ Mastery Level: <b>{mastery}</b>",
        f"├─ Practice Score: <b>{user_marks} Marks</b>",
        f"└─ Accuracy: <b>{int((profile['correct']/profile['total'])*100) if profile['total'] > 0 else 0}%</b>\n",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🔥 <b>TOP 10 THIS WEEK:</b>"
    ]
    
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    for i, row in enumerate(weekly_top):
        user_label = f"Student {row['user_id'][-4:]}"
        leaderboard_text.append(f" {medals[i]} {user_label} — <b>{row['total_score']} Marks</b>")
        
    leaderboard_text.append("\n━━━━━━━━━━━━━━━━━━━━━━━━")
    leaderboard_text.append(
        "💡 <i>Tip: Slower students can easily reach Gold level by completing exercises daily! "
        "Habitual study builds Mastery.</i>"
    )
    
    await update.message.reply_text("\n".join(leaderboard_text), parse_mode="HTML")

async def main():
    if not os.path.exists("logs"):
        os.makedirs("logs")

    config = engine.config
    token = config.get("token")
    channel = config.get("channel")
    if not token or not channel:
        print(f"{Style.RED}CRITICAL: .env or config is missing BOT_TOKEN or CHANNEL_ID.{Style.RESET}", flush=True)
        return

    # Initialize complete async python-telegram-bot application wrapper
    app = Application.builder().token(token).build()
    
    # Register core command and callback handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    app.add_handler(CallbackQueryHandler(lambda u, c: handle_callback(update=u, context=c, engine=engine)))

    # Render Web Services automatically provide the PORT environment variable
    RENDER_PORT = os.getenv("PORT")

    if RENDER_PORT:
        # --- CLOUD PRODUCTION MODE (WEBHOOKS) ---
        print(f"Starting cloud Webhook listener on port {RENDER_PORT}...", flush=True)
        PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL") 
        
        # Using the standard high-level blocking run_webhook loop for robust port-binding
        app.run_webhook(
            listen="0.0.0.0",
            port=int(RENDER_PORT),
            url_path=token,
            webhook_url=f"{PUBLIC_URL}/{token}",
            drop_pending_updates=True
        )
    else:
        # --- LOCAL DEVELOPMENT/ADMIN MODE (POLLING + CLI) ---
        print("Starting local Polling mode with CLI...", flush=True)
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        print(f"Quiz Master Pro is online and connected to {channel}.", flush=True)
        
        # Run the Admin CLI only if stdin is a real terminal (TTY)
        run_cli = sys.stdin.isatty()
        if run_cli:
            try:
                await admin_panel(app, engine)
            finally:
                await app.updater.stop()
                await app.stop()
                await app.shutdown()
                print(f"System successfully shut down.", flush=True)
        else:
            # Fallback loop if local is run headless
            while True:
                await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        import sys
        sys.exit(0)