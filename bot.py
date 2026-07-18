import os
import sys
import json
import asyncio
import threading
from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from src.config import CONFIG, Style
from src.database import (
    QuizEngine,
    db_get_user_profile,
    db_get_weekly_leaderboard,
    db_get_pending_scheduled_question,
    db_mark_question_as_sent,
    process_user_score
)
from src.rendering import get_grade_mastery_title, UIFactory, fetch_kroki_image
from src.callbacks import handle_callback
from src.cli import admin_panel
import httpx
from telegram import Poll
from src.typography import lite_math

# Global Application Orchestrator
engine = QuizEngine()

async def handle_http_request(reader, writer, app):
    """
    Custom lightweight asynchronous web server handler.
    Exposes:
      - GET /health   --> Returns 200 OK (Clean, compliant health check)
      - POST /webhook  --> Receives Telegram Updates and processes them directly via app.process_update()
    """
    try:
        # Read the incoming HTTP request headers
        header_data = await reader.readuntil(b"\r\n\r\n")
        headers = header_data.decode("utf-8")

        # Parse the request line
        request_line = headers.split("\r\n")[0]
        method, path, _ = request_line.split(" ")

        # Extract Content-Length for POST payloads
        content_length = 0
        for line in headers.split("\r\n"):
            if line.lower().startswith("content-length:"):
                content_length = int(line.split(":")[1].strip())
                break

        # 1. Handle GET /health
        if method == "GET" and path == "/health":
            response_body = '{"status": "ok"}'
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(response_body)}\r\n"
                "Connection: close\r\n\r\n"
                f"{response_body}"
            )
            writer.write(response.encode("utf-8"))
            await writer.drain()

        # 2. Handle POST /webhook
        elif method == "POST" and path == "/webhook":
            body_data = await reader.readexactly(content_length)
            body = body_data.decode("utf-8")

            # De-serialize the Telegram Update payload
            update_dict = json.loads(body)
            update = Update.de_json(update_dict, app.bot)

            # Instantly and natively process the update through python-telegram-bot's active handlers
            await app.process_update(update)

            response = "HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            writer.write(response.encode("utf-8"))
            await writer.drain()

        else:
            # 3. Handle 404 Fallback
            response = "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            writer.write(response.encode("utf-8"))
            await writer.drain()

    except Exception as e:
        # 4. Handle 500 Internal Error
        try:
            response = "HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            writer.write(response.encode("utf-8"))
            await writer.drain()
        except Exception:
            pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

async def check_and_publish_scheduled(app):
    """Checks the Neon database for scheduled posts, compiles graphics, and publishes them automatically."""
    q = db_get_pending_scheduled_question()
    if not q:
        return

    print(f"{Style.YELLOW}[SCHEDULER] Found pending scheduled question REF: {q['id']}. Publishing...{Style.RESET}", flush=True)
    channel = CONFIG.get("channel")

    # Track sequence number dynamically
    tracks = engine.db_get_all_tracks()
    last_seq = max((v.get('display_id', 100) for v in tracks.values()), default=100) + 1

    try:
        has_tikz = bool(q.get("latex"))
        if not has_tikz and not UIFactory.is_complex(q["question"]):
            # Native Poll
            poll_hint = UIFactory.replace_code_with_italic(UIFactory.generate_poll_hint(q))
            m = await app.bot.send_poll(
                chat_id=channel,
                question=lite_math(q['question'])[:290],
                options=[lite_math(o)[:90] for o in q['options']],
                type=Poll.QUIZ,
                correct_option_id=q['correct_option'],
                explanation=poll_hint,
                explanation_parse_mode="HTML"
            )
            msg_type = "poll"
            type_str = "native"
        else:
            # Premium Photo UI
            img_url, caption, _ = UIFactory.create_question_assets(q, last_seq)
            kb = UIFactory.build_keyboard(q, last_seq)
            if img_url:
                async with httpx.AsyncClient() as client:
                    resp = await fetch_kroki_image(client, img_url)
                    if resp and resp.status_code == 200:
                        print(f" {Style.GREEN}[SCHEDULER] Solution Sheet compiled successfully. Swapping active image...{Style.RESET}", flush=True)
                        m = await app.bot.send_photo(chat_id=channel, photo=resp.content, caption=caption, reply_markup=kb, parse_mode="HTML")
                        msg_type = "photo"
                        type_str = "premium"
                    else:
                        raise Exception("Kroki failed to compile scheduled asset.")
            else:
                m = await app.bot.send_message(chat_id=channel, text=caption, reply_markup=kb, parse_mode="HTML")
                msg_type = "text"
                type_str = "premium"

        # Register in sent_tracks and mark as sent in questions table
        engine.db_save_track(m.message_id, q['id'], "active", last_seq, type_str, msg_type)
        db_mark_question_as_sent(q['id'])
        print(f"{Style.GREEN}[SCHEDULER] Successfully posted scheduled quiz REF: {last_seq} to channel.{Style.RESET}", flush=True)
    except Exception as e:
        print(f"{Style.RED}[SCHEDULER ERROR] Failed to post scheduled question {q['id']}: {e}{Style.RESET}", flush=True)

