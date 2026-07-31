# bot.py
import os
import sys
import json
import asyncio
import threading
import traceback
import io
import logging
import signal
import re
from datetime import datetime, timezone

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.WARNING
)
for log_name in ["telegram", "telegram.ext", "telegram.ext.Updater", "telegram.ext._updater", "httpx"]:
    logging.getLogger(log_name).setLevel(logging.CRITICAL)

from telegram import Update, BotCommand
from telegram.ext import Application, CallbackQueryHandler, CommandHandler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from src.config import CONFIG, Style, LOCKOUT_MESSAGES
from src.database import (
    QuizEngine,
    db_get_user_profile,
    db_get_user_response,
    db_update_private_message_id,
    db_get_weekly_leaderboard,
    db_get_alliance_leaderboard,
    db_set_user_alliance,
    db_get_pending_scheduled_question,
    db_mark_question_as_sent,
    process_user_score,
    db_update_response_view_state,
    db_get_track_and_question,
    db_get_cached_file_id,
    db_save_cached_file_id,
    db_get_active_tournament_rounds,
    db_update_user_telegram_info,
    db_set_user_nickname,
)
from src.rendering import get_grade_mastery_title, UIFactory, fetch_kroki_image
from src.rendering.html_views import get_next_rank_info, format_public_name
from src.rendering.rich_helpers import send_rich_message_safe, edit_rich_message_safe, convert_to_legacy_html
from src.callbacks import handle_callback
from src.cli import admin_panel
from src.tournament import tournament_watcher_loop, emergency_shutdown_cleanup
import httpx
from telegram import Poll
from src.typography import lite_math

engine = QuizEngine()

BOT_COMMANDS = [
    BotCommand("start", "Register your profile / view your stats"),
    BotCommand("school", "Set your school or study-alliance tag"),
    BotCommand("name", "Set your public nickname on scoreboard"),
    BotCommand("leaderboard", "View your rank, or /leaderboard school for group rankings"),
]

async def handle_http_request(reader, writer, app):
    try:
        header_data = await reader.readuntil(b"\r\n\r\n")
        headers = header_data.decode("utf-8")

        request_line = headers.split("\r\n")[0]
        method, path, _ = request_line.split(" ")

        content_length = 0
        for line in headers.split("\r\n"):
            if line.lower().startswith("content-length:"):
                content_length = int(line.split(":")[1].strip())
                break

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

        elif method == "POST" and path == "/webhook":
            body_data = await reader.readexactly(content_length)
            body = body_data.decode("utf-8")

            update_dict = json.loads(body)
            update = Update.de_json(update_dict, app.bot)

            await app.process_update(update)

            response = "HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            writer.write(response.encode("utf-8"))
            await writer.drain()

        else:
            response = "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            writer.write(response.encode("utf-8"))
            await writer.drain()

    except Exception as e:
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
    while True:
        import src.config
        if src.config.SHUTTING_DOWN:
            print("[SCHEDULER] Scheduler loop detected shutdown. Exiting cleanly.", flush=True)
            break

        try:
            active_rounds = await asyncio.to_thread(db_get_active_tournament_rounds)
            has_active_tournament = len(active_rounds) > 0

            if has_active_tournament:
                await asyncio.sleep(15)
                continue

            q = await asyncio.to_thread(db_get_pending_scheduled_question)
            if q:
                scheduled_val = q['scheduled_for']
                if isinstance(scheduled_val, str):
                    scheduled_dt = datetime.fromisoformat(scheduled_val)
                else:
                    scheduled_dt = scheduled_val

                now_dt = datetime.now(timezone.utc)
                scheduled_dt_utc = scheduled_dt.astimezone(timezone.utc)
                time_diff = now_dt - scheduled_dt_utc

                if time_diff.total_seconds() > 3600:
                    print(f"{Style.YELLOW}[SCHEDULER] Skipping question {q['id']} (scheduled for {q['scheduled_for']} in the past). Archiving.{Style.RESET}", flush=True)
                    await asyncio.to_thread(db_mark_question_as_sent, q['id'])
                    continue

                print(f"{Style.YELLOW}[SCHEDULER] Found scheduled question REF: {q['id']}. Publishing...{Style.RESET}", flush=True)
                channel = CONFIG.get("channel")

                tracks = await asyncio.to_thread(engine.db_get_all_tracks)
                last_seq = max((v.get('display_id', 100) for v in tracks.values()), default=100) + 1

                has_tikz = UIFactory.has_real_diagram(q)
                if not has_tikz:
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
                    img_url, caption = UIFactory.create_question_assets(q, last_seq)
                    kb = UIFactory.build_keyboard(q, last_seq)

                    cache_key = f"q:{q['id']}:diagram"
                    cached_file_id = await asyncio.to_thread(db_get_cached_file_id, cache_key)

                    media_bytes = None
                    if img_url and not cached_file_id:
                        async with httpx.AsyncClient() as client:
                            resp = await fetch_kroki_image(client, img_url)
                            if resp and resp.status_code == 200:
                                media_bytes = resp.content
                            else:
                                raise Exception("Kroki failed to compile scheduled asset.")

                    m = await send_rich_message_safe(app.bot, chat_id=channel, html_content=caption, reply_markup=kb, media_bytes=media_bytes, file_id=cached_file_id)
                    msg_type = "photo" if img_url else "text"
                    type_str = "premium"

                    if img_url and not cached_file_id and m.photo:
                        await asyncio.to_thread(db_save_cached_file_id, cache_key, m.photo[-1].file_id)

                await asyncio.to_thread(engine.db_save_track, m.message_id, q['id'], "active", last_seq, type_str, msg_type)
                await asyncio.to_thread(db_mark_question_as_sent, q['id'])
                print(f"{Style.GREEN}[SCHEDULER] Successfully posted scheduled quiz REF: {last_seq} to channel.{Style.RESET}", flush=True)
        except Exception as e:
            traceback.print_exc()
            print(f"{Style.RED}[SCHEDULER ERROR] Failed to process scheduler tick: {e}{Style.RESET}", flush=True)

        await asyncio.sleep(60)

