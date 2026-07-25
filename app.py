# app.py
import os
import sys
import asyncio
import threading
import gradio as gr
from dotenv import load_dotenv

# Load environmental variables
load_dotenv()

# Import the main runner loop from your bot.py
from bot import main as run_telegram_bot

def run_bot_in_background():
    """Executes the bot engine in an isolated thread wrapper."""
    try:
        run_telegram_bot()
    except Exception as e:
        print(f"[BACKGROUND ENGINE EXCEPTION]: {e}", file=sys.stderr)

# Spawn the Telegram bot as a persistent background process
threading.Thread(target=run_bot_in_background, daemon=True).start()

# Build a minimal, beautiful Gradio webpage to satisfy Hugging Face's requirements
with gr.Blocks() as demo:
    gr.Markdown("# 🎓 Quiz Master Pro")
    gr.Markdown("The advanced TikZ WGI Rendering Engine & Telegram Bot is running 24/7.")

# Gradio automatically binds to port 7860 to keep the container active
demo.launch(server_name="0.0.0.0", server_port=7860)