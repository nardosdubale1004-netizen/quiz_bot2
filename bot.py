import os
import asyncio
from telegram.ext import Application, CallbackQueryHandler
from src.config import CONFIG, Style
from src.database import QuizEngine
from src.callbacks import handle_callback
from src.cli import admin_panel

# Global Application Orchestrator
engine = QuizEngine()

async def main():
    if not os.path.exists("logs"):
        os.makedirs("logs")

    config = engine.config
    if "token" not in config or "channel" not in config:
        print(f"{Style.RED}CRITICAL: config.json is missing 'token' or 'channel'.{Style.RESET}")
        return

    # Initialize complete async python-telegram-bot application wrapper
    app = Application.builder().token(config["token"]).build()
    
    # Register core modular handlers
    app.add_handler(CallbackQueryHandler(lambda u, c: handle_callback(update=u, context=c, engine=engine)))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    print(f"{Style.GREEN}Quiz Master Pro is online and connected to {config['channel']}.{Style.RESET}")

    try:
        # Run dashboard panel thread loops concurrently with Telegram's pooling scheduler
        await admin_panel(app, engine)
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        print(f"{Style.YELLOW}System successfully shut down.{Style.RESET}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        import sys
        sys.exit(0)