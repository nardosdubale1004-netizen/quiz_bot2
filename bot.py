# bot.py
import os
import sys
import json
import asyncio
import threading
import traceback
import io
import logging
from datetime import datetime, timezone

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.WARNING
)
for log_name in ["telegram", "telegram.ext", "telegram.ext.Updater", "telegram.ext._updater", "httpx"]:
    logging.getLogger(log_name).setLevel(logging.CRITICAL)

from telegram import Update
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
    db_save_cached_file_id
)
from src.rendering import get_grade_mastery_title, UIFactory, fetch_kroki_image
from src.rendering.html_views import get_next_rank_info
from src.rendering.rich_helpers import send_rich_message_safe, edit_rich_message_safe, convert_to_legacy_html
from src.callbacks import handle_callback
from src.cli import admin_panel
import httpx
from telegram import Poll
from src.typography import lite_math

engine = QuizEngine()

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
        try:
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
    user_id = update.effective_user.id
    args = context.args

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

            track, question_data = await asyncio.to_thread(db_get_track_and_question, display_id)

            if not track or not question_data:
                await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content="⚠️ This quiz session has ended or the reference was not found.", reply_markup=channel_kb)
                return

            channel_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("📣 RETURN TO CHANNEL", url=f"https://t.me/{channel_username}/{track['message_id']}")
            ]])

            track_status = track.get('status')
            mid_key = track['message_id']

            # --- TOURNAMENT ANTI-CHEAT CHECKS ---
            if track_status == "tournament_closed":
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
                    await send_rich_message_safe(
                        context.bot,
                        chat_id=update.message.chat_id,
                        html_content="⚠️ <b>Lockout active!</b>\n\nYou have already submitted your response for this live tournament question. Your selection is locked.",
                        reply_markup=channel_kb
                    )
                    return

                # Record score silently in DB
                is_correct = (user_selection == question_data['correct_option'])
                await asyncio.to_thread(process_user_score, user_id, mid_key, question_data['id'], is_correct, user_selection, None, False, False)

                # Send confirmation message and CAPTURE the message_id to allow updates later
                confirmation_msg = await send_rich_message_safe(
                    context.bot,
                    chat_id=update.message.chat_id,
                    html_content=(
                        "✅ <b>Response Recorded!</b>\n\n"
                        "Your selection has been securely logged. The correct answer and step-by-step "
                        "explanation card will be automatically delivered here in your DMs once the round ends!"
                    ),
                    reply_markup=channel_kb
                )
                if confirmation_msg:
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
                    asyncio.create_task(delete_msg_safe(update.message.chat_id, old_private_mid + 1))

                perf_card = await asyncio.to_thread(process_user_score, user_id, mid_key, question_data['id'], existing_response['is_correct'], original_selection)
                warning_notice = "⚠️ <b>Lockout active: You have already answered this question!</b>\n" \
                                 "<i>Your original selection and score have been securely locked.</i>\n\n"

                explanation_html = warning_notice + UIFactory.build_answered_view(
                    question_data, str(display_id), original_selection, show_derivation=show_derivation, show_perf=show_perf, perf_card=perf_card
                )

                has_ex_diag = UIFactory.has_explanation_diagram(question_data)
                if has_ex_diag:
                    explanation_html_compact = warning_notice + UIFactory.build_answered_view(
                        question_data, str(display_id), original_selection, show_derivation=False, show_perf=False, perf_card=perf_card
                    )

                    cache_key = f"q:{question_data['id']}:exp:{original_selection}"
                    cached_file_id = await asyncio.to_thread(db_get_cached_file_id, cache_key)

                    photo_kb = UIFactory.build_answered_keyboard(display_id, original_selection, show_derivation, show_perf, is_photo=True, message_id=track['message_id'])
                    legacy_caption = convert_to_legacy_html(explanation_html_compact)

                    if cached_file_id:
                        print(f" {Style.GREEN}├─ [CACHE HIT] Direct DM delivery using cached file ID. Bypassing Kroki...{Style.RESET}")
                        m = await context.bot.send_photo(chat_id=update.message.chat_id, photo=cached_file_id, caption=legacy_caption, parse_mode="HTML", reply_markup=photo_kb)
                    else:
                        print(f" {Style.YELLOW}├─ [CACHE MISS] Solution diagram not cached. Rendering via Kroki...{Style.RESET}")
                        latex_code, _ = UIFactory.create_explanation_assets(question_data, original_selection, display_id)
                        if latex_code:
                            img_url = UIFactory.get_latex_url(latex_code)
                            async with httpx.AsyncClient() as client:
                                resp = await fetch_kroki_image(client, img_url, latex_code)
                                if resp and resp.status_code == 200:
                                    m = await context.bot.send_photo(chat_id=update.message.chat_id, photo=io.BytesIO(resp.content), caption=legacy_caption, parse_mode="HTML", reply_markup=photo_kb)
                                    if m.photo:
                                        await asyncio.to_thread(db_save_cached_file_id, cache_key, m.photo[-1].file_id)
                                else:
                                    m = await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content=explanation_html, reply_markup=photo_kb)

                    LOCKOUT_MESSAGES.add((user_id, m.message_id))
                    await asyncio.to_thread(db_update_private_message_id, user_id, mid_key, m.message_id)

                    if show_derivation or show_perf:
                        full_text = warning_notice + UIFactory.build_answered_view(
                            question_data, str(display_id), original_selection, show_derivation=show_derivation, show_perf=show_perf, perf_card=perf_card, continuation=True
                        )
                        follow_up = await send_rich_message_safe(
                            context.bot,
                            chat_id=update.message.chat_id,
                            html_content=full_text,
                            reply_to_message_id=m.message_id
                        )
                        await asyncio.to_thread(engine.db_save_track, mid_key, track["q_id"], "active", display_id, track["type"], track["msg_type"], followup_mid=follow_up.message_id)
                    return

                reveal_kb = UIFactory.build_answered_keyboard(display_id, original_selection, show_derivation, show_perf, is_photo=False, message_id=track['message_id'])
                f_m = await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content=explanation_html, reply_markup=reveal_kb)

                LOCKOUT_MESSAGES.add((user_id, f_m.message_id))
                await asyncio.to_thread(db_update_private_message_id, user_id, mid_key, f_m.message_id)
                return

            is_correct = (user_selection == question_data['correct_option'])
            perf_card = await asyncio.to_thread(process_user_score, user_id, mid_key, question_data['id'], is_correct, user_selection, None, False, False)

            explanation_html = UIFactory.build_answered_view(
                question_data, str(display_id), user_selection, show_derivation=False, show_perf=False, perf_card=perf_card
            )

            has_ex_diag = UIFactory.has_explanation_diagram(question_data)
            if has_ex_diag:
                explanation_html_compact = UIFactory.build_answered_view(
                    question_data, str(display_id), user_selection, show_derivation=False, show_perf=False, perf_card=perf_card
                )

                cache_key = f"q:{question_data['id']}:exp:{user_selection}"
                cached_file_id = await asyncio.to_thread(db_get_cached_file_id, cache_key)

                photo_kb = UIFactory.build_answered_keyboard(display_id, user_selection, show_derivation=False, show_perf=False, is_photo=True, message_id=track['message_id'])
                legacy_caption = convert_to_legacy_html(explanation_html_compact)

                if cached_file_id:
                    print(f" {Style.GREEN}├─ [CACHE HIT] Direct DM delivery using cached file ID. Bypassing Kroki...{Style.RESET}")
                    m = await context.bot.send_photo(chat_id=update.message.chat_id, photo=cached_file_id, caption=legacy_caption, parse_mode="HTML", reply_markup=photo_kb)
                else:
                    print(f" {Style.YELLOW}├─ [CACHE MISS] Solution diagram not cached. Rendering via Kroki...{Style.RESET}")
                    latex_code, _ = UIFactory.create_explanation_assets(question_data, user_selection, display_id)
                    if latex_code:
                        img_url = UIFactory.get_latex_url(latex_code)
                        async with httpx.AsyncClient() as client:
                            resp = await fetch_kroki_image(client, img_url, latex_code)
                            if resp and resp.status_code == 200:
                                m = await context.bot.send_photo(chat_id=update.message.chat_id, photo=io.BytesIO(resp.content), caption=legacy_caption, parse_mode="HTML", reply_markup=photo_kb)
                                if m.photo:
                                    await asyncio.to_thread(db_save_cached_file_id, cache_key, m.photo[-1].file_id)
                            else:
                                m = await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content=explanation_html, reply_markup=photo_kb)

                await asyncio.to_thread(db_update_private_message_id, user_id, mid_key, m.message_id)
                return

            reveal_kb = UIFactory.build_answered_keyboard(display_id, user_selection, show_derivation=False, show_perf=False, is_photo=False, message_id=track['message_id'])
            f_m = await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content=explanation_html, reply_markup=reveal_kb)
            await asyncio.to_thread(db_update_private_message_id, user_id, mid_key, f_m.message_id)
            return
        except Exception as e:
            traceback.print_exc()
            print(f" {Style.RED}[ERROR] Failed to process deep-linked answer: {e}{Style.RESET}")
            await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content="⚠️ Failed to load your explanation. Please try again.", reply_markup=channel_kb)
            return

    profile = await asyncio.to_thread(db_get_user_profile, user_id)
    if profile and profile.get("grade"):
        grade = profile['grade']
        user_marks = profile['total_marks']
        mastery = get_grade_mastery_title(user_marks)
        accuracy = int((profile['correct'] / profile['total']) * 100) if profile['total'] > 0 else 0
        streak = profile.get('current_streak', 0)

        alliance_info = f"├─ Study Alliance:  <b>#{profile['alliance_tag']}</b>\n" if profile.get('alliance_tag') else ""

        await send_rich_message_safe(
            context.bot,
            chat_id=update.message.chat_id,
            html_content=(
                f"👋 <b>Welcome Back, Scholar!</b>\n\n"
                f"Your academic profile is active and fully synchronized.\n\n"
                f"📊 <b>YOUR STUDY METRICS:</b>\n"
                f"├─ Registered Level: <b>Grade {grade}</b>\n"
                f"{alliance_info}"
                f"├─ Practice Score:  <b>{user_marks} Marks</b>\n"
                f"├─ Active Streak:   <b>🔥 {streak} Days</b>\n"
                f"├─ Mastery Level:   <b>{mastery}</b>\n"
                f"└─ Accuracy:        <b>{accuracy}%</b> ({profile['correct']} of {profile['total']} questions solved correctly)\n\n"
                f"💬 <b>STUDY CHANNELS:</b>\n"
                f"• Check the main channel for active scheduled questions!\n"
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
        [InlineKeyboardButton("🏷️ SET SCHOOL / ALLIANCE TAG", callback_data="prompt_alliance|0")],
        [InlineKeyboardButton("📢 VISIT CHANNEL", url=f"https://t.me/{channel_username}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await send_rich_message_safe(
        context.bot,
        chat_id=update.message.chat_id,
        html_content=(
            "👋 <b>Welcome to Quiz Master Pro!</b>\n\n"
            "To customize your study experience, unlock early bird rewards, and compare "
            "scores inside fair rank tables, select your academic grade level below:\n\n"
            "💡 <i>Tip: Tap the Alliance button to join or create a school group to participate in group challenges!</i>"
        ),
        reply_markup=reply_markup
    )

async def school_command(update: Update, context):
    """Saves user's alliance registration."""
    user_id = update.effective_user.id
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

async def leaderboard_command(update: Update, context):
    user_id = update.effective_user.id
    args = context.args

    # Check for school filter parameter
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
        f"├─ Mastery Level: <b>{mastery}</b>",
        f"├─ Practice Score: <b>{user_marks} Marks</b>",
        f"├─ Daily Streak:   <b>🔥 {profile.get('current_streak', 0)} Days</b>",
        f"└─ Accuracy: <b>{int((profile['correct']/profile['total'])*100) if profile['total'] > 0 else 0}%</b>\n",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🔥 <b>TOP 10 THIS WEEK:</b>"
    ]

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    for i, row in enumerate(weekly_top):
        user_label = f"Student {str(row['user_id'])[-4:]}"
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

    asyncio.create_task(check_and_publish_scheduled(app))

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

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("school", school_command))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    app.add_handler(CallbackQueryHandler(lambda u, c: handle_callback(update=u, context=c, engine=engine)))

    RENDER_PORT = os.getenv("PORT")

    if RENDER_PORT:
        print(f"Starting cloud Webhook listener on port {RENDER_PORT}...", flush=True)
        PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        loop.run_until_complete(app.initialize())
        loop.run_until_complete(app.start())

        bot_info = loop.run_until_complete(app.bot.get_me())
        CONFIG["bot_username"] = bot_info.username
        print(f"Registered Bot Username: @{bot_info.username}", flush=True)

        try:
            loop.run_until_complete(run_cloud_server(app, RENDER_PORT))
        except KeyboardInterrupt:
            pass
        finally:
            loop.run_until_complete(app.stop())
            loop.run_until_complete(app.shutdown())
            print(f"System successfully shut down.", flush=True)
    else:
        print("Starting local Admin Dashboard cockpit...", flush=True)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        loop.run_until_complete(app.initialize())
        loop.run_until_complete(app.start())

        print("Clearing active webhook to prevent polling conflict...", flush=True)
        loop.run_until_complete(app.bot.delete_webhook(drop_pending_updates=True))

        loop.run_until_complete(app.updater.start_polling())

        asyncio.ensure_future(check_and_publish_scheduled(app), loop=loop)

        bot_info = loop.run_until_complete(app.bot.get_me())
        CONFIG["bot_username"] = bot_info.username
        print(f"Quiz Master Pro Admin Client is online and connected to {channel}.", flush=True)

        run_cli = sys.stdin.isatty()
        if run_cli:
            try:
                loop.run_until_complete(admin_panel(app, engine))
            except KeyboardInterrupt:
                pass
            finally:
                loop.run_until_complete(app.updater.stop())
                loop.run_until_complete(app.stop())
                loop.run_until_complete(app.shutdown())
                print(f"System successfully shut down.", flush=True)
        else:
            # --- CRITICAL NON-BLOCKING EVENT LOOP KEEPALIVE ---
            # Using async loop sleep prevents thread freeze on daemon runs
            async def keep_alive():
                while True:
                    await asyncio.sleep(3600)
            try:
                loop.run_until_complete(keep_alive())
            except (KeyboardInterrupt, SystemExit):
                pass
            finally:
                loop.run_until_complete(app.updater.stop())
                loop.run_until_complete(app.stop())
                loop.run_until_complete(app.shutdown())
                print(f"System successfully shut down.", flush=True)

if __name__ == "__main__":
    main()