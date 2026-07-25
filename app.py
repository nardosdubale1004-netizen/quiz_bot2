# app.py
import os
import sys
import asyncio
import threading
import time
import httpx
import gradio as gr
from dotenv import load_dotenv

# Load environmental variables
load_dotenv()

# Import the main runner loop from your bot.py
from bot import main as run_telegram_bot
from src.database import QuizEngine

def run_bot_in_background():
    """Executes the bot engine in an isolated thread wrapper."""
    try:
        run_telegram_bot()
    except Exception as e:
        print(f"[BACKGROUND ENGINE EXCEPTION]: {e}", file=sys.stderr)

def db_and_web_keep_alive():
    """
    Background worker that runs every 4 minutes to keep the 
    Neon Database active and prevent Render from sleeping.
    """
    # Wait a moment for the system to boot up
    time.sleep(30)
    engine = QuizEngine()
    
    # Get Render URL to ping ourselves (helps prevent Render container sleep)
    public_url = os.getenv("RENDER_EXTERNAL_URL")
    
    while True:
        try:
            # 1. Ping Neon Database (Keeps Neon compute instance active)
            if engine.db_url:
                conn = engine.get_db_connection()
                with conn.cursor() as cur:
                    cur.execute("SELECT 1;")
                    cur.fetchone()
                engine.release_connection(conn)
                print("[KEEP-ALIVE] Neon database pinged successfully.", flush=True)
        except Exception as e:
            print(f"[KEEP-ALIVE ERROR] Database ping failed: {e}", file=sys.stderr)
            
        try:
            # 2. Self-Ping (Keeps the Render web service active if not using external cron)
            if public_url:
                # Use a sync HTTP client to ping the health endpoint
                with httpx.Client() as client:
                    resp = client.get(f"{public_url}/health", timeout=10.0)
                    print(f"[KEEP-ALIVE] Self-ping status: {resp.status_code}", flush=True)
        except Exception as e:
            print(f"[KEEP-ALIVE ERROR] Self-ping failed: {e}", file=sys.stderr)
            
        # Sleep for 4 minutes (240 seconds)
        time.sleep(240)

# Spawn the Telegram bot as a persistent background process
threading.Thread(target=run_bot_in_background, daemon=True).start()

# Spawn the database & web keep-alive thread
threading.Thread(target=db_and_web_keep_alive, daemon=True).start()

# Build a minimal webpage to satisfy Hugging Face's requirements
with gr.Blocks() as demo:
    gr.Markdown("# 🎓 Quiz Master Pro")
    gr.Markdown("The advanced TikZ WGI Rendering Engine & Telegram Bot is running 24/7.")

# Gradio automatically binds to port 7860 to keep the container active
demo.launch(server_name="0.0.0.0", server_port=7860)