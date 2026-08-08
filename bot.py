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
import html
from datetime import datetime, timezone

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.WARNING
)
for log_name in ["telegram", "telegram.ext", "telegram.ext.Updater", "telegram.ext._updater", "httpx"]:
    logging.getLogger(log_name).setLevel(logging.CRITICAL)

from telegram import Update, BotCommand
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters, ContextTypes
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from src.config import CONFIG, Style, LOCKOUT_MESSAGES, USER_STATES, USER_PAYLOADS, ADMIN_IDS, FEEDBACK_CATEGORIES, LAST_UTILITY_MID, FSM_INPUT_HINT
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
    db_create_organization,
    db_join_organization,
    db_join_organization_by_id,
    db_get_organization_roster,
    db_update_user_location,
    db_set_user_referrer,
    db_count_referrals,
    db_find_similar_organizations,
    db_submit_feedback,
    db_get_feedback_by_id,
    db_update_feedback_status,
    db_save_feedback_reply,
    db_get_feedback_stats,
    db_join_organization_by_token,
    db_call_guarded,
    db_get_user_feedback_list,
    db_count_user_feedback,
    db_mark_question_shown,
    db_get_user_subject_marks,
    db_get_or_create_referral_token,
    db_get_user_id_by_referral_token,
    db_add_feedback_message,
    db_get_feedback_thread,
    db_get_bot_state,
    db_get_user_timezone,
    db_create_dedicated_organization,
)
from src.rendering import get_grade_mastery_title, UIFactory, fetch_kroki_image
from src.rendering.html_views import get_next_rank_info, format_public_name, build_profile_card_text, build_feedback_stats_text, build_feedback_item_text, build_user_feedback_list_text
from src.rendering.rich_helpers import send_rich_message_safe, edit_rich_message_safe, convert_to_legacy_html
from src.callbacks import handle_callback, _notify_org_admins_pending_request
from src.cli import admin_panel
from src.tournament import tournament_watcher_loop, emergency_shutdown_cleanup
import httpx
from telegram import Poll
from src.typography import lite_math
_UTILITY_LOCKS: dict = {}
engine = QuizEngine()

async def _enforce_location_gate(context, user_id, chat_id) -> bool:
    """Mandatory gate: a student must have a city AND country on file (approved OR still
    pending admin review both count) before they can submit a NEW answer. Viewing previous
    answers, /profile, etc. is never blocked — only fresh submissions are gated here."""
    from src.database import db_user_location_complete
    if await asyncio.to_thread(db_user_location_complete, user_id):
        return True
    gate_kb = InlineKeyboardMarkup([[InlineKeyboardButton("📍 SET MY LOCATION NOW", callback_data="regloc_start|0")]])
    await send_rich_message_safe(
        context.bot, chat_id=chat_id,
        html_content=(
            "🚫 <b>Set your city &amp; country first</b>\n\n"
            "Before you can answer questions, we need your city and country on file — "
            "even if it's still pending admin review, that's enough to unlock answering.\n\n"
            "Tap below to finish this in under a minute."
        ),
        reply_markup=gate_kb
    )
    return False