async def start_command(update: Update, context):
    """Greets the student and launches inline grade selection onboarding or processes deep-linked quiz answers."""
    user_id = update.effective_user.id
    args = context.args

    # --- DEEP-LINKED QUIZ ANSWER PROCESSING ---
    if args and args[0].startswith("ans_"):
        payload = args[0]
        try:
            _, ref_id, choice_idx_str = payload.split("_")
            display_id = int(ref_id)
            user_selection = int(choice_idx_str)

            # Fetch question data from the Neon database mapping
            tracks = engine.db_get_all_tracks()
            mid_key = next((k for k, v in tracks.items() if k.isdigit() and v.get('display_id') == display_id), None)

            if not mid_key:
                await update.message.reply_text("⚠️ This quiz session has ended or the reference was not found.")
                return

            engine.refresh_database()
            all_qs = {q['id']: q for subject_list in engine.db.values() for q in subject_list}
            question_data = all_qs.get(tracks[mid_key]['q_id'])

            if not question_data:
                await update.message.reply_text("Error: Question data not found.")
                return

            # Evaluate the score privately inside Neon database
            is_correct = (user_selection == question_data['correct_option'])
            perf_card = process_user_score(user_id, mid_key, question_data['id'], is_correct)

            # Generate the comprehensive, context-retaining Answered View
            explanation_html = UIFactory.build_answered_view(question_data, str(display_id), user_selection, compact=False, perf_card=perf_card)

            # If the question originally had an active diagram, send the solution image card!
            has_tikz = UIFactory.has_real_diagram(question_data)
            if has_tikz:
                # Generate a COMPACT caption to stay safely under Telegram's 1024-character caption limit
                explanation_html_compact = UIFactory.build_answered_view(question_data, str(display_id), user_selection, compact=True, perf_card=perf_card)
                latex_code, _ = UIFactory.create_explanation_assets(question_data, user_selection, display_id)
                if latex_code:
                    img_url = UIFactory.get_latex_url(latex_code)
                    async with httpx.AsyncClient() as client:
                        resp = await fetch_kroki_image(client, img_url, latex_code)
                        if resp and resp.status_code == 200:
                            m = await update.message.reply_photo(photo=resp.content, caption=explanation_html_compact, parse_mode="HTML")

                            # Deliver the detailed, un-truncated derivation steps as a separate threaded text reply
                            full_text = UIFactory.build_answered_view(question_data, str(display_id), user_selection, compact=False, perf_card=perf_card, continuation=True)
                            await update.message.reply_text(text=full_text, parse_mode="HTML", reply_to_message_id=m.message_id, disable_web_page_preview=True)
                            return

            # Fallback to pure text message
            await update.message.reply_text(text=explanation_html, parse_mode="HTML", disable_web_page_preview=True)
            return
        except Exception as e:
            print(f" {Style.RED}[ERROR] Failed to process deep-linked answer: {e}{Style.RESET}")
            await update.message.reply_text("⚠️ Failed to load your explanation. Please try again.")
            return

    # --- PERSISTENT PROFILE METRICS GREETER ---
    profile = db_get_user_profile(user_id)
    if profile and profile.get("grade"):
        grade = profile['grade']
        user_marks = profile['total_marks']
        mastery = get_grade_mastery_title(user_marks)
        accuracy = int((profile['correct'] / profile['total']) * 100) if profile['total'] > 0 else 0
        accuracy_bar = "🟩" * (accuracy // 10) + "⬜" * (10 - (accuracy // 10))

        await update.message.reply_text(
            f"👋 <b>Welcome Back, Scholar!</b>\n\n"
            f"Your academic profile is active and fully synchronized.\n\n"
            f"📊 <b>YOUR STUDY METRICS:</b>\n"
            f"├─ Registered Level: <b>Grade {grade}</b>\n"
            f"├─ Practice Score:  <b>{user_marks} Marks</b>\n"
            f"├─ Mastery Level:   <b>{mastery}</b>\n"
            f"├─ Accuracy:        <b>{accuracy}%</b> ({profile['correct']}/{profile['total']})\n"
            f"└─ Progress:         <code>{accuracy_bar}</code>\n\n"
            f"💬 <b>STUDY CHANNELS:</b>\n"
            f"• Check the main channel for active scheduled questions!\n"
            f"• Use the /leaderboard command here to view your rank standings!",
            parse_mode="HTML"
        )
        return

    # --- STANDARD ONBOARDING GRADE SELECTION ---
    keyboard = [
        [InlineKeyboardButton("🎒 Grade 6", callback_data="set_grade|6"),
         InlineKeyboardButton("🎒 Grade 8", callback_data="set_grade|8")],
        [InlineKeyboardButton("🎒 Grade 10", callback_data="set_grade|10"),
         InlineKeyboardButton("🎒 Grade 12", callback_data="set_grade|12")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "👋 <b>Welcome to Quiz Master Pro!</b>\n\n"
        "To customize your study experience, unlock early bird rewards, and compare "
        "scores inside fair rank tables, select your academic grade level below:",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )

async def leaderboard_command(update: Update, context):
    """Pulls current user's profile and compiles the Grade-Level Weekly Top 10."""
    user_id = update.effective_user.id
    profile = db_get_user_profile(user_id)

    if not profile:
        await update.message.reply_text("⚠️ Please register your grade first by typing /start.")
        return

    grade = profile['grade']
    user_marks = profile['total_marks']
    mastery = get_grade_mastery_title(user_marks)

    # Compile the top 10 weekly responders for this specific grade
    weekly_top = db_get_weekly_leaderboard(grade)

    leaderboard_text = [
        f"🏆 <b>GRADE {grade} WEEKLY LEADERBOARD</b> 🏆\n",
        f"🏅 <b>Your Rank Status:</b>",
        f"├─ Mastery Level: <b>{mastery}</b>",
        f"├─ Practice Score: <b>{user_marks} Marks</b>",
        f"└─ Accuracy: <b>{int((profile['correct']/profile['total'])*100) if profile['total'] > 0 else 0}%</b>\n",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🔥 <b>TOP 10 THIS WEEK:</b>"
    ]

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    for i, row in enumerate(weekly_top):
        user_label = f"Student {row['user_id'][-4:]}"
        leaderboard_text.append(f" {medals[i]} {user_label} — <b>{row['total_score']} Marks</b>")

    leaderboard_text.append("\n━━━━━━━━━━━━━━━━━━━━━━━━")
    leaderboard_text.append(
        "💡 <i>Tip: Slower students can easily reach Gold level by completing exercises daily! "
        "Habitual study builds Mastery.</i>"
    )

    await update.message.reply_text("\n".join(leaderboard_text), parse_mode="HTML")

async def run_cloud_server(app, port):
    """Asynchronous runner to configure the webhook target and start the custom HTTP webserver."""
    PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL")
    
    # Set the webhook URL with Telegram manually (pointing to /webhook)
    await app.bot.set_webhook(
        url=f"{PUBLIC_URL}/webhook",
        drop_pending_updates=True
    )
    print(f"Webhook is active on {PUBLIC_URL}/webhook.", flush=True)

    # Spawn a background task loop to check and publish any scheduled questions immediately upon wake-up
    asyncio.create_task(check_and_publish_scheduled(app))

    # Spawn our completely custom, lightweight async web server on port
    server = await asyncio.start_server(
        lambda r, w: handle_http_request(r, w, app),
        "0.0.0.0",
        int(port)
    )
    print(f"Custom light webserver is listening on port {port}.", flush=True)
    
    async with server:
        while True:
            await asyncio.sleep(3600)

def main():
    if not os.path.exists("logs"):
        os.makedirs("logs")

    config = engine.config
    token = config.get("token")
    channel = config.get("channel")
    if not token or not channel:
        print(f"{Style.RED}CRITICAL: .env or config is missing BOT_TOKEN or CHANNEL_ID.{Style.RESET}")
        return

    # Initialize complete async python-telegram-bot application wrapper
    app = Application.builder().token(token).build()
    
    # Register core command and callback handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    app.add_handler(CallbackQueryHandler(lambda u, c: handle_callback(update=u, context=c, engine=engine)))

    # Start polling or webhook services
    RENDER_PORT = os.getenv("PORT")

    if RENDER_PORT:
        # --- CLOUD PRODUCTION MODE (WEBHOOKS) ---
        print(f"Starting cloud Webhook listener on port {RENDER_PORT}...", flush=True)
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # Initialize and start the application so we can make outbound API calls
        loop.run_until_complete(app.initialize())
        loop.run_until_complete(app.start())
        
        # Dynamically register the active bot username so build_keyboard can construct deep links automatically
        bot_info = loop.run_until_complete(app.bot.get_me())
        CONFIG["bot_username"] = bot_info.username
        print(f"Registered Bot Username: @{bot_info.username}", flush=True)

        # Execute the async server loop within the event loop
        try:
            loop.run_until_complete(run_cloud_server(app, RENDER_PORT))
        except KeyboardInterrupt:
            pass
        finally:
            loop.run_until_complete(app.stop())
            loop.run_until_complete(app.shutdown())
            print(f"System successfully shut down.", flush=True)
    else:
        # --- LOCAL DEVELOPMENT/ADMIN MODE (OUTBOUND-ONLY CLI CLIENT) ---
        print("Starting local Admin Dashboard cockpit...", flush=True)
        
        # We initialize and start the application so we can make outbound API calls
        # But we DO NOT start any polling listener! This prevents all webhook/polling conflicts!
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        loop.run_until_complete(app.initialize())
        loop.run_until_complete(app.start())
        
        # Dynamically register the active bot username for local deep-link simulation
        bot_info = loop.run_until_complete(app.bot.get_me())
        CONFIG["bot_username"] = bot_info.username
        print(f"Quiz Master Pro Admin Client is online and connected to {channel}.", flush=True)

        # Run the Admin CLI only if stdin is a real terminal (TTY)
        run_cli = sys.stdin.isatty()
        if run_cli:
            try:
                # Execute the admin panel using a synchronous runner wrapper
                loop.run_until_complete(admin_panel(app, engine))
            except KeyboardInterrupt:
                pass
            finally:
                loop.run_until_complete(app.stop())
                loop.run_until_complete(app.shutdown())
                print(f"System successfully shut down.", flush=True)
        else:
            # Fallback loop if local is run headless
            import time
            while True:
                time.sleep(3600)

if __name__ == "__main__":
    main()