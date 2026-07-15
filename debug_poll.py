import asyncio, json
from telegram import Poll
from telegram.ext import Application

config = json.load(open("config.json"))

CANDIDATES = [
    ("1. raw \\n\\n",           "Topic\n\nQuestion\n\nTags"),
    ("2. \\u200b",              "Topic\n\nQuestion\n\u200b\nTags"),
    ("3. \\u200c",              "Topic\n\nQuestion\n\u200c\nTags"),
    ("4. \\u200d",              "Topic\n\nQuestion\n\u200d\nTags"),
    ("5. \\u2060",              "Topic\n\nQuestion\n\u2060\nTags"),
    ("6. \\uFEFF (BOM)",        "Topic\n\nQuestion\n\uFEFF\nTags"),
    ("7. \\u00A0 (nbsp)",       "Topic\n\nQuestion\n\u00A0\nTags"),
    ("8. ┈┈┈┈┈┈",              "Topic\n\nQuestion\n┈┈┈┈┈┈\nTags"),
    ("9. space only",           "Topic\n\nQuestion\n \nTags"),
    ("10. dot ·",               "Topic\n\nQuestion\n·\nTags"),
    ("11. ⠀ (braille blank)",   "Topic\n\nQuestion\n⠀\nTags"),
    ("12. ​ (\\u200b x3)",      "Topic\n\nQuestion\n\u200b\u200b\u200b\nTags"),
]

async def main():
    app = Application.builder().token(config["token"]).build()
    await app.initialize(); await app.start(); await app.updater.start_polling()

    for label, poll_q in CANDIDATES:
        try:
            await app.bot.send_poll(
                chat_id=config["channel"],
                question=poll_q,
                options=["Option A", "Option B"],
                type=Poll.QUIZ,
                correct_option_id=0,
                is_anonymous=True,
            )
            print(f"✅ Sent: {label}")
        except Exception as e:
            print(f"❌ Failed: {label} → {e}")
        await asyncio.sleep(1)

    await app.updater.stop(); await app.stop(); await app.shutdown()

asyncio.run(main())
