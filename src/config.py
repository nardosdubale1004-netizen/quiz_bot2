import os
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

# Read configurations securely from environment variables
CONFIG = {
    "token": os.getenv("BOT_TOKEN"),
    "channel": os.getenv("CHANNEL_ID"),
    "database_url": os.getenv("DATABASE_URL"),
    "kroki_url": os.getenv("KROKI_URL", "https://kroki.io")
}