# Consolidated commands to simplify the user interface
BOT_COMMANDS = [
    BotCommand("start", "Register your academic profile / level"),
    BotCommand("profile", "Open scoreboard visibility, nickname & school alliance dashboard"),
    BotCommand("leaderboard", "View individual rank standings or school rankings"),
    BotCommand("invite", "Get your referral link & earn bonus marks"),
    BotCommand("help", "How the bot works, step by step"),
    BotCommand("feedback", "Report a bug, request a feature, or share feedback"),
    BotCommand("myanswers", "Browse every question & your answers"),
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

            # Fire-and-forget: don't block the HTTP response on full update processing.
            # This lets the server accept the next incoming webhook immediately instead
            # of serializing all traffic through one connection's processing time.
            asyncio.create_task(_process_update_safe(app, update))

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

async def _process_update_safe(app, update):
    """Wraps app.process_update so a crash in one update's processing never
    surfaces as an unhandled task exception or takes down the event loop."""
    try:
        await app.process_update(update)
    except Exception as e:
        print(f"[BACKGROUND UPDATE EXCEPTION]: {e}", file=sys.stderr, flush=True)

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
                    from src.database import db_mark_question_shown
                    await asyncio.to_thread(db_mark_question_shown, q['id'])
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
                from src.database import db_mark_question_shown
                await asyncio.to_thread(db_mark_question_shown, q['id'])
                print(f"{Style.GREEN}[SCHEDULER] Successfully posted scheduled quiz REF: {last_seq} to channel.{Style.RESET}", flush=True)
        except Exception as e:
            traceback.print_exc()
            print(f"{Style.RED}[SCHEDULER ERROR] Failed to process scheduler tick: {e}{Style.RESET}", flush=True)

        await asyncio.sleep(60)

async def start_command(update: Update, context):
    try:
        await _start_command_inner(update, context)
    except Exception as e:
        traceback.print_exc()
        try:
            await update.message.reply_text("⚠️ Something went wrong loading that. Please try /start again.")
        except Exception:
            pass


async def _start_command_inner(update: Update, context):
    user = update.effective_user
    user = update.effective_user
    user_id = user.id
    args = context.args

    print("\n" + "="*80, flush=True)
    print(f"{Style.CYAN}[TRACE-START] Entered start_command for User ID: {user_id}{Style.RESET}", flush=True)
    print(f" ├─ Chat ID:            {update.effective_chat.id}", flush=True)
    print(f" ├─ Username:           {user.username}", flush=True)
    print(f" ├─ First Name:         {user.first_name}", flush=True)
    print(f" └─ Command Arguments:  {args}", flush=True)
    print("="*80, flush=True)

    # Sync latest Telegram attributes on command start
    await asyncio.to_thread(db_update_user_telegram_info, user_id, user.username, user.first_name)

    channel_username = CONFIG.get("channel", "EthiopiaEntranceExam").lstrip('@')
    channel_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📣 TO CHANNEL", url=f"https://t.me/{channel_username}")
    ]])

    if args and args[0].startswith("ans_"):
        payload = args[0]
        asyncio.create_task(_delete_silent(context.bot, update.message.chat_id, update.message.message_id))  # <-- ADD THIS LINE
        print(f"\n{Style.YELLOW}[TRACE-STEP 1] Detected deep-linked answering argument payload: '{payload}'{Style.RESET}", flush=True)

        try:
            _, ref_id, choice_idx_str = payload.split("_")
            display_id = int(ref_id)
            user_selection = int(choice_idx_str)

            print(f" ├─ Parsed Display ID:   {display_id}", flush=True)
            print(f" └─ Parsed User Choice:  {user_selection}", flush=True)

            print(f"[TRACE-STEP 2] Fetching track & question data from database for display_id: {display_id}...", flush=True)
            track, question_data = await asyncio.to_thread(db_get_track_and_question, display_id)

            if not track or not question_data:
                print(f"{Style.RED}[TRACE-FAILURE] Lookups failed. track found: {track is not None}, question_data found: {question_data is not None}{Style.RESET}", flush=True)
                await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content="⚠️ This quiz session has ended or the reference was not found.", reply_markup=channel_kb)
                return

            print(f"{Style.GREEN}[TRACE-SUCCESS] DB track found.{Style.RESET}", flush=True)
            print(f" ├─ Track Message ID:   {track.get('message_id')}", flush=True)
            print(f" ├─ Track Status:       {track.get('status')}", flush=True)
            print(f" ├─ Question ID:        {question_data.get('id')}", flush=True)
            print(f" └─ Correct Option:     {question_data.get('correct_option')}", flush=True)

            channel_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("📣 TO QUESTION", url=f"https://t.me/{channel_username}/{track['message_id']}")
            ]])

            track_status = track.get('status')
            mid_key = track['message_id']

        except Exception as e:
            print("\n" + "#"*80, flush=True)
            print(f"{Style.RED}[TRACE-CRITICAL-EXCEPTION] Failed to parse deep-link payload or fetch track/question!{Style.RESET}", flush=True)
            print(f" ├─ Error Message:      {e}", flush=True)
            print(f" └─ Raw Payload:        {payload}", flush=True)
            traceback.print_exc()
            print("#"*80 + "\n", flush=True)

            try:
                from src.debug_log import dlog_exception
                dlog_exception(f"bot.py -> start_command deep link parse crash (Payload: {payload})", e)
            except Exception:
                pass

            await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content="⚠️ This link appears to be invalid or expired. Please try again from the channel.", reply_markup=channel_kb)
            return

        if track_status == "tournament_closed":
            print(f"[TRACE-STEP 3] Locked out: Round is closed for display_id: {display_id}", flush=True)
            closed_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📣 TO CHANNEL", url=f"https://t.me/{channel_username}")],
                [InlineKeyboardButton("👤 MY PROFILE", callback_data="privacy_menu|0")]
            ])
            await send_rich_message_safe(
                context.bot,
                chat_id=update.message.chat_id,
                html_content="🏁 <b>This Round Has Ended</b>\n\nThe timer ran out before your tap reached us, so this one's closed for submissions. Catch the next live round in the channel!",
                reply_markup=closed_kb
            )
            return

        # --- TOURNAMENT BRANCH: Answering block ---
        if track_status == "tournament_active":
            try:
                print(f"[TRACE-STEP 3] Active tournament round detected. Reading student history...", flush=True)
                existing_response = await asyncio.to_thread(db_get_user_response, user_id, mid_key)
                if existing_response:
                    from src.database import db_is_tournament_round_still_open, db_get_user_edit_stats
                    still_open = await asyncio.to_thread(db_is_tournament_round_still_open, mid_key)
                    old_opt = existing_response['selected_option']
                    old_private_mid = existing_response.get('private_message_id')

                    # Always clear the previous "Response Received!" (or prior prompt) bubble first —
                    # every branch below replaces it with something new, so it should never linger.
                    if old_private_mid:
                        try:
                            await context.bot.delete_message(chat_id=update.message.chat_id, message_id=old_private_mid)
                        except Exception:
                            pass

                    if not still_open or user_selection == old_opt:
                        letters = ["A", "B", "C", "D", "E"]
                        lockout_kb = InlineKeyboardMarkup([
                            [InlineKeyboardButton("📣 TO CHANNEL", url=f"https://t.me/{channel_username}")],
                            [InlineKeyboardButton("👤 MY PROFILE", callback_data="privacy_menu|0")]
                        ])
                        lockout_html = (
                            f"✅ <b>Already Submitted</b>\n\n"
                            f"You answered <b>{letters[old_opt] if old_opt < len(letters) else '?'}</b> for REF <code>{display_id}</code>. "
                            + ("The round has ended, so that's your final answer — the full explanation lands here shortly."
                               if not still_open else
                               "That's still your saved answer.")
                        )
                        m = await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content=lockout_html, reply_markup=lockout_kb)
                        if m:
                            await asyncio.to_thread(db_update_private_message_id, user_id, mid_key, m.message_id)
                        return

                    # Round still open and they picked a DIFFERENT option — offer to change it.
                    stats = await asyncio.to_thread(db_get_user_edit_stats, user_id)
                    total_edits = stats.get("answer_edits_total", 0)
                    hint_line = ""
                    if total_edits >= 3:
                        helped_pct = int((stats.get("answer_edits_helped", 0) / total_edits) * 100)
                        hurt_pct = int((stats.get("answer_edits_hurt", 0) / total_edits) * 100)
                        hint_line = (
                            f"\n\n📊 <i>Just for context: changing your mind has helped you {helped_pct}% of the time "
                            f"and hurt you {hurt_pct}% of the time in the past. What matters now is this question — trust your read on it.</i>"
                        )

                    letters = ["A", "B", "C", "D", "E"]
                    confirm_kb = InlineKeyboardMarkup([
                        [InlineKeyboardButton("📣 RE-CHECK THE QUESTION", url=f"https://t.me/{channel_username}/{track['message_id']}")],
                        [InlineKeyboardButton(f"✅ CHANGE TO {letters[user_selection]}", callback_data=f"confirm_change|{display_id}|{user_selection}")],
                        [InlineKeyboardButton("❌ KEEP MY ORIGINAL ANSWER", callback_data=f"cancel_change|{display_id}")]
                    ])
                    confirm_html = (
                        f"🔁 <b>Change your answer?</b>\n\n"
                        f"You currently have <b>{letters[old_opt]}</b> saved for REF <code>{display_id}</code>. "
                        f"You just tapped <b>{letters[user_selection]}</b>.\n\n"
                        f"⏳ <b>This only applies if you confirm before the round timer runs out.</b> "
                        f"If time runs out before you confirm, your original answer (<b>{letters[old_opt]}</b>) stays locked in."
                        f"{hint_line}"
                    )
                    m = await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content=confirm_html, reply_markup=confirm_kb)
                    if m:
                        await asyncio.to_thread(db_update_private_message_id, user_id, mid_key, m.message_id)
                    return

                print(f"[TRACE-STEP 4] No history found. Calculating score logic...", flush=True)

                if not await _enforce_location_gate(context, user_id, update.message.chat_id):
                    return

                is_correct = (user_selection == question_data['correct_option'])

                print(f"[TRACE-STEP 5] Calling process_user_score in database module...", flush=True)
                try:
                    perf_card = await db_call_guarded(process_user_score, user_id, mid_key, question_data['id'], is_correct, user_selection, None, False, False)
                except TimeoutError:
                    await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content="⚠️ We're experiencing very high traffic right now. Please tap your answer again in a few seconds.", reply_markup=channel_kb)
                    return
                if perf_card is None:
                    print(f"{Style.RED}[TRACE-FAILURE] process_user_score returned None! Database transaction failed.{Style.RESET}", flush=True)
                    
                    try:
                        from src.debug_log import dlog
                        dlog(f"[TRACE-ERROR] process_user_score returned None for user {user_id}, mid {mid_key}")
                    except Exception:
                        pass

                    await send_rich_message_safe(
                        context.bot,
                        chat_id=update.message.chat_id,
                        html_content="⚠️ <b>Database Connection Error!</b>\n\nYour selection could not be saved to our secure database because the database was unreachable. Please try clicking the option again in a few seconds!",
                        reply_markup=channel_kb
                    )
                    return

                print(f"{Style.GREEN}[TRACE-SUCCESS] Score processed successfully. Total score: {perf_card.get('total_marks')} Marks.{Style.RESET}", flush=True)

                print(f"[TRACE-STEP 6] Sending DM response receipt...", flush=True)
                received_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📣 TO CHANNEL", url=f"https://t.me/{channel_username}")],
                    [InlineKeyboardButton("👤 MY PROFILE", callback_data="privacy_menu|0")]
                ])
                confirmation_msg = await send_rich_message_safe(
                    context.bot,
                    chat_id=update.message.chat_id,
                    html_content=(
                        "<b>✅ Response Received!</b>\n\n"
                        "Your selection has been securely logged. The correct answer and step-by-step "
                        "explanation card will be automatically delivered here in your DMs once the round ends!"
                    ),
                    reply_markup=received_kb
                )
                if confirmation_msg:
                    print(f" └─ Placeholder message delivered with ID: {confirmation_msg.message_id}. Saving message reference...", flush=True)
                    await asyncio.to_thread(db_update_private_message_id, user_id, mid_key, confirmation_msg.message_id)
                print(f"{Style.GREEN}[TRACE-COMPLETE] Answering sequence finished cleanly for tournament round.{Style.RESET}", flush=True)
                return

            except Exception as e:
                # Upgraded dynamic diagnostic pipeline to report actual Python tracebacks to user's Telegram DM
                tb_str = traceback.format_exc()
                error_class = type(e).__name__
                
                print("\n" + "#"*80, flush=True)
                print(f"{Style.RED}[TRACE-CRITICAL-EXCEPTION] An error occurred while submitting a live tournament response!{Style.RESET}", flush=True)
                print(f" ├─ Error Message:      {e}", flush=True)
                print(f" ├─ User ID:            {user_id}", flush=True)
                print(f" ├─ display_id:         {display_id}", flush=True)
                print(f" └─ mid_key:            {mid_key}", flush=True)
                print(" └─ Stack Trace details:", flush=True)
                traceback.print_exc()
                print("#"*80 + "\n", flush=True)

                try:
                    from src.debug_log import dlog_exception
                    dlog_exception(f"bot.py -> start_command active tournament crash (User={user_id}, DisplayID={display_id}, Mid={mid_key})", e)
                except Exception:
                    pass

                diagnostic_html = (
                    f"⚠️ <b>Submission Error!</b>\n\n"
                    f"Your response could not be saved right now. Please tap the option again in a few seconds — your round timer is still running.\n\n"
                    f"🛠️ <b>DEVELOPER DIAGNOSTIC LOG:</b>\n"
                    f"<blockquote>"
                    f"<b>Error Class:</b> <code>{error_class}</code>\n"
                    f"<b>Details:</b> <code>{html.escape(str(e))}</code>\n\n"
                    f"<b>Traceback snippet:</b>\n"
                    f"<code>{html.escape(tb_str[-400:])}</code>"
                    f"</blockquote>"
                )

                await send_rich_message_safe(
                    context.bot,
                    chat_id=update.message.chat_id,
                    html_content=diagnostic_html,
                    reply_markup=channel_kb
                )
                return

        # --- STANDARD (non-tournament) BRANCH ---
        try:
            print(f"[TRACE-STEP 3] Standard (non-tournament) active quiz path. Fetching history...", flush=True)
            existing_response = await asyncio.to_thread(db_get_user_response, user_id, mid_key)
            is_removed = (track_status == "deleted")

            if existing_response:
                if existing_response:
                    print(f" ├─ History found. Selected Option: {existing_response.get('selected_option')} | Is Correct: {existing_response.get('is_correct')}", flush=True)
                    original_selection = existing_response['selected_option']
                    old_private_mid = existing_response.get('private_message_id')

                    # Clear the previous saved-answer bubble first so re-taps never stack duplicates.
                    if old_private_mid:
                        try:
                            await context.bot.delete_message(chat_id=update.message.chat_id, message_id=old_private_mid)
                        except Exception:
                            pass

                    show_derivation = existing_response.get('show_derivation', False)
                    show_perf = existing_response.get('show_perf', False)

                    async def delete_msg_safe(chat_id, mid):
                        try:
                            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
                        except Exception:
                            pass

                    asyncio.create_task(delete_msg_safe(update.message.chat_id, update.message.message_id))

                    print(f" ├─ Computing latest scoreboard metadata...", flush=True)
                    perf_card = await asyncio.to_thread(process_user_score, user_id, mid_key, question_data['id'], existing_response['is_correct'], original_selection)

                    # Answered-before and removed are NOT mutually exclusive — a question you
                    # already answered can later be pulled from the channel. Show BOTH facts
                    # instead of collapsing to a generic "quiz ended" message.
                    if is_removed:
                        warning_notice = (
                            "⚫ <b>This Question Was Removed</b>\n"
                            "<i>It's no longer visible in the channel, but your saved answer below is completely unaffected.</i>\n\n"
                        )
                    else:
                        warning_notice = "ℹ️ <b>Already Answered</b>\n" \
                                        "<i>You've already submitted your answer for this one — here's your saved result.</i>\n\n"

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
                            print(f" ├─ [CACHE MISS] Solution sheet not cached. Generating LaTeX...", flush=True)
                            latex_code, _ = UIFactory.create_explanation_assets(question_data, original_selection, display_id)
                            if latex_code:
                                img_url = UIFactory.get_latex_url(latex_code)
                                async with httpx.AsyncClient() as client:
                                    resp = await fetch_kroki_image(client, img_url, latex_code)
                                    if resp and resp.status_code == 200:
                                        media_bytes = resp.content

                    print(f" ├─ Sending updated explanation card...", flush=True)
                    m = await send_rich_message_safe(
                        context.bot,
                        chat_id=update.message.chat_id,
                        html_content=explanation_html,
                        reply_markup=kb,
                        media_bytes=media_bytes,
                        file_id=cached_file_id,
                        preserve_utility=True
                    )

                    if media_bytes and m and m.photo and not cached_file_id:
                        await asyncio.to_thread(db_save_cached_file_id, cache_key, m.photo[-1].file_id)

                    LOCKOUT_MESSAGES.add((user_id, m.message_id))
                    await asyncio.to_thread(db_update_private_message_id, user_id, mid_key, m.message_id)
                    print(f"{Style.GREEN}[TRACE-COMPLETE] History fallback view successfully displayed.{Style.RESET}", flush=True)
                    return

            # Not answered yet — if it was removed, there's nothing to submit against.
            # THIS is the case that used to silently do nothing: track_status=="deleted"
            # fell through the old "!= active and != closed" check into a generic dead-end.
            if is_removed:
                print(f" ├─ Question removed and never answered by this user. Showing removal confirmation.", flush=True)
                removed_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("👤 MY PROFILE", callback_data="privacy_menu|0")],
                    [InlineKeyboardButton("📣 TO CHANNEL", url=f"https://t.me/{channel_username}")]
                ])
                await send_rich_message_safe(
                    context.bot,
                    chat_id=update.message.chat_id,
                    html_content=(
                        f"⚫ <b>This Question Was Removed</b>\n\n"
                        f"REF <code>{display_id}</code> is no longer available in the channel, and no answer was "
                        f"recorded for it — no worries, there's always the next question!"
                    ),
                    reply_markup=removed_kb
                )
                return

            if track_status not in ("active", "closed"):
                print(f" {Style.YELLOW}└─ [WARNING] Blocked submission: Quiz status is '{track_status}'.{Style.RESET}", flush=True)
                await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content="⚠️ This quiz session has ended or the reference was not found.", reply_markup=channel_kb)
                return

            print(f"[TRACE-STEP 4] Standard active path. Calculating first-time response...", flush=True)

            if not await _enforce_location_gate(context, user_id, update.message.chat_id):
                return

            is_correct = (user_selection == question_data['correct_option'])
            try:
                perf_card = await db_call_guarded(process_user_score, user_id, mid_key, question_data['id'], is_correct, user_selection, None, False, False)
            except TimeoutError:
                await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content="⚠️ We're experiencing very high traffic right now. Please tap your answer again in a few seconds.", reply_markup=channel_kb)
                return
            if perf_card is None:
                print(f"{Style.RED}[TRACE-FAILURE] process_user_score returned None! Database transaction failed.{Style.RESET}", flush=True)
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
                    print(f" ├─ [CACHE MISS] Solution sheet not cached. Generating LaTeX...", flush=True)
                    latex_code, _ = UIFactory.create_explanation_assets(question_data, user_selection, display_id)
                    if latex_code:
                        img_url = UIFactory.get_latex_url(latex_code)
                        async with httpx.AsyncClient() as client:
                            resp = await fetch_kroki_image(client, img_url, latex_code)
                            if resp and resp.status_code == 200:
                                    media_bytes = resp.content

            print(f" ├─ Sending new explanation card...", flush=True)
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
            print(f"{Style.GREEN}[TRACE-COMPLETE] First-time answering completed successfully.{Style.RESET}", flush=True)
            return
        except Exception as e:
            tb_str = traceback.format_exc()
            error_class = type(e).__name__

            print("\n" + "#"*80, flush=True)
            print(f"{Style.RED}[CONSOLIDATED-FIX] Critical exception occurred inside standard explanation-rendering!{Style.RESET}", flush=True)
            print(f" ├─ Error Message:      {e}", flush=True)
            print(f" ├─ User ID:            {user_id}", flush=True)
            print(f" ├─ display_id variable: {display_id if 'display_id' in locals() else 'None'}", flush=True)
            print(f" ├─ mid_key variable:    {mid_key if 'mid_key' in locals() else 'None'}", flush=True)
            print(" └─ Stack Trace details:", flush=True)
            traceback.print_exc()
            print("#"*80 + "\n", flush=True)

            try:
                from src.debug_log import dlog_exception
                dlog_exception(f"bot.py -> start_command standard answering crash (User={user_id}, DisplayID={display_id if 'display_id' in locals() else 'None'})", e)
            except Exception:
                pass

            diagnostic_html = (
                f"⚠️ <b>Submission Error!</b>\n\n"
                f"Your response could not be saved right now. Please try again in a few seconds.\n\n"
                f"🛠️ <b>DEVELOPER DIAGNOSTIC LOG:</b>\n"
                f"<blockquote>"
                f"<b>Error Class:</b> <code>{error_class}</code>\n"
                f"<b>Details:</b> <code>{html.escape(str(e))}</code>\n\n"
                f"<b>Traceback snippet:</b>\n"
                f"<code>{html.escape(tb_str[-400:])}</code>"
                f"</blockquote>"
            )

            await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content=diagnostic_html, reply_markup=channel_kb)
            return

    if args and args[0].startswith("view_"):
        asyncio.create_task(_delete_silent(context.bot, update.message.chat_id, update.message.message_id))
        channel_username = CONFIG.get("channel", "EthiopiaEntranceExam").lstrip('@')
        channel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("📣 TO CHANNEL", url=f"https://t.me/{channel_username}")]])
        try:
            display_id = int(args[0][5:])
        except ValueError:
            await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content="⚠️ Invalid reference link.", reply_markup=channel_kb)
            return

        track, question_data = await asyncio.to_thread(db_get_track_and_question, display_id)
        if not track or not question_data:
            await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content="⚠️ This quiz reference could not be found.", reply_markup=channel_kb)
            return

        existing_response = await asyncio.to_thread(db_get_user_response, user_id, track['message_id'])

        # Clear out any earlier "no answer yet" nudge for this exact (user, question)
        # before doing anything else — stops the DM from accumulating a repeat
        # nudge every single time the user taps the link again from the channel.
        from src.config import NO_ANSWER_NUDGE_MIDS
        nudge_key = (user_id, display_id)
        prev_nudge_mid = NO_ANSWER_NUDGE_MIDS.pop(nudge_key, None)
        if prev_nudge_mid:
            asyncio.create_task(_delete_silent(context.bot, update.message.chat_id, prev_nudge_mid))

        if not existing_response:
            is_closed = track.get('status') in ('closed', 'tournament_closed', 'deleted')
            nudge_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📣 TO CHANNEL", url=f"https://t.me/{channel_username}")],
                [InlineKeyboardButton("👤 MY PROFILE", callback_data="privacy_menu|0")]
            ])
            if is_closed:
                nudge_text = f"🏁 <b>REF {display_id} is already closed.</b>\n\nSubmissions ended before you answered this one — no worries, there's always the next question!"
            else:
                nudge_text = f"📭 <b>No answer on file yet for REF {display_id}.</b>\n\nTap one of the options under the question in the channel first!"
            nudge_msg = await send_rich_message_safe(
                context.bot, chat_id=update.message.chat_id,
                html_content=nudge_text,
                reply_markup=nudge_kb
            )
            NO_ANSWER_NUDGE_MIDS[nudge_key] = nudge_msg.message_id
            nudge_ttl = await asyncio.to_thread(db_get_bot_state, "no_answer_nudge_ttl_seconds", 45)
            # Ephemeral by design — this is just a nudge, not a record worth keeping.
            asyncio.create_task(_delayed_delete(context.bot, update.message.chat_id, nudge_msg.message_id, delay_seconds=nudge_ttl))
            return

        perf_card = await asyncio.to_thread(process_user_score, user_id, track['message_id'], question_data['id'], existing_response['is_correct'], existing_response['selected_option'])
        explanation_html = UIFactory.build_answered_view(
            question_data, str(display_id), existing_response['selected_option'],
            show_derivation=existing_response.get('show_derivation', False),
            show_perf=existing_response.get('show_perf', False),
            perf_card=perf_card
        )
        kb = UIFactory.build_answered_keyboard(display_id, existing_response['selected_option'], existing_response.get('show_derivation', False), existing_response.get('show_perf', False), is_photo=False, message_id=track['message_id'])
        m = await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content=explanation_html, reply_markup=kb)
        await asyncio.to_thread(db_update_private_message_id, user_id, track['message_id'], m.message_id)
        return

    if args and args[0].startswith("ref_"):
        referrer_token = args[0][4:].strip()
        referrer_id = await asyncio.to_thread(db_get_user_id_by_referral_token, referrer_token)
        if referrer_id:
            linked = await asyncio.to_thread(db_set_user_referrer, user_id, referrer_id)
            if linked:
                print(f"[REFERRAL] User {user_id} linked to referrer {referrer_id}.", flush=True)
                ref_count = await asyncio.to_thread(db_count_referrals, referrer_id)
                # Notify for the first 10 individually, then only every 5th afterward —
                # keeps power-inviters from getting flooded with notifications.
                should_notify = (ref_count <= 10) or (ref_count % 5 == 0)
                if should_notify:
                    try:
                        new_name = html.escape(user.first_name or user.username or "A student")
                        referral_nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("👤 GO TO PROFILE", callback_data="privacy_menu|0")]])
                        await context.bot.send_message(
                            chat_id=int(referrer_id),
                            text=(
                                f"🤝 <b>{new_name}</b> joined using your invite link!\n"
                                f"You'll earn bonus marks from their correct answers.\n\n"
                                f"📊 Total referrals so far: <b>{ref_count}</b>"
                            ),
                            parse_mode="HTML",
                            reply_markup=referral_nav_kb
                        )
                    except Exception:
                        pass

    if args and args[0].startswith("join_"):
        join_token = args[0][5:].strip()
        from src.database import GLOBAL_ENGINE as _GE
        conn2 = _GE.get_db_connection()
        try:
            with conn2.cursor() as cur2:
                cur2.execute("SELECT org_name, org_type, description FROM organizations WHERE join_token = %s AND deleted_at IS NULL;", (join_token,))
                preview = cur2.fetchone()
        finally:
            _GE.release_connection(conn2)

        if not preview:
            await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content="⚠️ This invite link is invalid or the team no longer exists.")
            return

        # THE FIX: invite links used to join instantly with zero confirmation. Now shows
        # a welcome card and requires an explicit tap — the actual join only happens in
        # the confirm_team_invite callback below.
        confirm_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ JOIN {preview['org_name'][:24]}", callback_data=f"confirm_team_invite|{join_token}")],
            [InlineKeyboardButton("❌ NO THANKS", callback_data="close_portal|0")]
        ])
        desc_line = f"\n\n<i>{html.escape(preview['description'])}</i>" if preview.get('description') else ""
        await send_rich_message_safe(
            context.bot, chat_id=update.message.chat_id,
            html_content=(
                f"🤝 <b>You've been invited to join {html.escape(preview['org_name'])}!</b>{desc_line}\n\n"
                f"Team up, answer together, and climb the boards side by side. Tap below to join."
            ),
            reply_markup=confirm_kb
        )
        return

        if False and args and args[0].startswith("join_"):
            join_token = args[0][5:].strip()
            pre_existing_profile = await asyncio.to_thread(db_get_user_profile, user_id)
            is_new_to_bot = pre_existing_profile is None
            join_data = await asyncio.to_thread(db_join_organization_by_token, user_id, join_token)
        if not join_data:
            msg = "⚠️ This team invite link is invalid or the team no longer exists."
        elif join_data.get("already_member"):
            msg = f"ℹ️ You're already on <b>{join_data['org_name']}</b> as <b>{join_data['role_assigned'].title()}</b> — nothing to do here."
        elif join_data.get("already_pending"):
            msg = f"📥 Your join request for <b>{join_data['org_name']}</b> is still pending admin approval."
        elif join_data["role_assigned"] == "pending":
            msg = f"📥 <b>Request sent!</b> <b>{join_data['org_name']}</b> requires admin approval."
            # THE FIX: this deep-link join path computed "pending" but never told any
            # admin — the request just sat in the database with nobody notified.
            await _notify_org_admins_pending_request(context, join_data["org_id"], join_data["org_name"], user)
        else:
            msg = f"✅ <b>You're in!</b> You're now registered under <b>{join_data['org_name']}</b>."

        # Team admin referral credit — ONLY if this person had never used the bot before.
        # A user who already has an account wasn't "brought in" by this link, so crediting
        # here would let a team admin farm points by re-sharing links to existing users.
        creator_id = join_data.get("creator_id") if join_data else None
        if is_new_to_bot and creator_id and str(creator_id) != str(user_id):
            linked = await asyncio.to_thread(db_set_user_referrer, user_id, creator_id)
            if linked:
                try:
                    ref_count = await asyncio.to_thread(db_count_referrals, creator_id)
                    new_name = html.escape(user.first_name or user.username or "A student")
                    should_notify = (ref_count <= 10) or (ref_count % 5 == 0)
                    if should_notify:
                        referral_nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("👤 GO TO PROFILE", callback_data="privacy_menu|0")]])
                        await context.bot.send_message(
                            chat_id=int(creator_id),
                            text=(
                                f"🤝 <b>{new_name}</b> joined the bot through your team's invite link!\n"
                                f"You'll earn bonus marks from their correct answers.\n\n"
                                f"📊 Total referrals so far: <b>{ref_count}</b>"
                            ),
                            parse_mode="HTML",
                            reply_markup=referral_nav_kb
                        )
                except Exception:
                    pass

        await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content=msg)
    
    if args and args[0].startswith("tourney_"):
        # "FULL RESULTS IN DM" now opens the regular leaderboard screen
        # directly, instead of the separate tournament-only ranking view.
        await leaderboard_command(update, context)
        return

    # Setup is considered complete once city+country are on file — grade is entirely
    # optional (it only affects the challenge-bonus multiplier) and must never gate
    # whether a user is treated as "registered." This was the actual root cause of
    # "setup complete" immediately followed by "you haven't finished setup" — two
    # different, disagreeing definitions of "done" in the same flow.
    profile = await asyncio.to_thread(db_get_user_profile, user_id)
    if profile and profile.get("personal_city") and profile.get("personal_country"):
        asyncio.create_task(_delete_silent(context.bot, update.message.chat_id, update.message.message_id))
        await profile_command(update, context)
        from telegram import BotCommandScopeChat
        try:
            await context.bot.set_my_commands(
                [c for c in BOT_COMMANDS if c.command != "start"],
                scope=BotCommandScopeChat(chat_id=update.effective_chat.id)
            )
        except Exception:
            pass
        return

    keyboard = [
        [InlineKeyboardButton("📍 SET MY LOCATION", callback_data="regloc_start|0")],
        [InlineKeyboardButton("🎒 Grade 6", callback_data="set_grade|6"),
         InlineKeyboardButton("🎒 Grade 8", callback_data="set_grade|8")],
        [InlineKeyboardButton("🎒 Grade 10", callback_data="set_grade|10"),
         InlineKeyboardButton("🎒 Grade 12", callback_data="set_grade|12")],
        [InlineKeyboardButton("📝 SET PUBLIC NICKNAME", callback_data="set_nick_fsm|0")],
        [InlineKeyboardButton("🏰 STUDY ALLIANCE TEAMS", callback_data="alliance_portal|0")],
        [InlineKeyboardButton("📖 HOW IT WORKS", callback_data="full_docs|0")],
        [InlineKeyboardButton("📢 VISIT CHANNEL", url=f"https://t.me/{channel_username}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    asyncio.create_task(_delete_silent(context.bot, update.message.chat_id, update.message.message_id))
    # THE FIX: this text was DESCRIBED in an earlier pass as the new welcome message but
    # was never actually written into this file — the old generic text was still live.
    await send_rich_message_safe(
        context.bot,
        chat_id=update.message.chat_id,
        html_content=(
            "👋 <b>Welcome — you just found something good.</b>\n\n"
            "Bite-sized challenges land in the channel day and night — math, science, "
            "language, logic, a bit of everything. Tap an answer, get scored instantly, "
            "see the full breakdown.\n\n"
            "<blockquote>"
            "🔥 Build a streak, climb the ranks\n"
            "⚔️ Jump into live tournaments\n"
            "🏫 Team up and compete together\n"
            "🕵️ Your identity stays private unless you choose otherwise"
            "</blockquote>\n\n"
            "One thing first — tap <b>📍 SET MY LOCATION</b> below so we can unlock your "
            "scoreboard. Takes 20 seconds. 🎒 Grade is optional, anytime."
        ),
        reply_markup=reply_markup
    )

async def profile_command(update: Update, context):
    """Bypasses start and opens student dynamic Privacy & Consent dashboard."""
    user = update.effective_user
    user_id = user.id

    await asyncio.to_thread(db_update_user_telegram_info, user_id, user.username, user.first_name)
    profile = await asyncio.to_thread(db_get_user_profile, user_id)

    if not profile or not profile.get("personal_city") or not profile.get("personal_country"):
        await update.message.reply_text("📍 Please type /start first to set your city and country.")
        return

    asyncio.create_task(_delete_silent(context.bot, update.message.chat_id, update.message.message_id))

    org_id = profile.get("org_id")
    from src.database import db_get_user_subject_marks, db_get_user_top_topic, db_get_user_rank_summary
    subject_marks = await asyncio.to_thread(db_get_user_subject_marks, user_id)
    top_topic = await asyncio.to_thread(db_get_user_top_topic, user_id)
    rank_summary = await asyncio.to_thread(db_get_user_rank_summary, user_id)
    text = build_profile_card_text(profile, None, subject_marks, top_topic, rank_summary)
    from src.rendering.html_views import build_profile_main_keyboard
    kb = build_profile_main_keyboard(has_team=bool(profile.get("team_id")))
    await _open_utility_view(context, user_id, update.message.chat_id, text, kb)

async def school_command(update: Update, context):
    """Shortcut: /school <TAG> joins (or requests to join) an existing school team by its Team Code."""
    user = update.effective_user
    user_id = user.id

    await asyncio.to_thread(db_update_user_telegram_info, user_id, user.username, user.first_name)
    asyncio.create_task(_delete_silent(context.bot, update.message.chat_id, update.message.message_id))

    nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("👤 MY PROFILE", callback_data="privacy_menu|0")]])

    if not context.args:
        await _open_utility_view(
            context, user_id, update.message.chat_id,
            "⚠️ Please specify a team's Code. Example: <code>/school ABYSSINIA</code>\n\n"
            "No code yet, or want to create your own team? Type /profile → 🏰 STUDY ALLIANCE TEAMS.",
            nav_kb
        )
        return

    tag = context.args[0].strip()
    join_data = await asyncio.to_thread(db_join_organization, user_id, tag)

    if join_data and join_data.get("scope_blocked"):
        await _open_utility_view(
            context, user_id, update.message.chat_id,
            f"🔒 <b>{html.escape(join_data['org_name'])}</b> is a dedicated team.\n\n{join_data['reason']}",
            nav_kb
        )
        return

    if not join_data:
        await _open_utility_view(
            context, user_id, update.message.chat_id,
            f"⚠️ No team found with the code <code>#{tag.upper()}</code>. Double-check with your school admin, "
            f"or type /profile → 🏰 STUDY ALLIANCE TEAMS to create your own.",
            nav_kb
        )
        return

    if join_data.get("already_member"):
        await _open_utility_view(
            context, user_id, update.message.chat_id,
            f"ℹ️ <b>You're already on this team!</b>\n\nYou're registered under <b>{join_data['org_name']}</b> "
            f"(<code>#{tag.upper()}</code>) as <b>{join_data['role_assigned'].title()}</b>.",
            nav_kb
        )
        return

    if join_data.get("already_pending"):
        from src.geo import format_local_time
        viewer_tz = await asyncio.to_thread(db_get_user_timezone, user_id)
        last_req = format_local_time(join_data.get("last_requested_at"), viewer_tz) if join_data.get("last_requested_at") else "recently"
        resend_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔁 SEND AGAIN", callback_data=f"resend_join|{join_data['org_id']}")],
            [InlineKeyboardButton("❌ DON'T SEND", callback_data="privacy_menu|0")]
        ])
        await _open_utility_view(
            context, user_id, update.message.chat_id,
            f"📥 <b>Request already pending.</b>\n\n"
            f"You requested to join <b>{join_data['org_name']}</b> (<code>#{tag.upper()}</code>) "
            f"{join_data.get('request_count', 1)}× — last on {last_req}. Still waiting on admin approval.\n\n"
            f"Want to nudge the admins again?",
            resend_kb
        )
        return

    if join_data["role_assigned"] == "pending":
        await _notify_org_admins_pending_request(context, join_data["org_id"], join_data["org_name"], user)
        await _open_utility_view(
            context, user_id, update.message.chat_id,
            f"📥 <b>Request sent!</b> <b>{join_data['org_name']}</b> (<code>#{tag.upper()}</code>) requires admin "
            f"approval — you'll be added to the roster once approved.",
            nav_kb
        )
    else:
        await _open_utility_view(
            context, user_id, update.message.chat_id,
            f"✅ <b>You're in!</b> You're now registered under <b>{join_data['org_name']}</b> "
            f"(<code>#{tag.upper()}</code>). Every correct answer you submit now also scores for your team!",
            nav_kb
        )