async def start_command(update: Update, context):
    user = update.effective_user
    user_id = user.id
    args = context.args

    # Sync latest Telegram attributes on command start
    await asyncio.to_thread(db_update_user_telegram_info, user_id, user.username, user.first_name)

    channel_username = CONFIG.get("channel", "EthiopiaEntranceExam").lstrip('@')
    channel_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📣 RETURN TO CHANNEL", url=f"https://t.me/{channel_username}")
    ]])

    if args and args[0].startswith("ans_"):
        payload = args[0]
        try:
            _, ref_id, choice_idx_str = payload.split("_")
            display_id = int(ref_id)
            user_selection = int(choice_idx_str)

            print(f"[DEBUG-FIX-START] User {user_id} clicked answer link REF: {display_id}, Selection Index: {user_selection}", flush=True)
            track, question_data = await asyncio.to_thread(db_get_track_and_question, display_id)

            if not track or not question_data:
                print(f"[DEBUG-FIX-START] Track or question missing for REF: {display_id}", flush=True)
                await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content="⚠️ This quiz session has ended or the reference was not found.", reply_markup=channel_kb)
                return

            channel_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("📣 RETURN TO CHANNEL", url=f"https://t.me/{channel_username}/{track['message_id']}")
            ]])

            track_status = track.get('status')
            mid_key = track['message_id']

            if track_status == "tournament_closed":
                print(f"[DEBUG-FIX-START] User {user_id} attempted blocked submit for closed tournament REF: {display_id}", flush=True)
                await send_rich_message_safe(
                    context.bot,
                    chat_id=update.message.chat_id,
                    html_content="⚠️ <b>Round Closed!</b>\n\nSubmissions are no longer accepted for this tournament question.",
                    reply_markup=channel_kb
                )
                return

            if track_status == "tournament_active":
                existing_response = await asyncio.to_thread(db_get_user_response, user_id, mid_key)
                if existing_response:
                    print(f"[DEBUG-FIX-START] Lockout active. User {user_id} has already answered tournament question REF: {display_id}", flush=True)
                    await send_rich_message_safe(
                        context.bot,
                        chat_id=update.message.chat_id,
                        html_content="⚠️ <b>Lockout active!</b>\n\nYou have already submitted your response for this live tournament question. Your selection is locked.",
                        reply_markup=channel_kb
                    )
                    return

                is_correct = (user_selection == question_data['correct_option'])
                # Defensively verify the database write was committed
                perf_card = await asyncio.to_thread(process_user_score, user_id, mid_key, question_data['id'], is_correct, user_selection, None, False, False)
                if perf_card is None:
                    print(f"[CRITICAL-BOT-ERROR] process_user_score returned None for User {user_id} on active tournament round! Database write blocked.", flush=True)
                    await send_rich_message_safe(
                        context.bot,
                        chat_id=update.message.chat_id,
                        html_content="⚠️ <b>Database Connection Error!</b>\n\nYour selection could not be saved to our secure database because the database was unreachable. Please try clicking the option again in a few seconds!",
                        reply_markup=channel_kb
                    )
                    return

                print(f"[DEBUG-FIX-START] Logged initial tournament score record for User {user_id}, message_id: {mid_key}", flush=True)

                confirmation_msg = await send_rich_message_safe(
                    context.bot,
                    chat_id=update.message.chat_id,
                    html_content=(
                        "<b>✅ Response Received!</b>\n\n"
                        "Your selection has been securely logged. The correct answer and step-by-step "
                        "explanation card will be automatically delivered here in your DMs once the round ends!"
                    ),
                    reply_markup=channel_kb
                )
                if confirmation_msg:
                    print(f"[DEBUG-FIX-START] Storing placeholder message_id={confirmation_msg.message_id} in DM for User {user_id}, tournament message_id: {mid_key}", flush=True)
                    await asyncio.to_thread(db_update_private_message_id, user_id, mid_key, confirmation_msg.message_id)
                return

            existing_response = await asyncio.to_thread(db_get_user_response, user_id, mid_key)

            if existing_response:
                original_selection = existing_response['selected_option']
                old_private_mid = existing_response.get('private_message_id')

                show_derivation = existing_response.get('show_derivation', False)
                show_perf = existing_response.get('show_perf', False)

                async def delete_msg_safe(chat_id, mid):
                    try:
                        await context.bot.delete_message(chat_id=chat_id, message_id=mid)
                    except Exception:
                        pass

                asyncio.create_task(delete_msg_safe(update.message.chat_id, update.message.message_id))
                if old_private_mid:
                    asyncio.create_task(delete_msg_safe(update.message.chat_id, old_private_mid))

                perf_card = await asyncio.to_thread(process_user_score, user_id, mid_key, question_data['id'], existing_response['is_correct'], original_selection)
                warning_notice = "⚠️ <b>Lockout active: You have already answered this question!</b>\n" \
                                 "<i>Your original selection and score have been securely locked.</i>\n\n"

                explanation_html = warning_notice + UIFactory.build_answered_view(
                    question_data, str(display_id), original_selection, show_derivation=show_derivation, show_perf=show_perf, perf_card=perf_card
                )

                has_tikz = UIFactory.has_real_diagram(question_data)
                media_bytes = None
                cached_file_id = None

                kb = UIFactory.build_answered_keyboard(display_id, original_selection, show_derivation, show_perf, is_photo=False, message_id=track['message_id'])

                if has_tikz:
                    cache_key = f"q:{question_data['id']}:exp:{original_selection}"
                    cached_file_id = await asyncio.to_thread(db_get_cached_file_id, cache_key)

                    if not cached_file_id:
                        latex_code, _ = UIFactory.create_explanation_assets(question_data, original_selection, display_id)
                        if latex_code:
                            img_url = UIFactory.get_latex_url(latex_code)
                            async with httpx.AsyncClient() as client:
                                resp = await fetch_kroki_image(client, img_url, latex_code)
                                if resp and resp.status_code == 200:
                                    media_bytes = resp.content

                m = await send_rich_message_safe(
                    context.bot,
                    chat_id=update.message.chat_id,
                    html_content=explanation_html,
                    reply_markup=kb,
                    media_bytes=media_bytes,
                    file_id=cached_file_id
                )

                if media_bytes and m and m.photo and not cached_file_id:
                    await asyncio.to_thread(db_save_cached_file_id, cache_key, m.photo[-1].file_id)

                LOCKOUT_MESSAGES.add((user_id, m.message_id))
                await asyncio.to_thread(db_update_private_message_id, user_id, mid_key, m.message_id)
                return

            is_correct = (user_selection == question_data['correct_option'])
            # Defensively verify the database write was committed
            perf_card = await asyncio.to_thread(process_user_score, user_id, mid_key, question_data['id'], is_correct, user_selection, None, False, False)
            if perf_card is None:
                print(f"[CRITICAL-BOT-ERROR] process_user_score returned None for User {user_id} on active quiz! Database write blocked.", flush=True)
                await send_rich_message_safe(
                    context.bot,
                    chat_id=update.message.chat_id,
                    html_content="⚠️ <b>Database Connection Error!</b>\n\nYour selection could not be saved to our secure database because the database was unreachable. Please try again!",
                    reply_markup=channel_kb
                )
                return

            explanation_html = UIFactory.build_answered_view(
                question_data, str(display_id), user_selection, show_derivation=False, show_perf=False, perf_card=perf_card
            )

            has_tikz = UIFactory.has_real_diagram(question_data)
            media_bytes = None
            cached_file_id = None

            kb = UIFactory.build_answered_keyboard(display_id, user_selection, show_derivation=False, show_perf=False, is_photo=False, message_id=track['message_id'])

            if has_tikz:
                cache_key = f"q:{question_data['id']}:exp:{user_selection}"
                cached_file_id = await asyncio.to_thread(db_get_cached_file_id, cache_key)

                if not cached_file_id:
                    latex_code, _ = UIFactory.create_explanation_assets(question_data, user_selection, display_id)
                    if latex_code:
                        img_url = UIFactory.get_latex_url(latex_code)
                        async with httpx.AsyncClient() as client:
                            resp = await fetch_kroki_image(client, img_url, latex_code)
                            if resp and resp.status_code == 200:
                                    media_bytes = resp.content

            m = await send_rich_message_safe(
                context.bot,
                chat_id=update.message.chat_id,
                html_content=explanation_html,
                reply_markup=kb,
                media_bytes=media_bytes,
                file_id=cached_file_id
            )

            if media_bytes and m and m.photo and not cached_file_id:
                await asyncio.to_thread(db_save_cached_file_id, cache_key, m.photo[-1].file_id)

            await asyncio.to_thread(db_update_private_message_id, user_id, mid_key, m.message_id)
            return
        except Exception as e:
            traceback.print_exc()
            print(f" {Style.RED}[ERROR] Failed to process linked answer: {e}{Style.RESET}")
            await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content="⚠️ Failed to load your explanation. Please try again.", reply_markup=channel_kb)
            return

    profile = await asyncio.to_thread(db_get_user_profile, user_id)
    if profile and profile.get("grade"):
        grade = profile['grade']
        user_marks = profile['total_marks']
        mastery = get_grade_mastery_title(user_marks)
        accuracy = int((profile['correct'] / profile['total']) * 100) if profile['total'] > 0 else 0
        streak = profile.get('current_streak', 0)

        # Uses fallback helper for profile layout
        public_name = format_public_name(profile)
        alliance_info = f"├─ Study Alliance:  <b>#{profile['alliance_tag']}</b>\n" if profile.get('alliance_tag') else ""

        await send_rich_message_safe(
            context.bot,
            chat_id=update.message.chat_id,
            html_content=(
                f"👋 <b>Welcome Back, Scholar!</b>\n\n"
                f"Your profile is active and synchronized.\n\n"
                f"📊 <b>YOUR STUDY METRICS:</b>\n"
                f"├─ Display Handle:  <b>{public_name}</b>\n"
                f"├─ Registered Level: <b>Grade {grade}</b>\n"
                f"{alliance_info}"
                f"├─ Practice Score:  <b>{user_marks} Marks</b>\n"
                f"├─ Active Streak:   <b>🔥 {streak} Days</b>\n"
                f"├─ Mastery Level:   <b>{mastery}</b>\n"
                f"└─ Accuracy:        <b>{accuracy}%</b> ({profile['correct']} of {profile['total']} solved)\n\n"
                f"💬 <b>STUDY COMMANDS:</b>\n"
                f"• Change display name: <code>/name YOUR_NICKNAME</code>\n"
                f"• Check the channel for active scheduled questions!\n"
                f"• Type /leaderboard to view your individual rank, or <code>/leaderboard school</code> to check group rankings!"
            ),
            reply_markup=channel_kb
        )
        return

    keyboard = [
        [InlineKeyboardButton("🎒 Grade 6", callback_data="set_grade|6"),
         InlineKeyboardButton("🎒 Grade 8", callback_data="set_grade|8")],
        [InlineKeyboardButton("🎒 Grade 10", callback_data="set_grade|10"),
         InlineKeyboardButton("🎒 Grade 12", callback_data="set_grade|12")],
        [InlineKeyboardButton("🎒 SET PUBLIC NICKNAME", callback_data="prompt_nickname|0")],
        [InlineKeyboardButton("🎒 SET ALLIANCE TAG", callback_data="prompt_alliance|0")],
        [InlineKeyboardButton("📢 VISIT CHANNEL", url=f"https://t.me/{channel_username}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await send_rich_message_safe(
        context.bot,
        chat_id=update.message.chat_id,
        html_content=(
            "👋 <b>Welcome to Quiz Master Pro!</b>\n\n"
            "To customize your study experience and compare scores inside "
            "leaderboards, select your grade level below:\n\n"
            "💡 <i>Tip: Tap the Public Nickname button to set your scoreboard handle! Otherwise, the bot will use your Telegram username or first name.</i>"
        ),
        reply_markup=reply_markup
    )

async def school_command(update: Update, context):
    user = update.effective_user
    user_id = user.id

    # Sync profile parameters
    await asyncio.to_thread(db_update_user_telegram_info, user_id, user.username, user.first_name)

    if not context.args:
        await update.message.reply_text("⚠️ Please specify your school name. Example: <code>/school ABYSSINIA</code>", parse_mode="HTML")
        return

    school_name = "_".join(context.args)
    saved_tag = await asyncio.to_thread(db_set_user_alliance, user_id, school_name)

    if saved_tag:
        await update.message.reply_text(
            f"✅ <b>Success!</b> You are now registered under the study alliance: <b>#{saved_tag}</b>.\n"
            f"Your correct answers will now earn points for your school's global leaderboard!",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text("⚠️ Invalid tag format. Please use alphanumeric characters only.")

async def name_command(update: Update, context):
    """Sets a custom scoreboard nickname for the player."""
    user = update.effective_user
    user_id = user.id

    # Sync real Telegram details
    await asyncio.to_thread(db_update_user_telegram_info, user_id, user.username, user.first_name)

    if not context.args:
        await update.message.reply_text(
            "📝 <b>How to set your Public Scoreboard Name:</b>\n\n"
            "Type <code>/name YOUR_NICKNAME</code> to set a custom scoreboard nickname!\n"
            "<i>Example:</i> <code>/name Einstein_12</code>\n\n"
            "If you want to clear your custom nickname and use your Telegram username or first name instead, type <code>/name clear</code>.",
            parse_mode="HTML"
        )
        return

    nickname = " ".join(context.args).strip()
    if nickname.lower() == "clear":
        await asyncio.to_thread(db_set_user_nickname, user_id, None)
        await update.message.reply_text("✅ Your custom nickname has been cleared. The system will fall back to your Telegram username or first name on public standings.", parse_mode="HTML")
        return

    # Sanitize input to prevent styling injection or overflow
    clean_name = re.sub(r'[^\w\s\-@]', '', nickname)[:20].strip()
    if not clean_name:
        await update.message.reply_text("⚠️ Invalid nickname format. Please use alphanumeric characters, underscores, or dashes (max 20 characters).")
        return

    success = await asyncio.to_thread(db_set_user_nickname, user_id, clean_name)
    if success:
        await update.message.reply_text(
            f"✅ <b>Success!</b> Your public display handle has been updated to: <b>{clean_name}</b>.\n"
            f"This name will now be used on round podiums and weekly grade leaderboards! 🏆",
            parse_mode="HTML"
        )

async def leaderboard_command(update: Update, context):
    user = update.effective_user
    user_id = user.id

    # Auto-sync real Telegram parameters
    await asyncio.to_thread(db_update_user_telegram_info, user_id, user.username, user.first_name)

    args = context.args

    if args and args[0].lower() == "school":
        alliance_top = await asyncio.to_thread(db_get_alliance_leaderboard)
        leaderboard_text = [
            "🏆 <b>GLOBAL STUDY ALLIANCE STANDINGS</b> 🏆\n",
            "🔥 <b>TOP 10 SCHOOLS & CLANS:</b>\n"
        ]

        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        if not alliance_top:
            leaderboard_text.append("<i>No schools have registered points yet. Be the first by typing /school school_name!</i>")
        else:
            for i, row in enumerate(alliance_top):
                leaderboard_text.append(
                    f" {medals[i]} <b>#{row['alliance_tag']}</b> — <b>{row['total_score']} Marks</b> "
                    f"({row['active_members']} members)"
                )

        leaderboard_text.append("\n━━━━━━━━━━━━━━━━━━━━━━━━")
        leaderboard_text.append("💡 <i>Encourage your classmates to join and represent your school!</i>")
        await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content="\n".join(leaderboard_text))
        return

    profile = await asyncio.to_thread(db_get_user_profile, user_id)

    if not profile:
        await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content="⚠️ Please register your grade first by typing /start.")
        return

    grade = profile['grade']
    user_marks = profile['total_marks']
    mastery = get_grade_mastery_title(user_marks)

    weekly_top = await asyncio.to_thread(db_get_weekly_leaderboard, grade)

    channel_username = CONFIG.get("channel", "grade12EntranceExam").lstrip('@')
    channel_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📣 RETURN TO CHANNEL", url=f"https://t.me/{channel_username}")
    ]])

    leaderboard_text = [
        f"🏆 <b>GRADE {grade} WEEKLY LEADERBOARD</b> 🏆\n",
        f"🏅 <b>Your Rank Status:</b>",
        f"├─ Display Handle: <b>{format_public_name(profile)}</b>",
        f"├─ Mastery Level: <b>{mastery}</b>",
        f"├─ Practice Score: <b>{user_marks} Marks</b>",
        f"├─ Daily Streak:   <b>🔥 {profile.get('current_streak', 0)} Days</b>",
        f"└─ Accuracy: <b>{int((profile['correct']/profile['total'])*100) if profile['total'] > 0 else 0}%</b>\n",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🔥 <b>TOP 10 THIS WEEK:</b>"
    ]

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    for i, row in enumerate(weekly_top):
        # Format the top list profiles using format_public_name
        user_label = format_public_name(row)
        leaderboard_text.append(f" {medals[i]} {user_label} — <b>{row['total_score']} Marks</b>")

    leaderboard_text.append("\n━━━━━━━━━━━━━━━━━━━━━━━━")
    leaderboard_text.append(
        "💡 <i>Tip: Slower students can easily reach Gold level by completing exercises daily! "
        "Habitual study builds Mastery.</i>"
    )

    await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content="\n".join(leaderboard_text), reply_markup=channel_kb)

