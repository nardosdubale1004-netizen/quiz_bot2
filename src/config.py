import os
import json

class Style:
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BOLD = '\033[1m'
    MAGENTA = '\033[95m'
    WHITE = '\033[97m'
    RESET = '\033[0m'

# Load config once and share across modules
config_path = "config.json"
if os.path.exists(config_path):
    with open(config_path, "r", encoding="utf-8") as f:
        CONFIG = json.load(f)
else:
    CONFIG = {}