async def name_command(update: Update, context):
    """Sets a custom scoreboard nickname for the player."""
    user = update.effective_user
    user_id = user.id

    await asyncio.to_thread(db_update_user_telegram_info, user_id, user.username, user.first_name)
    asyncio.create_task(_delete_silent(context.bot, update.message.chat_id, update.message.message_id))

    nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("👤 MY PROFILE", callback_data="privacy_menu|0")]])

    if not context.args:
        await _open_utility_view(
            context, user_id, update.message.chat_id,
            "📝 <b>How to set your Public Scoreboard Name:</b>\n\n"
            "Type <code>/name YOUR_NICKNAME</code> to set a custom scoreboard nickname!\n"
            "<i>Example:</i> <code>/name Einstein_12</code>\n\n"
            "If you want to clear your custom nickname and use your Telegram username or first name instead, type <code>/name clear</code>.",
            nav_kb
        )
        return

    nickname = " ".join(context.args).strip()
    if nickname.lower() == "clear":
        await asyncio.to_thread(db_set_user_nickname, user_id, None)
        await _open_utility_view(
            context, user_id, update.message.chat_id,
            "✅ Your custom nickname has been cleared. The system will fall back to your Telegram username or first name on public standings.",
            nav_kb
        )
        return

    clean_name = re.sub(r'[^\w\s\-@]', '', nickname)[:20].strip()
    if not clean_name:
        await _open_utility_view(context, user_id, update.message.chat_id, "⚠️ Invalid nickname format. Please use alphanumeric characters, underscores, or dashes (max 20 characters).", nav_kb)
        return

    success = await asyncio.to_thread(db_set_user_nickname, user_id, clean_name)
    if success:
        await _open_utility_view(
            context, user_id, update.message.chat_id,
            f"✅ <b>Success!</b> Your public display handle has been updated to: <b>{clean_name}</b>.\n"
            f"This name will now be used on round podiums and weekly grade leaderboards! 🏆",
            nav_kb
        )