async def run_cloud_server(app, port):
    PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL")

    await app.bot.set_webhook(
        url=f"{PUBLIC_URL}/webhook",
        drop_pending_updates=True
    )
    print(f"Webhook is active on {PUBLIC_URL}/webhook.", flush=True)

    print(f"[DEBUG-FIX] Webhook active on Cloud; initializing background loops safely.", flush=True)
    asyncio.create_task(check_and_publish_scheduled(app))
    asyncio.create_task(tournament_watcher_loop(app, engine, poll_seconds=2))

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
    db_url = config.get("database_url")
    
    # Critical Startup Safety verification to protect against empty Cloud configurations
    if not db_url:
        print(f"\n{Style.RED}############################################################")
        print(f"CRITICAL WARNING: DATABASE_URL IS NOT CONFIGURED!")
        print(f"THE BOT IS RUNNING WITHOUT CLOUD DATABASE CONNECTIVITY.")
        print(f"ALL STUDENT SUBMISSIONS WILL FAIL TO WRITE TO THE DATABASE!")
        print(f"############################################################{Style.RESET}\n", flush=True)

    def sigterm_handler(signum, frame):
        print(f"\n{Style.RED}[SYSTEM TERMINATION] Received signal {signum}. Shutting down gracefully...{Style.RESET}", flush=True)
        try:
            from src.tournament import run_graceful_shutdown_sync
            run_graceful_shutdown_sync()
        except Exception as e:
            print(f"Cleanup error: {e}", flush=True)
        sys.exit(0)

    try:
        signal.signal(signal.SIGTERM, sigterm_handler)
        signal.signal(signal.SIGINT, sigterm_handler)
    except ValueError:
        pass

    app = Application.builder().token(token).build()

    import src.config
    src.config.ACTIVE_APP = app
    src.config.ACTIVE_ENGINE = engine

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("school", school_command))
    app.add_handler(CommandHandler("name", name_command))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    app.add_handler(CallbackQueryHandler(lambda u, c: handle_callback(update=u, context=c, engine=engine)))

    RENDER_PORT = os.getenv("PORT")

    if RENDER_PORT:
        print(f"Starting cloud Webhook listener on port {RENDER_PORT}...", flush=True)
        PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        src.config.ACTIVE_LOOP = loop

        loop.run_until_complete(app.initialize())
        loop.run_until_complete(app.start())

        bot_info = loop.run_until_complete(app.bot.get_me())
        CONFIG["bot_username"] = bot_info.username
        print(f"Registered Bot Username: @{bot_info.username}", flush=True)

        try:
            loop.run_until_complete(app.bot.set_my_commands(BOT_COMMANDS))
            print(f"{Style.GREEN}Registered {len(BOT_COMMANDS)} bot commands for the '/' menu.{Style.RESET}", flush=True)
        except Exception as e:
            print(f"{Style.YELLOW}[WARNING] Failed to register bot commands: {e}{Style.RESET}", flush=True)

        try:
            loop.run_until_complete(run_cloud_server(app, RENDER_PORT))
        except KeyboardInterrupt:
            pass
        finally:
            loop.run_until_complete(emergency_shutdown_cleanup(app, engine))
            loop.run_until_complete(app.stop())
            loop.run_until_complete(app.shutdown())
            print(f"System successfully shut down.", flush=True)
    else:
        print("Starting local Admin Dashboard cockpit...", flush=True)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        src.config.ACTIVE_LOOP = loop

        loop.run_until_complete(app.initialize())
        loop.run_until_complete(app.start())

        disable_polling = os.getenv("DISABLE_LOCAL_POLLING", "false").lower() == "true"

        if not disable_polling:
            print("Clearing active webhook to prevent polling conflict...", flush=True)
            loop.run_until_complete(app.bot.delete_webhook(drop_pending_updates=True))

            loop.run_until_complete(app.updater.start_polling())

            asyncio.ensure_future(check_and_publish_scheduled(app), loop=loop)
            asyncio.ensure_future(tournament_watcher_loop(app, engine, poll_seconds=2), loop=loop)
        else:
            print(f"{Style.YELLOW}⚠️  DISABLE_LOCAL_POLLING is active. Local Telegram polling is bypassed.{Style.RESET}", flush=True)
            print(f"{Style.YELLOW}Outbound dashboard active. Cloud handles webhook/callbacks for students.{Style.RESET}", flush=True)
            print(f"{Style.YELLOW}[DEBUG-FIX] Local background loop runners are disabled here. Ensure your Cloud server instance is up and active to process scheduled items!{Style.RESET}", flush=True)

        bot_info = loop.run_until_complete(app.bot.get_me())
        CONFIG["bot_username"] = bot_info.username
        print(f"Quiz Master Pro Admin Client is online and connected to {channel}.", flush=True)

        try:
            loop.run_until_complete(app.bot.set_my_commands(BOT_COMMANDS))
            print(f"{Style.GREEN}Registered {len(BOT_COMMANDS)} bot commands for the '/' menu.{Style.RESET}", flush=True)
        except Exception as e:
            print(f"{Style.YELLOW}[WARNING] Failed to register bot commands: {e}{Style.RESET}", flush=True)

        run_cli = sys.stdin.isatty()
        if run_cli:
            try:
                loop.run_until_complete(admin_panel(app, engine))
            except KeyboardInterrupt:
                pass
            finally:
                loop.run_until_complete(emergency_shutdown_cleanup(app, engine))
                if not disable_polling:
                    loop.run_until_complete(app.updater.stop())
                loop.run_until_complete(app.stop())
                loop.run_until_complete(app.shutdown())
                print(f"System successfully shut down.", flush=True)
        else:
            async def keep_alive():
                while True:
                    await asyncio.sleep(3600)
            try:
                loop.run_until_complete(keep_alive())
            except (KeyboardInterrupt, SystemExit):
                pass
            finally:
                loop.run_until_complete(emergency_shutdown_cleanup(app, engine))
                if not disable_polling:
                    loop.run_until_complete(app.updater.stop())
                loop.run_until_complete(app.stop())
                loop.run_until_complete(app.shutdown())
                print(f"System successfully shut down.", flush=True)

if __name__ == "__main__":
    main()