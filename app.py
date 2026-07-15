# app.py
import os
import asyncio
import threading
import gradio as gr
from dotenv import load_dotenv

# Load environmental variables from your local .env if running tests
load_dotenv()

# Import the main runner loop from your bot.py
from bot import main as run_telegram_bot

def run_bot_in_background():
    # Creates and executes the bot's asynchronous event loop in a daemon thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_telegram_bot())

# Spawn the Telegram bot as a persistent background process
threading.Thread(target=run_bot_in_background, daemon=True).start()

# Build a minimal, beautiful Gradio webpage to satisfy Hugging Face's requirements
with gr.Blocks() as demo:
    gr.Markdown("# 🎓 Quiz Master Pro")
    gr.Markdown("The advanced TikZ WGI Rendering Engine & Telegram Bot is running 24/7.")

# Gradio automatically binds to port 7860 to keep the container active
demo.launch(server_name="0.0.0.0", server_port=7860)