async def leaderboard_command(update: Update, context):
    user = update.effective_user
    user_id = user.id
    asyncio.create_task(_delete_silent(context.bot, update.message.chat_id, update.message.message_id))
    await asyncio.to_thread(db_update_user_telegram_info, user_id, user.username, user.first_name)

    profile = await asyncio.to_thread(db_get_user_profile, user_id)
    if not profile or not profile.get("personal_city") or not profile.get("personal_country"):
        # Grade was never the real requirement — city+country is, same as every other
        # entry point (profile_command, privacy_menu, profile_popup). This was the last
        # stale grade-gate left in the app, and it's what the tournament "FULL RESULTS IN
        # DM" deep link was hitting since it routes straight into this function.
        nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("📍 SET MY LOCATION", callback_data="regloc_start|0")]])
        await _open_utility_view(
            context, user_id, update.message.chat_id,
            "📍 Please set your city and country first by typing /start.",
            nav_kb
        )
        return

    from src.database import db_get_rank_matrix, db_get_scope_summary
    from src.rendering.html_views import build_leaderboard_text, build_leaderboard_keyboard
    matrix = await asyncio.to_thread(db_get_rank_matrix, "world", None, "all", "all", "all", "total", 10)
    summary = await asyncio.to_thread(db_get_scope_summary, "world", None, "all")
    text = build_leaderboard_text("world", None, "all", "all", "all", "total", matrix, summary)
    kb = build_leaderboard_keyboard("world", None, "all", "all", "all", "total", "none", [], 0)
    await _open_utility_view(context, user_id, update.message.chat_id, text, kb)

async def invite_command(update: Update, context):
    """Generates the student's personal referral deep-link."""
    user = update.effective_user
    user_id = user.id

    asyncio.create_task(_delete_silent(context.bot, update.message.chat_id, update.message.message_id))
    await asyncio.to_thread(db_update_user_telegram_info, user_id, user.username, user.first_name)

    referral_token = await asyncio.to_thread(db_get_or_create_referral_token, user_id)
    bot_username = CONFIG.get("bot_username") or (await context.bot.get_me()).username
    invite_link = f"https://t.me/{bot_username}?start=ref_{referral_token}"

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("👤 MY PROFILE", callback_data="privacy_menu|0"),
        InlineKeyboardButton("🔙 CLOSE", callback_data="close_portal|0")
    ]])

    await _open_utility_view(
        context, user_id, update.message.chat_id,
        (
            "🤝 <b>INVITE FRIENDS, EARN BONUS MARKS!</b>\n\n"
            "Share your link. When a friend joins and answers correctly, you get "
            "<b>+1 Mark</b> per correct answer — and a smaller share two levels deep too.\n\n"
            f"🔗 <b>Your link:</b>\n<code>{invite_link}</code>\n\n"
            f"👆 <i>Tap the link above — Telegram copies it straight to your clipboard.</i>"
        ),
        kb
    )

async def help_command(update: Update, context):
    from src.rendering.html_views import build_help_menu_text, build_help_menu_keyboard
    user_id = update.effective_user.id
    asyncio.create_task(_delete_silent(context.bot, update.message.chat_id, update.message.message_id))
    await _open_utility_view(context, user_id, update.message.chat_id, build_help_menu_text(), build_help_menu_keyboard())

async def feedback_command(update: Update, context):
    from src.rendering.html_views import build_feedback_menu_text
    user_id = update.effective_user.id
    buttons = [[InlineKeyboardButton(label, callback_data=f"fb_cat|{key}")] for key, label in FEEDBACK_CATEGORIES.items()]
    buttons.append([InlineKeyboardButton("📋 MY FEEDBACK & REQUESTS", callback_data="my_feedback|0")])
    buttons.append([
        InlineKeyboardButton("👤 GO TO PROFILE", callback_data="privacy_menu|0"),
        InlineKeyboardButton("❌ CANCEL", callback_data="close_portal|0")
    ])

    asyncio.create_task(_delete_silent(context.bot, update.message.chat_id, update.message.message_id))
    await _open_utility_view(context, user_id, update.message.chat_id, build_feedback_menu_text(), InlineKeyboardMarkup(buttons))


async def myfeedback_command(update: Update, context):
    user = update.effective_user
    user_id = user.id
    await asyncio.to_thread(db_update_user_telegram_info, user_id, user.username, user.first_name)

    from src.database import db_get_user_feedback_and_requests, db_count_user_feedback_and_requests
    from src.rendering.html_views import build_user_feedback_requests_list_text
    items = await asyncio.to_thread(db_get_user_feedback_and_requests, user_id, 5, 0)
    total = await asyncio.to_thread(db_count_user_feedback_and_requests, user_id)
    text = build_user_feedback_requests_list_text(items, total)

    item_rows = []
    for item in items:
        label = str(item['label'])[:24]
        if item['kind'] == 'feedback':
            item_rows.append([InlineKeyboardButton(f"#{item['id']} · {label}", callback_data=f"fb_view|{item['id']}|0")])
        else:
            item_rows.append([InlineKeyboardButton(f"#{item['id']} · {label}", callback_data=f"loc_user_item|{item['id']}|0")])
    if total > 5:
        item_rows.append([InlineKeyboardButton("NEXT ➡️", callback_data="my_feedback|5")])
    item_rows.append([InlineKeyboardButton("🔙 BACK TO FEEDBACK MENU", callback_data="fb_menu|0")])
    item_rows.append([InlineKeyboardButton("👤 BACK TO PROFILE", callback_data="privacy_menu|0")])

    asyncio.create_task(_delete_silent(context.bot, update.message.chat_id, update.message.message_id))
    await _open_utility_view(context, user_id, update.message.chat_id, text, InlineKeyboardMarkup(item_rows))

async def build_user_directory_text(users: list) -> str:
    """Rich-text paginated user list for the admin dashboard."""
    if not users:
        return "<h2>👥 USER DIRECTORY</h2>\n<hr/>\n<i>No users found.</i>"

    rows = []
    for u in users:
        name = format_public_name(u)
        rows.append(
            f"<tr><td>{html.escape(name)}</td><td>Gr.{u.get('grade') or '-'}</td>"
            f"<td>{u.get('total_marks', 0)}</td><td>{html.escape(str(u.get('country', '-')))}</td></tr>"
        )

    return (
        "<h2>👥 USER DIRECTORY (Most Recently Active)</h2>\n<hr/>\n"
        "<table><tr><td><b>Name</b></td><td><b>Grade</b></td><td><b>Marks</b></td><td><b>Country</b></td></tr>"
        + "".join(rows) + "</table>"
    )

async def claim_admin_command(update: Update, context):
    """Hidden, unlisted command. Grants admin only if the caller supplies the exact bootstrap secret."""
    from src.config import ADMIN_BOOTSTRAP_SECRET
    from src.database import db_claim_admin

    user_id = update.effective_user.id
    await asyncio.to_thread(context.bot.delete_message, update.message.chat_id, update.message.message_id)  # scrub the secret from chat history immediately

    if not ADMIN_BOOTSTRAP_SECRET or not context.args or context.args[0] != ADMIN_BOOTSTRAP_SECRET:
        return  # silent failure — no hint given to anyone probing this command

    await asyncio.to_thread(db_claim_admin, user_id)
    await context.bot.send_message(chat_id=user_id, text="✅ Admin access granted.")

async def feedback_admin_command(update: Update, context):
    user_id = update.effective_user.id
    from src.database import db_is_admin
    if not await asyncio.to_thread(db_is_admin, user_id):
        return
    stats = await asyncio.to_thread(db_get_feedback_stats)
    from src.config import FEEDBACK_CATEGORIES
    buttons = [[InlineKeyboardButton(label, callback_data=f"fb_browse|{key}|open:0")] for key, label in FEEDBACK_CATEGORIES.items()]
    buttons.append([InlineKeyboardButton("📋 ALL OPEN ITEMS", callback_data="fb_browse|all|open:0")])
    await send_rich_message_safe(
        context.bot, chat_id=update.message.chat_id,
        html_content=build_feedback_stats_text(stats),
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def _notify_admins_new_feedback(context, fb_id: int, fb: dict):
    from src.database import db_get_all_admin_ids
    admin_ids = await asyncio.to_thread(db_get_all_admin_ids)
    if not admin_ids:
        return
    text = build_feedback_item_text(fb)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔧 In Progress", callback_data=f"fb_status|{fb_id}|in_progress"),
         InlineKeyboardButton("🗓️ Planned", callback_data=f"fb_status|{fb_id}|planned")],
        [InlineKeyboardButton("✅ Resolved", callback_data=f"fb_status|{fb_id}|resolved"),
         InlineKeyboardButton("🚫 Not Planned", callback_data=f"fb_status|{fb_id}|wontfix")],
        [InlineKeyboardButton("💬 Reply to User", callback_data=f"fb_reply|{fb_id}")],
        [InlineKeyboardButton("🔙 BACK TO DASHBOARD", callback_data="admin_dashboard|0")]
    ])
    for admin_id in admin_ids:
        try:
            from src.rendering.rich_helpers import open_utility_view
            await open_utility_view(context.bot, LAST_UTILITY_MID, _UTILITY_LOCKS, admin_id, admin_id, f"🆕 <b>NEW FEEDBACK</b>\n\n{text}", kb)
        except Exception:
            pass

async def feedback_admin_command(update: Update, context):
    user_id = update.effective_user.id
    from src.database import db_is_admin
    if not await asyncio.to_thread(db_is_admin, user_id):
        return
    asyncio.create_task(_delete_silent(context.bot, update.message.chat_id, update.message.message_id))
    stats = await asyncio.to_thread(db_get_feedback_stats)
    from src.config import FEEDBACK_CATEGORIES
    buttons = [[InlineKeyboardButton(label, callback_data=f"fb_browse|{key}|open:0")] for key, label in FEEDBACK_CATEGORIES.items()]
    buttons.append([InlineKeyboardButton("📋 ALL OPEN ITEMS", callback_data="fb_browse|all|open:0")])
    buttons.append([InlineKeyboardButton("🔙 BACK TO DASHBOARD", callback_data="admin_dashboard|0")])
    await _open_utility_view(context, user_id, update.message.chat_id, build_feedback_stats_text(stats), InlineKeyboardMarkup(buttons))

async def admin_dashboard_command(update: Update, context):
    user_id = update.effective_user.id
    from src.database import db_is_admin, db_get_admin_dashboard_stats
    if not await asyncio.to_thread(db_is_admin, user_id):
        return
    asyncio.create_task(_delete_silent(context.bot, update.message.chat_id, update.message.message_id))
    from src.rendering.html_views import build_admin_dashboard_text
    stats = await asyncio.to_thread(db_get_admin_dashboard_stats)
    text = build_admin_dashboard_text(stats)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 VIEW USER DIRECTORY", callback_data="admin_users|0"),
         InlineKeyboardButton("💬 VIEW FEEDBACK", callback_data="fb_browse|all|open:0")],
        [InlineKeyboardButton("📍 LOCATION & SCHOOL REQUESTS", callback_data="loc_admin_browse|all|pending:0")],
        # Was missing here — callbacks.py's admin_dashboard action already had this button,
        # but the /admin_dashboard COMMAND built a separate copy of the keyboard that never
        # got it added. This is the entire reason "Kanban doesn't work" — the button to
        # reach it just wasn't present when you opened the dashboard via the command.
        [InlineKeyboardButton("🗂️ FEEDBACK KANBAN", callback_data="fb_kanban|0")],
        [InlineKeyboardButton("📚 ALL QUESTIONS", callback_data="admin_questions|all:all:0")],
        [InlineKeyboardButton("👤 MY PROFILE", callback_data="privacy_menu|0")]
    ])
    await _open_utility_view(context, user_id, update.message.chat_id, text, kb)

def db_get_all_admin_ids():
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM user_stats WHERE is_admin = TRUE;")
            return [r["user_id"] for r in cur.fetchall()]
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch admin ids: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

