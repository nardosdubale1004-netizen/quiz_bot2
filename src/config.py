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
ADMIN_BOOTSTRAP_SECRET = os.getenv("ADMIN_BOOTSTRAP_SECRET")

ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.strip().isdigit()] \
    or [int(x) for x in local_config.get("admin_ids", [])]

FEEDBACK_CATEGORIES = {
    "bug": "🐛 Bug / Something's Broken",
    "feature": "💡 Feature Request",
    "confusing": "❓ Confusing / Unclear",
    "general": "⭐ General Feedback",
}

FEEDBACK_STATUS_LABELS = {
    "open": "🆕 Open",
    "in_progress": "🔧 In Progress",
    "planned": "🗓️ Planned Next Update",
    "resolved": "✅ Resolved",
    "wontfix": "🚫 Not Planned",
}

# --- Shared In-Memory Tracking for Lockout States ---
LOCKOUT_MESSAGES = set()
FEEDBACK_NOTICE_MIDS = {}
NO_ANSWER_NUDGE_MIDS = {}
LAST_UTILITY_MID = {} 
SHUTTING_DOWN = False

# --- Central Application State Hooks for Graceful Shutdowns ---
ACTIVE_LOOP = None
ACTIVE_APP = None
ACTIVE_ENGINE = None

# --- In-Memory FSM State Registries ---
USER_STATES = {}       # Maps user_id -> Active State String
USER_PAYLOADS = {}     # Maps user_id -> Temporary Session Dictionary dict