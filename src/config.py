# src/config.py
import os
import json
from dotenv import load_dotenv

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

# Read configurations with environment variables taking priority
CONFIG = {
    "token": os.getenv("BOT_TOKEN") or local_config.get("token"),
    "channel": os.getenv("CHANNEL_ID") or local_config.get("channel"),
    "database_url": os.getenv("DATABASE_URL") or local_config.get("database_url"),
    "kroki_url": os.getenv("KROKI_URL") or local_config.get("kroki_url") or "https://kroki.io",
    "disable_native_rich_messages": os.getenv("DISABLE_NATIVE_RICH_MESSAGES", "False").lower() in ("true", "1", "yes") or local_config.get("disable_native_rich_messages", False)
}

LOCKOUT_MESSAGES = set()