# --- CONVERSATIONAL FSM INPUT STATE PROCESSOR ---

async def _delete_silent(bot, chat_id, mid):
    try:
        await bot.delete_message(chat_id=chat_id, message_id=mid)
    except Exception:
        pass

async def _open_utility_view(context, user_id, chat_id, html_content, reply_markup=None):
    """Ensures only one 'utility' message (profile/leaderboard/invite/help/feedback/alliance/
    admin panel) is ever visible in a user's DM at a time."""
    from src.rendering.rich_helpers import open_utility_view
    return await open_utility_view(context.bot, LAST_UTILITY_MID, _UTILITY_LOCKS, user_id, chat_id, html_content, reply_markup)


async def handle_pin_service_message(update: Update, context):
    """Telegram auto-inserts a 'X pinned a message' service notification every time
    pin_chat_message succeeds. If the pinned message is later deleted (round-complete
    cleanup, cooldown card, etc.) without touching this service message, it turns into
    a permanent 'pinned Deleted message' artifact in the chat history. Deleting it
    immediately after it's posted removes the artifact at the source."""
    msg = update.channel_post or update.message
    if not msg:
        return
    try:
        await context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.message_id)
    except Exception:
        pass

async def _delayed_delete(bot, chat_id, message_id, delay_seconds: int = 10800):
    """Deletes a message after a delay — used for ephemeral notifications
    (e.g. 'your feedback was resolved') that should stay visible for a while but
    not clutter the chat forever. The record stays fully visible in /myfeedback's
    history — this only removes the transient push-notification bubble.
    Default: 3 hours."""
    try:
        await asyncio.sleep(delay_seconds)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass



async def _fsm_advance(context, chat_id, edit_mid, html_content, reply_markup=None):
    """Edits the SAME prompt message across an entire multi-step flow (nickname, org
    creation, location) instead of sending a fresh message each step — the whole
    conversation reads as one continuously-updating card."""
    if edit_mid:
        try:
            m = await edit_rich_message_safe(context.bot, chat_id=chat_id, message_id=edit_mid, html_content=html_content, reply_markup=reply_markup)
            return m.message_id if m else edit_mid
        except Exception:
            pass
    m = await send_rich_message_safe(context.bot, chat_id=chat_id, html_content=html_content, reply_markup=reply_markup)
    return m.message_id if m else None

