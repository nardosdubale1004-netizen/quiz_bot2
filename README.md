---
title: Quiz Master Pro
emoji: 🎓
colorFrom: blue
colorTo: indigo
sdk: gradio
sdk_version: 4.44.0
app_file: app.py
pinned: false
---


🎓 Quiz Master Pro: Advanced Telegram Quiz Engine

Quiz Master Pro is a professional-grade Telegram bot designed for educational
channels. It features a dual-rendering engine that converts complex LaTeX and
TikZ code into high-definition images, providing a "Premium UI" experience for
students.

🚀 Key Features

  - Premium Math Rendering: Automatic detection of TikZ code for professional
    geometric graphs and LaTeX for standard formulas.
  - Internal Sidecar Architecture: Uses a local Kroki Docker container. The bot
    fetches images internally to bypass Telegram network restrictions.
  - Interactive CLI Dashboard: Manage your entire channel via a terminal
    interface (powered by prompt_toolkit).
  - Dynamic Quiz Management:
      - Open/Close: Toggle quizzes between "Interactive Mode" and "Static View"
        (with spoiler-protected answers).
      - Live Sync: The clean command synchronizes your local database with the
        channel, identifying and marking deleted posts.
      - Range Sending: Bulk-send questions using ranges (e.g., 1, 3-5, 10).
  - Deep Analysis: Every question includes a logic explanation and an
    option-by-option breakdown.

📂 Project Structure

quiz_bot/
├── bot.py                # Main Bot Engine & Interactive CLI
├── config.json           # API Keys & Channel Settings
├── docker-compose.yml    # Orchestration (Bot + Kroki Renderer)
├── requirements.txt      # Dependencies (python-telegram-bot, httpx, etc.)
├── questions/            # JSON database (organized by subject)
│   ├── mathematics.json
│   ├── biology.json
│   └── english.json
└── logs/                 # Persistent tracking of sent quizzes & stats

🛠️ Installation & Setup

1. Prerequisites

  - Docker and Docker Compose installed.
  - A Telegram Bot Token from @BotFather.
  - A Telegram Channel where the bot is an Administrator.

2. Configuration

Edit config.json in the root folder:

{
  "token": "YOUR_BOT_TOKEN",
  "channel": "@YourChannelUsername"
}

3. Launching the System

Build and start the containers in detached mode:

docker-compose up -d --build

🖥️ Using the Admin Dashboard

Access the interactive control panel by attaching to the container:

docker attach quiz_bot

Dashboard Commands:

  - [1] Native Poll: Sends a standard Telegram Quiz (text-only).
  - [2] Premium UI: Renders LaTeX/TikZ into an image and sends it with
    interactive Inline Buttons.
  - [3] Manage Quizzes:
      - sw: Switch view between Active and Closed quizzes.
      - ft: Filter list by type (nap for Native, prp for Premium).
      - clean: Hard-sync with the channel to find deleted messages.
      - [index]: Toggle a specific quiz (Close an active one or Re-open a closed
        one).
      - all: Bulk-close all active quizzes on the current page.

📂 Adding Questions (JSON Format)

Add `.json` files to the `questions/` folder. For all formulas, use Telegram's advance rich text math format:

```json
{
  "id": "MATH-GEO-001",
  "subject": "Mathematics",
  "topic": "Geometry",
  "tags": ["circle", "angles"],
  "question": "Find the value of angle <tg-math>\\theta</tg-math> in the unit circle.",
  "latex": "\\begin{tikzpicture} ... TikZ Code ... \\end{tikzpicture}",
  "options": [
    "<tg-math>30^\\circ</tg-math>",
    "<tg-math>45^\\circ</tg-math>",
    "<tg-math>60^\\circ</tg-math>",
    "<tg-math>90^\\circ</tg-math>"
  ],
  "correct_option": 2,
  "poll_explanation": {
    "rule": "Arc Sine Identity: <tg-math-block>\\sin\\theta = y</tg-math-block>",
    "why": "The y-coordinate at this point is <tg-math>0.866 = \\frac{\\sqrt{3}}{2}</tg-math>, which corresponds to <tg-math>\\sin(60^\\circ)</tg-math>."
  },
  "options_analysis": [
    {"why": "Incorrect", "example": "<tg-math>\\sin(30^\\circ) = 0.5</tg-math>"},
    {"why": "Incorrect", "example": "<tg-math>\\sin(45^\\circ) = 0.707</tg-math>"},
    {"why": "Correct", "example": "<tg-math>\\sin(60^\\circ) = 0.866</tg-math>"},
    {"why": "Incorrect", "example": "<tg-math>\\sin(90^\\circ) = 1</tg-math>"}
  ]
}

```
🧠 Rendering Engines

1.  Local Kroki (Port 8000): Used for tikzpicture. It produces high-definition
    vector-like PNGs for geometry and functions.
2.  CodeCogs (Cloud): Used for standard mathematical formulas to ensure fast
    delivery and minimal local overhead.

🔧 Maintenance Commands

| Action                       | Command                            |
| :--------------------------- | :--------------------------------- |
| **Start Services**           | `docker-compose up -d`             |
| **Stop Services**            | `docker-compose stop`              |
| **View Bot Logs**            | `docker-compose logs -f bot`       |
| **Update Code/Dependencies** | `docker-compose up -d --build bot` |
| **Wipe & Reset**             | `docker-compose down -v`           |

⚠️ Troubleshooting

  - "Wrong remote file identifier": Fixed in current version. The bot now
    fetches image bytes internally via httpx and uploads them directly to
    Telegram.
  - Dashboard not appearing: After running docker attach, press Enter or type c
    to refresh the terminal UI.
  - Images failing to render: Ensure the quiz_renderer container is running
    (docker-compose ps).
# questions
