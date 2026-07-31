# src/config.py
import os
import json
from dotenv import load_dotenv

# Load local .env variables during manual development runs
load_dotenv()

class Style:
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BOLD = '\033[1m'
    MAGENTA = '\033[95m'
    WHITE = '\033[97m'
    RESET = '\033[0m'

local_config = {}
if os.path.exists("config.json"):
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            local_config = json.load(f)
    except Exception:
        pass

# Read configurations securely with local fallbacks
CONFIG = {
    "token": os.getenv("BOT_TOKEN") or local_config.get("token"),
    "channel": os.getenv("CHANNEL_ID") or local_config.get("channel"),
    "database_url": os.getenv("DATABASE_URL") or local_config.get("database_url"),
    "kroki_url": os.getenv("KROKI_URL") or local_config.get("kroki_url") or "https://kroki.io"
}

# --- Shared In-Memory Tracking for Lockout States ---
LOCKOUT_MESSAGES = set()
SHUTTING_DOWN = False