async def handle_fsm_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Global message filter intercepting user response messages for active state configurations.
    Also silently deletes any stray text/attachment the user sends outside of an active input
    flow — a user's DM should only ever contain quiz questions, answer explanation cards, and
    utility panels, never loose typed clutter or accidental attachments."""
    # THE FIX: user_id was referenced by the permission check BEFORE it was ever assigned —
    # this raised NameError on every single text message sent to the bot, in every FSM state,
    # for every user. This is why the permission system, as it stood, would have broken the
    # entire text-input pipeline (nicknames, feedback text, city entry, everything).
    user = update.effective_user
    user_id = user.id
    from src.database import db_check_user_permission
    if not await asyncio.to_thread(db_check_user_permission, user_id, "bot_access"):
        await _delete_silent(context.bot, update.message.chat_id, update.message.message_id)
        return
    state = USER_STATES.get(user_id)

    if not state or state == "IDLE":
        await _delete_silent(context.bot, update.message.chat_id, update.message.message_id)
        return

    if not update.message.text:
        # Every active FSM state here only ever expects a plain text reply — anything
        # else (photo, document, sticker, etc.) sent mid-flow is stray, not a valid answer.
        await _delete_silent(context.bot, update.message.chat_id, update.message.message_id)
        return

    text_input = update.message.text.strip()
    session = USER_PAYLOADS.get(user_id, {})
    edit_mid = session.get("edit_mid")

    # Every keystroke-driven message gets removed right away — the whole flow
    # should feel like one card being updated, not a growing chat log.
    await _delete_silent(context.bot, update.message.chat_id, update.message.message_id)

    profile_nav_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("👤 OPEN PROFILE DASHBOARD", callback_data="privacy_menu|0")
    ]])
    cancel_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ CANCEL & RETURN", callback_data="fsm_cancel|privacy_menu")
    ]])

    if text_input.lower() == "/cancel":
        USER_STATES[user_id] = "IDLE"
        USER_PAYLOADS.pop(user_id, None)

        # Delete the in-flight prompt outright — no "Action cancelled" stub left behind.
        if edit_mid:
            asyncio.create_task(_delete_silent(context.bot, update.message.chat_id, edit_mid))
        from src.config import LAST_UTILITY_MID
        LAST_UTILITY_MID.pop(user_id, None)
        return

    try:
        if state == "AWAITING_NICKNAME":
            clean_name = re.sub(r'[^\w\s\-@]', '', text_input)[:20].strip()
            if not clean_name:
                await _fsm_advance(context, update.message.chat_id, edit_mid, "⚠️ Invalid format. Only letters, spaces and underscores are allowed.\n\n<i>Try again, or /cancel.</i>", cancel_kb)
                return

            profile = await asyncio.to_thread(db_get_user_profile, user_id)
            old_nick = profile.get("nickname") if profile else None
            USER_PAYLOADS[user_id] = {"edit_mid": edit_mid, "pending_nickname": clean_name}
            USER_STATES[user_id] = "IDLE"

            old_line = f"<b>{old_nick}</b>" if old_nick else "<i>none set</i>"
            confirm_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ CONFIRM", callback_data="confirm_nick|1"),
                 InlineKeyboardButton("❌ CANCEL", callback_data="confirm_nick|0")]
            ])
            await _fsm_advance(
                context, update.message.chat_id, edit_mid,
                (
                    "✍️ <b>CONFIRM NICKNAME CHANGE</b>\n<hr/>\n"
                    f"From: {old_line}\n"
                    f"To: <b>{clean_name}</b>\n\n"
                    "This appears on public leaderboards and round podiums. Confirm?"
                ),
                confirm_kb
            )

        elif state == "AWAITING_ORG_NAME":
            clean_org_name = re.sub(r'[^\w\s\-]', '', text_input)[:50].strip()
            is_regloc = bool(session.get("reg_city") and session.get("reg_country"))
            back_target = "regloc_school_page|0" if is_regloc else "alliance_portal|0"
            local_cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data=back_target)]])

            if not clean_org_name:
                await _fsm_advance(context, update.message.chat_id, edit_mid, "⚠️ Invalid formal name.\n\n<i>Try again, or /cancel.</i>", local_cancel_kb)
                return

            if is_regloc:
                # Schools and study-alliance TEAMS both live in `organizations`, but they're
                # conceptually separate — a school just needs a name, and schools legitimately
                # share common name fragments ("Noone Primary" vs "Noone High"). The fuzzy
                # ILIKE "similar teams" check below is for TEAM creation only; a school only
                # gets blocked on an EXACT name+city match (a genuine duplicate), never a
                # partial one.
                from src.database import db_search_schools, _generate_unique_org_tag
                reg_city = session.get("reg_city")
                reg_country = session.get("reg_country")
                exact_candidates = await asyncio.to_thread(db_search_schools, clean_org_name, reg_city, reg_country, 5)
                exact_hit = next((s for s in exact_candidates if s['org_name'].strip().lower() == clean_org_name.lower()), None)

                if exact_hit:
                    USER_STATES[user_id] = "IDLE"
                    dup_kb = InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"🏫 USE {exact_hit['org_name']}", callback_data=f"regloc_school|{exact_hit['org_id']}")],
                        [InlineKeyboardButton("🔙 BACK", callback_data=back_target)]
                    ])
                    await _fsm_advance(
                        context, update.message.chat_id, edit_mid,
                        (
                            f"⚠️ <b>{html.escape(exact_hit['org_name'])}</b> is already registered in "
                            f"{html.escape(reg_city or '')} — that's the same school, not a new one. "
                            f"Tap below to use it instead."
                        ),
                        dup_kb
                    )
                    return

                # Regloc-created schools never enter a tag — one is derived from the
                # name automatically, and the user goes straight to the review screen.
                auto_tag = await asyncio.to_thread(_generate_unique_org_tag, clean_org_name)
                USER_PAYLOADS[user_id] = {
                    **USER_PAYLOADS.get(user_id, {}),
                    "org_name": clean_org_name,
                    "org_tag": auto_tag,
                    "reg_school_name": clean_org_name,
                    "reg_school_is_new": True,
                    "reg_school_org_id": None,
                    "reg_new_org_tag": auto_tag,
                    "reg_leave_school": False,
                }
                USER_STATES[user_id] = "IDLE"
                from src.callbacks import _regloc_show_review
                await _regloc_show_review(context, update.message.chat_id, edit_mid, user_id)
                return

            similar = await asyncio.to_thread(db_find_similar_organizations, clean_org_name)
            if similar and not session.get("team_scope"):
                USER_PAYLOADS[user_id] = {**USER_PAYLOADS.get(user_id, {}), "org_name": clean_org_name, "edit_mid": edit_mid}
                USER_STATES[user_id] = "IDLE"

                lines = ["🔎 <b>Found similar existing team(s):</b>\n"]
                buttons = []
                for org in similar:
                    loc = f" — {org.get('city','')}, {org.get('country','')}" if org.get('city') else ""
                    lines.append(f"🏫 <b>{org['org_name']}</b> (#{org['org_tag']}){loc}")
                    buttons.append([InlineKeyboardButton(f"🔑 Join #{org['org_tag']} instead", callback_data=f"quickjoin_org|{org['org_id']}")])
                buttons.append([InlineKeyboardButton("✨ No, create new anyway", callback_data="force_create_org|0")])
                buttons.append([InlineKeyboardButton("🔙 BACK", callback_data=back_target)])
                lines.append("\n<i>Joining an existing team keeps everyone's scores together instead of splitting them across duplicates.</i>")

                await _fsm_advance(context, update.message.chat_id, edit_mid, "\n".join(lines), InlineKeyboardMarkup(buttons))
                return

            USER_PAYLOADS[user_id] = {**USER_PAYLOADS.get(user_id, {}), "org_name": clean_org_name}
            USER_STATES[user_id] = "AWAITING_ORG_TAG"
            name_icon = "✨" if session.get("team_scope") else "🏫"
            await _fsm_advance(
                context, update.message.chat_id, edit_mid,
                f"{name_icon} Name Accepted: <b>{clean_org_name}</b>\n\n"
                "✍ Enter a short, uppercase Code Tag identifier (2-15 characters, no spaces):\n"
                "<i>(Example: ABYSSINIA)</i>",
                cancel_kb
            )

        elif state == "AWAITING_ORG_TAG":
            clean_tag = re.sub(r'\W', '', text_input).upper()[:15].strip()
            if len(clean_tag) < 2:
                await _fsm_advance(context, update.message.chat_id, edit_mid, "⚠️ Tag too short. Enter at least 2 alphanumeric characters.\n\n<i>Try again, or /cancel.</i>", cancel_kb)
                return

            USER_PAYLOADS[user_id]["org_tag"] = clean_tag

            prefilled_city = USER_PAYLOADS[user_id].get("org_city")
            prefilled_country = USER_PAYLOADS[user_id].get("org_country")
            # THE FIX: bool("open") is True in Python — a plain truthiness check treated
            # "open" team creation the exact same as a genuinely dedicated (country/city/
            # school-restricted) team. Must explicitly exclude "open".
            is_dedicated_creation = bool(USER_PAYLOADS[user_id].get("team_scope")) and USER_PAYLOADS[user_id].get("team_scope") != "open"

            if prefilled_city and prefilled_country and not is_dedicated_creation:
                org_name = USER_PAYLOADS[user_id]["org_name"]
                USER_PAYLOADS[user_id]["reg_school_name"] = org_name
                USER_PAYLOADS[user_id]["reg_school_is_new"] = True
                USER_PAYLOADS[user_id]["reg_school_org_id"] = None
                USER_PAYLOADS[user_id]["reg_new_org_tag"] = clean_tag
                USER_PAYLOADS[user_id]["reg_leave_school"] = False
                USER_STATES[user_id] = "IDLE"
                from src.callbacks import _regloc_show_review
                await _regloc_show_review(context, update.message.chat_id, edit_mid, user_id)
                return

            if prefilled_city and prefilled_country and is_dedicated_creation:
                USER_STATES[user_id] = "AWAITING_ORG_DESCRIPTION"
                vis_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🌐 OPEN (anyone can join instantly)", callback_data="team_visibility|1")],
                    [InlineKeyboardButton("🔒 APPROVAL REQUIRED (review each request)", callback_data="team_visibility|0")]
                ])
                await _fsm_advance(
                    context, update.message.chat_id, edit_mid,
                    "🎛️ <b>Who can join this team?</b>\n\n"
                    "🌐 <b>Open</b> — anyone can join instantly, no approval needed.\n"
                    "🔒 <b>Approval Required</b> — you (or your admins) approve each join request.",
                    vis_kb
                )
                return

            # THE FIX: an "open" team is open to EVERYONE regardless of location — it never
            # had any business asking for a city/country at all. Only a dedicated team
            # (already returned above) needs one.
            if USER_PAYLOADS[user_id].get("team_scope") == "open":
                USER_PAYLOADS[user_id]["org_city"] = None
                USER_PAYLOADS[user_id]["org_country"] = None
                USER_STATES[user_id] = "AWAITING_ORG_DESCRIPTION"
                vis_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🌐 OPEN (anyone can join instantly)", callback_data="team_visibility|1")],
                    [InlineKeyboardButton("🔒 APPROVAL REQUIRED (review each request)", callback_data="team_visibility|0")]
                ])
                await _fsm_advance(
                    context, update.message.chat_id, edit_mid,
                    f"🔑 Short Domain Code accepted: <code>#{clean_tag}</code>\n\n"
                    "🎛️ <b>Who can join this team?</b>\n\n"
                    "🌐 <b>Open</b> — anyone can join instantly, no approval needed.\n"
                    "🔒 <b>Approval Required</b> — you (or your admins) approve each join request.",
                    vis_kb
                )
                return

            USER_STATES[user_id] = "AWAITING_ORG_CITY"
            await _fsm_advance(
                context, update.message.chat_id, edit_mid,
                (
                    f"🔑 Short Domain Code accepted: <code>#{clean_tag}</code>\n\n"
                    "✍ <b>PROMPT: Team City Location</b>\n"
                    "Please enter the city where your team is based:\n"
                    "<i>(Example: Addis Ababa)</i>"
                ) + FSM_INPUT_HINT,
                cancel_kb
            )

        elif state == "AWAITING_ORG_CITY":
            clean_city = re.sub(r'[^\w\s\-]', '', text_input)[:50].strip()
            if not clean_city:
                await _fsm_advance(context, update.message.chat_id, edit_mid, "⚠️ Invalid city name.\n\n<i>Try again, or /cancel.</i>", cancel_kb)
                return

            USER_PAYLOADS[user_id]["org_city"] = clean_city
            USER_STATES[user_id] = "AWAITING_ORG_COUNTRY"
            await _fsm_advance(
                context, update.message.chat_id, edit_mid,
                (
                    f"🌆 City Accepted: <b>{clean_city}</b>\n\n"
                    "✍ <b>PROMPT: Team Country Location</b>\n"
                    "Please enter the country where your team is based:\n"
                    "<i>(Example: Ethiopia)</i>"
                ) + FSM_INPUT_HINT,
                cancel_kb
            )

        elif state == "AWAITING_ORG_COUNTRY":
            clean_country = re.sub(r'[^\w\s\-]', '', text_input)[:50].strip()
            if not clean_country:
                await _fsm_advance(context, update.message.chat_id, edit_mid, "⚠️ Invalid country name.\n\n<i>Try again, or /cancel.</i>", cancel_kb)
                return

            USER_PAYLOADS[user_id]["org_country"] = clean_country
            USER_STATES[user_id] = "AWAITING_ORG_DESCRIPTION"
            vis_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🌐 OPEN (anyone can join instantly)", callback_data="team_visibility|1")],
                [InlineKeyboardButton("🔒 APPROVAL REQUIRED (review each request)", callback_data="team_visibility|0")]
            ])
            await _fsm_advance(
                context, update.message.chat_id, edit_mid,
                "🎛️ <b>Who can join this team?</b>\n\n"
                "🌐 <b>Open</b> — anyone can join instantly, no approval needed.\n"
                "🔒 <b>Approval Required</b> — you (or your admins) approve each join request.",
                vis_kb
            )

        elif state == "AWAITING_ORG_DESCRIPTION":
            clean_desc = text_input[:300].strip()
            if not clean_desc:
                await _fsm_advance(context, update.message.chat_id, edit_mid, "⚠️ Description can't be empty.\n\n<i>Try again, or /cancel.</i>", cancel_kb)
                return

            org_name = USER_PAYLOADS[user_id]["org_name"]
            org_tag = USER_PAYLOADS[user_id]["org_tag"]
            org_city = USER_PAYLOADS[user_id].get("org_city")
            org_country = USER_PAYLOADS[user_id].get("org_country")
            team_scope = USER_PAYLOADS[user_id].get("team_scope", "open")
            scope_value = USER_PAYLOADS[user_id].get("scope_value")
            is_public_choice = USER_PAYLOADS[user_id].get("is_public", True)

            try:
                if team_scope != "open":
                    await asyncio.to_thread(db_create_dedicated_organization, org_name, org_tag, user_id, team_scope, scope_value, clean_desc, org_city, org_country, is_public_choice)
                else:
                    from src.database import GLOBAL_ENGINE as _GE
                    new_org_id = await asyncio.to_thread(db_create_organization, org_name, org_tag, user_id, "Team", is_public_choice, org_city, org_country)
                    conn2 = _GE.get_db_connection()
                    try:
                        with conn2.cursor() as cur2:
                            cur2.execute("UPDATE organizations SET description = %s WHERE org_id = %s;", (clean_desc, new_org_id))
                            conn2.commit()
                    finally:
                        _GE.release_connection(conn2)

                USER_STATES[user_id] = "IDLE"
                USER_PAYLOADS.pop(user_id, None)
                visibility_line = "🌐 Open — anyone can join instantly" if is_public_choice else "🔒 Approval required to join"
                scope_line = f"🔒 Dedicated to: <b>{html.escape(str(scope_value))}</b>\n{visibility_line}" if team_scope != "open" else visibility_line
                location_line = f"📍 {org_city}, {org_country}\n" if org_city and org_country else "🌍 Open to everyone — no location restriction\n"
                await _fsm_advance(
                    context, update.message.chat_id, edit_mid,
                    f"✅ <b>Team Registered!</b>\n\n"
                    f"✨ <b>{org_name}</b> <code>#{org_tag}</code>\n"
                    f"{scope_line}\n"
                    f"{location_line}\n"
                    f"Share your team's invite link (from the team page) so students can join directly.",
                    profile_nav_kb
                )
            except Exception as e:
                traceback.print_exc()
                print(f"[TEAM-CREATE-ERROR] team_scope={team_scope}, org_tag={org_tag}: {e}", flush=True)
                if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                    await _fsm_advance(context, update.message.chat_id, edit_mid, f"⚠️ Error: <code>#{org_tag}</code> is already taken. Enter a unique tag:", cancel_kb)
                else:
                    await _fsm_advance(context, update.message.chat_id, edit_mid, "⚠️ Setup failed due to a database exception.\n\n<i>Try again, or /cancel.</i>", cancel_kb)

        elif state == "AWAITING_ORG_JOIN":
            clean_tag = re.sub(r'\W', '', text_input).upper().strip()
            join_data = await asyncio.to_thread(db_join_organization, user_id, clean_tag)
            if not join_data:
                await _fsm_advance(context, update.message.chat_id, edit_mid, f"⚠️ Alliance code <code>#{clean_tag}</code> not found. Please enter a valid Tag:", cancel_kb)
                return

            USER_STATES[user_id] = "IDLE"
            USER_PAYLOADS.pop(user_id, None)

            # THE FIX: db_join_organization can return {"scope_blocked": True, ...} for a
            # dedicated team you don't match — a dict with NO "role_assigned" key at all.
            # The old code jumped straight to join_data["role_assigned"] once
            # already_member/already_pending were ruled out, raising exactly
            # KeyError: 'role_assigned' in this state.
            if join_data.get("scope_blocked"):
                response_text = f"🔒 <b>{html.escape(join_data['org_name'])}</b> is a dedicated team.\n\n{join_data['reason']}"
            elif join_data.get("already_member"):
                response_text = f"ℹ️ You're already registered under <b>{join_data['org_name']}</b> (<code>#{clean_tag}</code>) as <b>{join_data['role_assigned'].title()}</b>."
            elif join_data.get("already_pending"):
                response_text = f"📥 Your request to join <b>{join_data['org_name']}</b> (<code>#{clean_tag}</code>) is already pending."
            elif join_data["role_assigned"] == "pending":
                response_text = (
                    f"📥 <b>Request sent!</b>\n\n"
                    f"You requested to join <b>{join_data['org_name']}</b> (<code>#{clean_tag}</code>). "
                    f"The team's admin(s) have been notified."
                )
                await _notify_org_admins_pending_request(context, join_data["org_id"], join_data["org_name"], user)
            else:
                response_text = f"✅ <b>Integrated Successfully!</b> You're now registered under <b>{join_data['org_name']}</b> (<code>#{clean_tag}</code>)."

            await _fsm_advance(context, update.message.chat_id, edit_mid, response_text, profile_nav_kb)

        elif state == "AWAITING_LOCATION_CITY":
            clean_city = re.sub(r'[^\w\s\-]', '', text_input)[:50].strip().title()
            if not clean_city:
                await _fsm_advance(context, update.message.chat_id, edit_mid, "⚠️ Invalid city name.\n\n<i>Try again, or /cancel.</i>", cancel_kb)
                return

            USER_PAYLOADS[user_id]["loc_city"] = clean_city
            USER_STATES[user_id] = "AWAITING_LOCATION_COUNTRY"
            await _fsm_advance(
                context, update.message.chat_id, edit_mid,
                (
                    f"🌆 City Accepted: <b>{clean_city}</b>\n\n"
                    "✍ <b>PROMPT: YOUR COUNTRY</b>\nPlease type the country you're studying in, then tap ➤ send:\n<i>(Example: Ethiopia)</i>"
                ) + FSM_INPUT_HINT,
                cancel_kb
            )

        elif state == "AWAITING_LOCATION_COUNTRY":
            from src.geo import normalize_country_input, find_close_match
            from src.database import db_get_cities_for_country
            raw_country = re.sub(r'[^\w\s\-]', '', text_input)[:50].strip()
            if not raw_country:
                await _fsm_advance(context, update.message.chat_id, edit_mid, "⚠️ Invalid country name.\n\n<i>Try again, or /cancel.</i>", cancel_kb)
                return

            normalized_country, is_exact = normalize_country_input(raw_country)
            typed_city = USER_PAYLOADS[user_id].get("loc_city", "")

            known_cities = await asyncio.to_thread(db_get_cities_for_country, normalized_country)
            matched_city = find_close_match(typed_city, known_cities)

            profile = await asyncio.to_thread(db_get_user_profile, user_id)
            old_city = profile.get("personal_city") if profile else None
            old_country = profile.get("personal_country") if profile else None

            USER_STATES[user_id] = "IDLE"
            old_line = f"{old_city}, {old_country}" if old_city else "<i>not set</i>"

            if matched_city:
                # Known city — normal instant-confirm path.
                USER_PAYLOADS[user_id]["pending_city"] = matched_city
                USER_PAYLOADS[user_id]["pending_country"] = normalized_country
                suggestion_note = "" if (is_exact and matched_city.lower() == typed_city.strip().lower()) else "\n<i>(Matched to closest known place — cancel and re-enter if wrong)</i>"
                confirm_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ CONFIRM", callback_data="confirm_location|1"),
                     InlineKeyboardButton("❌ CANCEL", callback_data="confirm_location|0")]
                ])
                await _fsm_advance(
                    context, update.message.chat_id, edit_mid,
                    (
                        "📍 <b>CONFIRM LOCATION CHANGE</b>\n<hr/>\n"
                        f"From: {old_line}\n"
                        f"To: <b>{matched_city}, {normalized_country}</b>{suggestion_note}\n\n"
                        "This powers your City/Country leaderboard placement. Confirm?"
                    ),
                    confirm_kb
                )
                return

            # No close match — this looks like a genuinely new city. Preview it as
            # pending, require a second confirm, then route to admin review.
            USER_PAYLOADS[user_id]["pending_city"] = typed_city
            USER_PAYLOADS[user_id]["pending_country"] = normalized_country
            confirm_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📨 SUBMIT FOR REVIEW", callback_data="confirm_location_pending|1")],
                [InlineKeyboardButton("❌ CANCEL", callback_data="confirm_location|0")]
            ])
            await _fsm_advance(
                context, update.message.chat_id, edit_mid,
                (
                    "📍 <b>NEW LOCATION — PREVIEW</b>\n<hr/>\n"
                    f"From: {old_line}\n"
                    f"To: <b>{typed_city}, {normalized_country}</b>\n\n"
                    "🕵️ We don't recognize this city yet. It'll be set on your profile as "
                    "<b>⏳ Pending</b> right away, and our admins will double-check it shortly — "
                    "they may message you here if they need to confirm the spelling."
                ),
                confirm_kb
            )

        elif state == "AWAITING_ADMIN_LOCATION_REPLY":
            # THE FIX: db_get_user_timezone is already imported at the top of this file.
            # Re-importing it locally HERE (even though this branch doesn't run for
            # AWAITING_USER_LOCATION_REPLY) makes Python treat it as a local name for the
            # WHOLE handle_fsm_message function — every elif branch shares one scope. That's
            # exactly why the other state below crashed with UnboundLocalError: it referenced
            # the same bare name before this branch's import line ever executed on its path.
            from src.database import db_add_location_suggestion_message, db_get_location_suggestion, db_get_location_suggestion_thread
            from src.rendering.html_views import build_location_suggestion_item_text
            sid = session.get("suggestion_id")
            target_user_id = session.get("target_user_id")
            q_text = text_input[:500].strip()
            if not q_text:
                return
            await asyncio.to_thread(db_add_location_suggestion_message, sid, "admin", user_id, q_text)

            # THE FIX: this used to drop straight to IDLE with a dead-end "sent" message —
            # the admin had to re-tap "ASK USER" from the queue every single time to say
            # anything else, and had no way to see the conversation so far without leaving
            # this screen. Now it loops back into the same threaded item view (with the
            # student's message and this one both visible, timestamped) and stays in the
            # SAME reply state, so typing again just keeps the conversation going.
            ls = await asyncio.to_thread(db_get_location_suggestion, sid)
            thread = await asyncio.to_thread(db_get_location_suggestion_thread, sid)
            viewer_tz = await asyncio.to_thread(db_get_user_timezone, user_id)
            thread_text = build_location_suggestion_item_text(ls, thread, viewer_tz) if ls else "✅ Sent."

            USER_PAYLOADS[user_id] = {"suggestion_id": sid, "target_user_id": target_user_id, "edit_mid": edit_mid}
            reply_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("💬 SEND ANOTHER MESSAGE", callback_data=f"loc_review_msg|{sid}")],
                [InlineKeyboardButton("🔙 QUEUE", callback_data="loc_admin_browse|all|pending:0")]
            ])
            await _fsm_advance(context, update.message.chat_id, edit_mid, thread_text, reply_kb)

            student_thread_kb = InlineKeyboardMarkup([[InlineKeyboardButton("💬 REPLY", callback_data=f"loc_user_reply|{sid}")]])
            try:
                await send_rich_message_safe(
                    context.bot, chat_id=int(target_user_id),
                    html_content=f"📍 <b>A question about your location:</b>\n\n<blockquote>{html.escape(q_text)}</blockquote>",
                    reply_markup=student_thread_kb
                )
            except Exception:
                pass

        elif state == "AWAITING_USER_LOCATION_REPLY":
            from src.database import db_add_location_suggestion_message, db_get_all_admin_ids, db_get_location_suggestion, db_get_location_suggestion_thread
            from src.rendering.html_views import build_location_suggestion_item_text
            sid = session.get("suggestion_id")
            reply_text = text_input[:500].strip()
            if not reply_text:
                return
            await asyncio.to_thread(db_add_location_suggestion_message, sid, "user", user_id, reply_text)

            # Same continuity fix as the admin side — loop back into the thread with a
            # REPLY button instead of a dead end, so the student can keep the conversation
            # going without re-navigating from their profile every time.
            ls = await asyncio.to_thread(db_get_location_suggestion, sid)
            thread = await asyncio.to_thread(db_get_location_suggestion_thread, sid)
            viewer_tz_reply = await asyncio.to_thread(db_get_user_timezone, user_id)
            thread_text = build_location_suggestion_item_text(ls, thread, viewer_tz_reply) if ls else "✅ Sent."
            USER_PAYLOADS[user_id] = {"suggestion_id": sid, "edit_mid": edit_mid}
            reply_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("💬 REPLY AGAIN", callback_data=f"loc_user_reply|{sid}")],
                [InlineKeyboardButton("👤 PROFILE", callback_data="privacy_menu|0")]
            ])
            await _fsm_advance(context, update.message.chat_id, edit_mid, thread_text, reply_kb)

            admin_ids = await asyncio.to_thread(db_get_all_admin_ids)
            for admin_id in admin_ids:
                try:
                    review_kb = InlineKeyboardMarkup([[
                        InlineKeyboardButton("💬 VIEW CONVERSATION", callback_data=f"loc_admin_item|{sid}|all:pending:0")
                    ]])
                    await context.bot.send_message(chat_id=int(admin_id), text=f"💬 <b>Student replied on suggestion #{sid}:</b>\n\n<blockquote>{html.escape(reply_text)}</blockquote>", parse_mode="HTML", reply_markup=review_kb)
                except Exception:
                    pass

        elif state == "AWAITING_FEEDBACK_TEXT":
            category = session.get("category", "general")
            clean_msg = text_input[:1000].strip()
            if not clean_msg:
                await _fsm_advance(context, update.message.chat_id, edit_mid, "⚠️ Message can't be empty.\n\n<i>Try again, or /cancel.</i>", cancel_kb)
                return

            fid = await asyncio.to_thread(db_submit_feedback, user_id, category, clean_msg)
            USER_STATES[user_id] = "IDLE"
            USER_PAYLOADS.pop(user_id, None)

            post_submit_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 BACK TO FEEDBACK MENU", callback_data="fb_menu|0")],
                [InlineKeyboardButton("👤 OPEN PROFILE DASHBOARD", callback_data="privacy_menu|0")]
            ])
            await _fsm_advance(
                context, update.message.chat_id, edit_mid,
                f"✅ <b>Thanks! Feedback #{fid} submitted.</b>\n\nYou'll get a message here if there's a reply or update on it.",
                post_submit_kb
            )
            if fid:
                try:
                    fb = await asyncio.to_thread(db_get_feedback_by_id, fid)
                    if fb:
                        await _notify_admins_new_feedback(context, fid, fb)
                except Exception as notify_err:
                    print(f"[FEEDBACK-NOTIFY-ERROR] Failed to notify admins for feedback #{fid}: {notify_err}", flush=True)

        elif state == "AWAITING_ADMIN_REPLY":
            target_fb_id = session.get("fb_id")
            target_user_id = session.get("target_user_id")
            return_state = session.get("return_state")
            reply_text = text_input[:1000].strip()
            if not reply_text:
                return

            await asyncio.to_thread(db_add_feedback_message, target_fb_id, "admin", user_id, reply_text)
            USER_STATES[user_id] = "IDLE"
            USER_PAYLOADS.pop(user_id, None)

            try:
                notify_kb = InlineKeyboardMarkup([[InlineKeyboardButton("💬 VIEW & REPLY", callback_data=f"fb_view|{target_fb_id}|0")]])
                await send_rich_message_safe(
                    context.bot, chat_id=int(target_user_id),
                    html_content=f"💬 <b>Reply to your feedback #{target_fb_id}</b>\n\n<blockquote>{html.escape(reply_text)}</blockquote>",
                    reply_markup=notify_kb
                )
            except Exception:
                pass

            from src.rendering.html_views import build_feedback_thread_text
            from src.callbacks import _build_feedback_detail_keyboard
            fb = await asyncio.to_thread(db_get_feedback_by_id, target_fb_id)
            thread = await asyncio.to_thread(db_get_feedback_thread, target_fb_id)
            viewer_tz = await asyncio.to_thread(db_get_user_timezone, user_id)
            kb = _build_feedback_detail_keyboard(target_fb_id, return_state)
            await _fsm_advance(context, update.message.chat_id, edit_mid, build_feedback_thread_text(fb, thread, viewer_tz), kb)

        elif state == "AWAITING_USER_FEEDBACK_REPLY":
            fb_id = session.get("fb_id")
            return_offset = session.get("return_offset", "0")
            reply_text = text_input[:1000].strip()
            if not reply_text:
                return

            await asyncio.to_thread(db_add_feedback_message, fb_id, "user", user_id, reply_text)
            USER_STATES[user_id] = "IDLE"
            USER_PAYLOADS.pop(user_id, None)

            # THE FIX: db_get_user_timezone is already imported at the top of bot.py.
            # Re-importing it locally HERE made Python treat it as local for the ENTIRE
            # handle_fsm_message function (every elif branch shares one scope) — so
            # AWAITING_USER_LOCATION_REPLY, which runs on a different path and never hits
            # this line, crashed with UnboundLocalError referencing the same bare name.
            # This is the third time this exact class of bug has appeared from a different
            # branch doing a redundant local import — removing it here for good.
            from src.rendering.html_views import build_feedback_thread_text
            fb = await asyncio.to_thread(db_get_feedback_by_id, fb_id)
            thread = await asyncio.to_thread(db_get_feedback_thread, fb_id)
            viewer_tz = await asyncio.to_thread(db_get_user_timezone, user_id)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("💬 REPLY", callback_data=f"fb_user_reply|{fb_id}|{return_offset}")],
                [InlineKeyboardButton("🔙 BACK TO LIST", callback_data=f"my_feedback|{return_offset}")]
            ])
            # THE FIX: viewer_tz was fetched a few lines above but never actually
            # passed into this call — build_feedback_thread_text silently fell back
            # to its "UTC" default every time.
            await _fsm_advance(context, update.message.chat_id, edit_mid, build_feedback_thread_text(fb, thread, viewer_tz), kb)

            try:
                from src.database import db_get_all_admin_ids
                admin_ids = await asyncio.to_thread(db_get_all_admin_ids)
                for admin_id in admin_ids:
                    try:
                        notice_kb = InlineKeyboardMarkup([[InlineKeyboardButton("💬 VIEW & REPLY", callback_data=f"fb_item|{fb_id}|all:all:0")]])
                        await send_rich_message_safe(context.bot, chat_id=admin_id, html_content=f"🆕 <b>New reply on feedback #{fb_id}</b>\n\n<blockquote>{html.escape(reply_text)}</blockquote>", reply_markup=notice_kb)
                    except Exception:
                        pass
            except Exception:
                pass

        elif state == "AWAITING_REGLOC_CITY_TEXT":
            clean_city = re.sub(r'[^\w\s\-,]', '', text_input)[:60].strip().title()
            if not clean_city:
                await _fsm_advance(context, update.message.chat_id, edit_mid, "⚠️ Invalid name. Try again.", None)
                return

            from src.geo import find_close_match
            from src.database import db_get_cities_for_country
            session = USER_PAYLOADS.get(user_id, {})
            reg_country = session.get("reg_country")
            known_cities = await asyncio.to_thread(db_get_cities_for_country, reg_country) if reg_country else []
            matched = find_close_match(clean_city, known_cities)

            final_city = matched or clean_city
            is_new = (matched is None)
            USER_PAYLOADS.setdefault(user_id, {})["reg_city"] = final_city
            USER_PAYLOADS[user_id]["reg_city_is_new"] = is_new
            USER_STATES[user_id] = "IDLE"

            if is_new:
                pending_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ ACCEPT & CONTINUE TO SCHOOL", callback_data="regloc_city_pending_ack|0")],
                    [InlineKeyboardButton("🔙 BACK", callback_data=f"regloc_country|{reg_country}")]
                ])
                await _fsm_advance(
                    context, update.message.chat_id, edit_mid,
                    (
                        f"⏳ <b>Review Needed — register \"{html.escape(final_city)}\" as a city in "
                        f"{html.escape(reg_country or '')}?</b>\n\n"
                        f"We don't have {html.escape(final_city)} on file yet — thanks for helping widen "
                        f"the platform for other students there too!\n\n"
                        f"It's saved to your profile as <b>pending</b> for now. Your marks won't count "
                        f"toward {html.escape(final_city)}'s leaderboard until an admin approves it — "
                        f"we'll message you the moment that happens.\n\n"
                        f"<i>Nothing is sent to admins yet — you'll review and confirm everything together "
                        f"(city + school) at the very end.</i> Tap below to continue with your school."
                    ),
                    pending_kb
                )
                return

            from src.callbacks import _regloc_show_school_step
            await _regloc_show_school_step(context, update.message.chat_id, edit_mid, user_id)

        elif state == "AWAITING_REGLOC_SCHOOL_SEARCH":
            from src.database import db_search_schools
            from src.callbacks import _build_school_kb
            session = USER_PAYLOADS.get(user_id, {})
            results = await asyncio.to_thread(db_search_schools, text_input, session.get("reg_city"), session.get("reg_country"), 8)
            USER_STATES[user_id] = "IDLE"
            if results:
                await _fsm_advance(context, update.message.chat_id, edit_mid, f"🏫 <b>Results:</b>", _build_school_kb(results, 0, len(results), session.get("reg_country")))
            else:
                no_match_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✨ CREATE NEW", callback_data="regloc_school_create|0")],
                    [InlineKeyboardButton("⏭ SKIP", callback_data="regloc_school_skip|0")]
                ])
                await _fsm_advance(context, update.message.chat_id, edit_mid, "🔍 No matches.", no_match_kb)

        elif state == "AWAITING_WR_CITY_TEXT":
            from src.geo import find_close_match
            from src.database import db_get_cities_for_country, db_get_rank_matrix, db_get_scope_summary
            from src.rendering.html_views import build_leaderboard_text, build_leaderboard_keyboard
            purpose = session.get("wr_purpose")
            country = session.get("wr_country")
            typed = text_input.strip()
            known_cities = await asyncio.to_thread(db_get_cities_for_country, country)
            matched = find_close_match(typed, known_cities, cutoff=0.86)

            USER_STATES[user_id] = "IDLE"
            if not matched:
                USER_PAYLOADS.pop(user_id, None)
                not_found_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 PICK FROM LIST", callback_data=f"wrsel_ctry_go|{purpose}|{country}")],
                    [InlineKeyboardButton("📍 ADD IT TO MY PROFILE", callback_data="regloc_start|0")]
                ])
                await _fsm_advance(
                    context, update.message.chat_id, edit_mid,
                    (
                        f"⚠️ <b>\"{html.escape(typed)}\" isn't listed for {html.escape(country)}.</b>\n\n"
                        f"This might be a typo — try picking from the list. If your city genuinely belongs "
                        f"here and isn't registered yet, add it from 📍 <b>Locations &amp; School</b> in your "
                        f"profile first, then it'll show up here."
                    ),
                    not_found_kb
                )
                return

            if purpose == "nav_city":
                matrix = await asyncio.to_thread(db_get_rank_matrix, "city", matched, "all", "all", "all", "total", 10)
                summary = await asyncio.to_thread(db_get_scope_summary, "city", matched, "all")
                text = build_leaderboard_text("city", matched, "all", "all", "all", "total", matrix, summary)
                kb = build_leaderboard_keyboard("city", matched, "all", "all", "all", "total", "none", [], 0)
                USER_PAYLOADS.pop(user_id, None)
                await _fsm_advance(context, update.message.chat_id, edit_mid, text, kb)
                return

            if purpose == "fav_city":
                from src.database import db_add_favorite
                await asyncio.to_thread(db_add_favorite, user_id, "city", matched, f"{matched}, {country}")
                USER_PAYLOADS.pop(user_id, None)
                nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⭐ MY CITY FAVORITES", callback_data="wr_fav_list|city|0")],
                                                [InlineKeyboardButton("🔙 LEADERBOARD", callback_data="wr|world|_|all|all|all|total|none|0")]])
                await _fsm_advance(context, update.message.chat_id, edit_mid, f"⭐ <b>{html.escape(matched)}</b> added to your favorites!", nav_kb)
                return

            from src.database import db_search_schools
            schools = await asyncio.to_thread(db_search_schools, None, matched, country, 20)
            rows = [[InlineKeyboardButton(s["org_name"], callback_data=f"wrsel_school_go|{purpose}|{s['org_id']}")] for s in schools]
            rows.append([InlineKeyboardButton("✍️ TYPE SCHOOL", callback_data=f"wrsel_school_type|{purpose}|{country}|{matched}")])
            rows.append([InlineKeyboardButton("🔙 CITIES", callback_data=f"wrsel_ctry_go|{purpose}|{country}")])
            subtitle = "Pick your school, or type its name if it's not listed:" if schools else "No schools on file yet for this city — type yours:"
            USER_PAYLOADS.pop(user_id, None)
            await _fsm_advance(context, update.message.chat_id, edit_mid, f"<h2>🏫 {html.escape(matched)}</h2>\n{subtitle}", InlineKeyboardMarkup(rows))
            return

        elif state == "AWAITING_WR_SCHOOL_TEXT":
            from src.database import db_search_schools, db_get_rank_matrix, db_get_scope_summary, db_add_favorite
            from src.rendering.html_views import build_leaderboard_text, build_leaderboard_keyboard
            purpose = session.get("wr_purpose")
            country = session.get("wr_country")
            city = session.get("wr_city")
            typed = text_input.strip()
            results = await asyncio.to_thread(db_search_schools, typed, city, country, 8)

            USER_STATES[user_id] = "IDLE"
            if not results:
                USER_PAYLOADS.pop(user_id, None)
                not_found_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 PICK FROM LIST", callback_data=f"wrsel_city_go|{purpose}|{country}|{city}")],
                    [InlineKeyboardButton("📍 ADD IT TO MY PROFILE", callback_data="regloc_start|0")]
                ])
                await _fsm_advance(
                    context, update.message.chat_id, edit_mid,
                    (
                        f"⚠️ <b>\"{html.escape(typed)}\" isn't listed in {html.escape(city)}.</b>\n\n"
                        f"This might be a typo — try picking from the list. If your school genuinely belongs "
                        f"here and isn't registered yet, add it from 📍 <b>Locations &amp; School</b> in your "
                        f"profile first, then it'll show up here."
                    ),
                    not_found_kb
                )
                return

            if len(results) == 1:
                org = results[0]
                USER_PAYLOADS.pop(user_id, None)
                if purpose == "fav_school":
                    await asyncio.to_thread(db_add_favorite, user_id, "school", str(org["org_id"]), org["org_name"])
                    nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⭐ MY SCHOOL FAVORITES", callback_data="wr_fav_list|school|0")],
                                                    [InlineKeyboardButton("🔙 LEADERBOARD", callback_data="wr|world|_|all|all|all|total|none|0")]])
                    await _fsm_advance(context, update.message.chat_id, edit_mid, f"⭐ <b>{html.escape(org['org_name'])}</b> added to your favorites!", nav_kb)
                    return
                matrix = await asyncio.to_thread(db_get_rank_matrix, "school", str(org["org_id"]), "all", "all", "all", "total", 10)
                summary = await asyncio.to_thread(db_get_scope_summary, "school", str(org["org_id"]), "all")
                text = build_leaderboard_text("school", str(org["org_id"]), "all", "all", "all", "total", matrix, summary)
                kb = build_leaderboard_keyboard("school", str(org["org_id"]), "all", "all", "all", "total", "none", [], 0)
                await _fsm_advance(context, update.message.chat_id, edit_mid, text, kb)
                return

            rows = [[InlineKeyboardButton(s["org_name"], callback_data=f"wrsel_school_go|{purpose}|{s['org_id']}")] for s in results]
            rows.append([InlineKeyboardButton("🔙 CITIES", callback_data=f"wrsel_ctry_go|{purpose}|{country}")])
            USER_PAYLOADS.pop(user_id, None)
            await _fsm_advance(context, update.message.chat_id, edit_mid, f"🔍 <b>Matches for \"{html.escape(typed)}\":</b>", InlineKeyboardMarkup(rows))
            return

    except Exception as fsm_err:
        # THE FIX: every FSM state (feedback reply, location-suggestion reply, nickname,
        # school creation, etc.) previously shared one generic message with zero detail —
        # "Connection Error: Failed to commit your input" told you nothing about WHICH
        # state failed or WHY, only the server log had that (and only if someone was
        # watching it live). Now the message itself names the state and the real error,
        # so the next failure is immediately actionable from the chat alone.
        traceback.print_exc()
        print(f"[FSM-ERROR] state={state} user={user_id} session_keys={list(session.keys())}: {fsm_err}", flush=True)
        error_detail = (
            f"\n\n🛠️ <code>{type(fsm_err).__name__}: {html.escape(str(fsm_err))[:150]}</code>\n"
            f"<i>Step: {html.escape(str(state))}</i>"
        )
        await _fsm_advance(
            context, update.message.chat_id, edit_mid,
            f"⚠️ Something went wrong saving that — please try again.{error_detail}",
            profile_nav_kb
        )

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

async def whoami_command(update: Update, context):
    """Anyone can run this — it only echoes back their own ID, never anyone else's."""
    user = update.effective_user
    nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("👤 MY PROFILE", callback_data="privacy_menu|0")]])
    await update.message.reply_text(
        f"🆔 Your Telegram User ID: <code>{user.id}</code>\n\n"
        f"Add this to ADMIN_IDS in your .env (comma-separated) or config.json's "
        f"\"admin_ids\" list, then restart the bot to unlock /admin_dashboard.",
        parse_mode="HTML", reply_markup=nav_kb
    )

async def cancel_command(update, context):
    user_id = update.effective_user.id
    state = USER_STATES.get(user_id)
    asyncio.create_task(_delete_silent(context.bot, update.message.chat_id, update.message.message_id))

    if not state or state == "IDLE":
        nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("👤 MY PROFILE", callback_data="privacy_menu|0")]])
        notice = await context.bot.send_message(chat_id=update.message.chat_id, text="Nothing to cancel right now.", reply_markup=nav_kb)
        asyncio.create_task(_delayed_delete(context.bot, update.message.chat_id, notice.message_id, delay_seconds=6))
        return

    session = USER_PAYLOADS.get(user_id, {})
    edit_mid = session.get("edit_mid")
    USER_STATES[user_id] = "IDLE"
    USER_PAYLOADS.pop(user_id, None)

    if edit_mid:
        asyncio.create_task(_delete_silent(context.bot, update.message.chat_id, edit_mid))
    from src.config import LAST_UTILITY_MID
    LAST_UTILITY_MID.pop(user_id, None)

async def myanswers_command(update: Update, context):
    user = update.effective_user
    user_id = user.id
    await asyncio.to_thread(db_update_user_telegram_info, user_id, user.username, user.first_name)
    asyncio.create_task(_delete_silent(context.bot, update.message.chat_id, update.message.message_id))

    from src.database import db_get_user_subjects_summary, db_is_admin
    from src.rendering.html_views import build_my_answers_subject_menu_text, build_my_answers_subject_keyboard
    viewer_is_admin = await asyncio.to_thread(db_is_admin, user_id)
    summary = await asyncio.to_thread(db_get_user_subjects_summary, user_id, viewer_is_admin)
    text = build_my_answers_subject_menu_text(summary)
    kb = build_my_answers_subject_keyboard(summary)
    await _open_utility_view(context, user_id, update.message.chat_id, text, kb)


BOT_COMMAND_LIST_TEXT = (
    "❓ <b>Unknown command.</b>\n\n"
    "Here's everything I understand:\n"
    "• /start — register or reopen your profile\n"
    "• /profile — score, streak, team, settings\n"
    "• /leaderboard — weekly grade rankings\n"
    "• /leaderboard school — school team rankings\n"
    "• /invite — get your referral link\n"
    "• /feedback — report a bug or share thoughts\n"
    "• /myfeedback — track your submitted feedback\n"
    "• /name YOUR_NICKNAME — set scoreboard name\n"
    "• /school CODE — join a team by code\n"
    "• /cancel — cancel whatever you're doing\n"
    "• /help — full help menu"
)

async def unknown_command_handler(update, context):
    chat_id = update.message.chat_id
    cmd_mid = update.message.message_id

    # Awaited (not fire-and-forget) so the unknown command is guaranteed gone.
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=cmd_mid)
    except Exception:
        pass

    nav_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 MY PROFILE", callback_data="privacy_menu|0"),
         InlineKeyboardButton("📖 HELP MENU", callback_data="help_menu|0")]
    ])
    # No longer auto-deletes — the full command list now stays visible.
    await context.bot.send_message(chat_id=chat_id, text=BOT_COMMAND_LIST_TEXT, parse_mode="HTML", reply_markup=nav_kb)

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
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    app.add_handler(CommandHandler("invite", invite_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("feedback", feedback_command))
    app.add_handler(CommandHandler("myfeedback", myfeedback_command))
    app.add_handler(CommandHandler("feedback_admin", feedback_admin_command))
    app.add_handler(CommandHandler(["admin_dashboard", "admindashboard"], admin_dashboard_command))
    app.add_handler(CommandHandler("claimadmin", claim_admin_command))
    app.add_handler(CommandHandler("school", school_command))
    app.add_handler(CommandHandler("name", name_command))
    app.add_handler(CommandHandler("whoami", whoami_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("myanswers", myanswers_command))
    app.add_handler(CallbackQueryHandler(lambda u, c: handle_callback(update=u, context=c, engine=engine)))
    app.add_handler(MessageHandler(filters.StatusUpdate.PINNED_MESSAGE, handle_pin_service_message))

    app.add_handler(MessageHandler(~filters.COMMAND, handle_fsm_message), group=-1)

    # Must stay LAST — catches every /command not matched above
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command_handler))

    RENDER_PORT = os.getenv("PORT")

    if RENDER_PORT:
        print(f"Starting cloud Webhook listener on port {RENDER_PORT}...", flush=True)
        PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL")

        loop = asyncio.new_event_loop()
        from concurrent.futures import ThreadPoolExecutor
        loop.set_default_executor(ThreadPoolExecutor(max_workers=100))
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
            from telegram import BotCommandScopeChat
            from src.database import db_get_all_admin_ids
            admin_ids = loop.run_until_complete(asyncio.to_thread(db_get_all_admin_ids))
            admin_cmds = BOT_COMMANDS + [BotCommand("admin_dashboard", "View platform stats, users & feedback")]
            for admin_id in admin_ids:
                try:
                    loop.run_until_complete(app.bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=int(admin_id))))
                except Exception:
                    pass
        except Exception as e:
            print(f"{Style.YELLOW}[WARNING] Failed to register admin-only commands: {e}{Style.RESET}", flush=True)

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
        # Prevent the local dashboard from starting loops when attached as TTY Client
        run_cli = sys.stdin.isatty()
        if run_cli:
            print("Starting local Admin Dashboard cockpit...", flush=True)
            print(f"{Style.YELLOW}⚠️  [COLLISION-PREVENTION] Local background loops (scheduler + tournament watcher) are BYPASSED in interactive TTY mode.{Style.RESET}", flush=True)
            
            loop = asyncio.new_event_loop()
            from concurrent.futures import ThreadPoolExecutor
            loop.set_default_executor(ThreadPoolExecutor(max_workers=100))
            asyncio.set_event_loop(loop)
            src.config.ACTIVE_LOOP = loop

            loop.run_until_complete(app.initialize())
            loop.run_until_complete(app.start())

            bot_info = loop.run_until_complete(app.bot.get_me())
            CONFIG["bot_username"] = bot_info.username
            print(f"Quiz Master Pro Admin Client is online and connected to @{bot_info.username}.", flush=True)

            try:
                loop.run_until_complete(app.bot.set_my_commands(BOT_COMMANDS))
                print(f"{Style.GREEN}Registered {len(BOT_COMMANDS)} bot commands for the '/' menu.{Style.RESET}", flush=True)
            except Exception as e:
                print(f"{Style.YELLOW}[WARNING] Failed to register bot commands: {e}{Style.RESET}", flush=True)
            try:
                from telegram import BotCommandScopeChat
                from src.database import db_get_all_admin_ids
                admin_ids = loop.run_until_complete(asyncio.to_thread(db_get_all_admin_ids))
                admin_cmds = BOT_COMMANDS + [BotCommand("admin_dashboard", "View platform stats, users & feedback")]
                for admin_id in admin_ids:
                    try:
                        loop.run_until_complete(app.bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=int(admin_id))))
                    except Exception:
                        pass
            except Exception as e:
                print(f"{Style.YELLOW}[WARNING] Failed to register admin-only commands: {e}{Style.RESET}", flush=True)

            try:
                loop.run_until_complete(admin_panel(app, engine))
            except KeyboardInterrupt:
                pass
            finally:
                loop.run_until_complete(emergency_shutdown_cleanup(app, engine))
                loop.run_until_complete(app.stop())
                loop.run_until_complete(app.shutdown())
                print(f"System successfully shut down.", flush=True)
        else:
            loop = asyncio.new_event_loop()
            from concurrent.futures import ThreadPoolExecutor
            loop.set_default_executor(ThreadPoolExecutor(max_workers=100))
            asyncio.set_event_loop(loop)
            src.config.ACTIVE_LOOP = loop

            loop.run_until_complete(app.initialize())
            loop.run_until_complete(app.start())

            # Only run background watchers on non-interactive instances (Cloud web service container)
            asyncio.ensure_future(check_and_publish_scheduled(app), loop=loop)
            asyncio.ensure_future(tournament_watcher_loop(app, engine, poll_seconds=2), loop=loop)

            bot_info = loop.run_until_complete(app.bot.get_me())
            CONFIG["bot_username"] = bot_info.username

            async def keep_alive():
                while True:
                    await asyncio.sleep(3600)
            try:
                loop.run_until_complete(keep_alive())
            except (KeyboardInterrupt, SystemExit):
                pass
            finally:
                loop.run_until_complete(emergency_shutdown_cleanup(app, engine))
                loop.run_until_complete(app.stop())
                loop.run_until_complete(app.shutdown())
                print(f"System successfully shut down.", flush=True)

if __name__ == "__main__":
    main()



