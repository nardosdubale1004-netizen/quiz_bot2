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
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from src.config import CONFIG, Style, LOCKOUT_MESSAGES, USER_STATES, USER_PAYLOADS, ADMIN_IDS, FEEDBACK_CATEGORIES
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
)
from src.rendering import get_grade_mastery_title, UIFactory, fetch_kroki_image
from src.rendering.html_views import get_next_rank_info, format_public_name, build_profile_card_text, build_feedback_stats_text, build_feedback_item_text, build_user_feedback_list_text
from src.rendering.rich_helpers import send_rich_message_safe, edit_rich_message_safe, convert_to_legacy_html
from src.callbacks import handle_callback
from src.cli import admin_panel
from src.tournament import tournament_watcher_loop, emergency_shutdown_cleanup
import httpx
from telegram import Poll
from src.typography import lite_math

engine = QuizEngine()

# Consolidated commands to simplify the user interface
BOT_COMMANDS = [
    BotCommand("start", "Register your academic profile / level"),
    BotCommand("profile", "Open scoreboard visibility, nickname & school alliance dashboard"),
    BotCommand("leaderboard", "View individual rank standings or school rankings"),
    BotCommand("invite", "Get your referral link & earn bonus marks"),
    BotCommand("help", "How the bot works, step by step"),
    BotCommand("feedback", "Report a bug, request a feature, or share feedback"),
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
        InlineKeyboardButton("📣 RETURN TO CHANNEL", url=f"https://t.me/{channel_username}")
    ]])

    if args and args[0].startswith("ans_"):
        payload = args[0]
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
                InlineKeyboardButton("📣 RETURN TO CHANNEL", url=f"https://t.me/{channel_username}/{track['message_id']}")
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
            await send_rich_message_safe(
                context.bot,
                chat_id=update.message.chat_id,
                html_content="⚠️ <b>Round Closed!</b>\n\nSubmissions are no longer accepted for this tournament question.",
                reply_markup=channel_kb
            )
            return

        # --- TOURNAMENT BRANCH: Answering block ---
        if track_status == "tournament_active":
            try:
                print(f"[TRACE-STEP 3] Active tournament round detected. Reading student history...", flush=True)
                existing_response = await asyncio.to_thread(db_get_user_response, user_id, mid_key)
                if existing_response:
                    print(f" └─ Already Answered: User {user_id} is locked out of further responses.", flush=True)
                    await send_rich_message_safe(
                        context.bot,
                        chat_id=update.message.chat_id,
                        html_content="⚠️ <b>Lockout active!</b>\n\nYou have already submitted your response for this live tournament question. Your selection is locked.",
                        reply_markup=channel_kb
                    )
                    return

                print(f"[TRACE-STEP 4] No history found. Calculating score logic...", flush=True)
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

            if existing_response:
                print(f" ├─ History found. Selected Option: {existing_response.get('selected_option')} | Is Correct: {existing_response.get('is_correct')}", flush=True)
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
                    print(f" ├─ Deleting stale static message ID: {old_private_mid}", flush=True)
                    asyncio.create_task(delete_msg_safe(update.message.chat_id, old_private_mid))

                print(f" ├─ Computing latest scoreboard metadata...", flush=True)
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
                    file_id=cached_file_id
                )

                if media_bytes and m and m.photo and not cached_file_id:
                    await asyncio.to_thread(db_save_cached_file_id, cache_key, m.photo[-1].file_id)

                LOCKOUT_MESSAGES.add((user_id, m.message_id))
                await asyncio.to_thread(db_update_private_message_id, user_id, mid_key, m.message_id)
                print(f"{Style.GREEN}[TRACE-COMPLETE] History fallback view successfully displayed.{Style.RESET}", flush=True)
                return

            print(f"[TRACE-STEP 4] Standard active path. Calculating first-time response...", flush=True)
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
                        await context.bot.send_message(
                            chat_id=int(referrer_id),
                            text=(
                                f"🤝 <b>{new_name}</b> joined using your invite link!\n"
                                f"You'll earn bonus marks from their correct answers.\n\n"
                                f"📊 Total referrals so far: <b>{ref_count}</b>"
                            ),
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

    if args and args[0].startswith("join_"):
        join_token = args[0][5:].strip()
        join_data = await asyncio.to_thread(db_join_organization_by_token, user_id, join_token)
        if join_data:
            if join_data["role_assigned"] == "pending":
                msg = f"📥 <b>Request sent!</b> <b>{join_data['org_name']}</b> requires admin approval."
            else:
                msg = f"✅ <b>You're in!</b> You're now registered under <b>{join_data['org_name']}</b>."
        else:
            msg = "⚠️ This team invite link is invalid or the team no longer exists."
        await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content=msg)

    # Check and render fallback grade profile if mapped
    profile = await asyncio.to_thread(db_get_user_profile, user_id)
    if profile and profile.get("grade"):
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
    await send_rich_message_safe(
        context.bot,
        chat_id=update.message.chat_id,
        html_content=(
            "👋 <b>Welcome to Quiz Master Pro!</b>\n\n"
            "This is your first-time setup — pick your grade level below to unlock your "
            "personal profile, scoreboard, and study team options:\n\n"
            "💡 <i>Tip: Tap the Public Nickname button to set your scoreboard handle! Otherwise, the bot will use your Telegram username or first name.</i>"
        ),
        reply_markup=reply_markup
    )

async def profile_command(update: Update, context):
    """Bypasses start and opens student dynamic Privacy & Consent dashboard."""
    user = update.effective_user
    user_id = user.id

    await asyncio.to_thread(db_update_user_telegram_info, user_id, user.username, user.first_name)
    profile = await asyncio.to_thread(db_get_user_profile, user_id)

    if not profile or not profile.get("grade"):
        await update.message.reply_text("🎒 Please type /start first to configure your basic grade profile details.")
        return

    asyncio.create_task(_delete_silent(context.bot, update.message.chat_id, update.message.message_id))

    org_id = profile.get("org_id")
    from src.database import db_get_user_subject_marks
    subject_marks = await asyncio.to_thread(db_get_user_subject_marks, user_id)
    text = build_profile_card_text(profile, None, subject_marks)
    from src.rendering.html_views import build_profile_main_keyboard
    kb = build_profile_main_keyboard(has_team=bool(org_id))
    await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content=text, reply_markup=kb)
    
async def school_command(update: Update, context):
    """Shortcut: /school <TAG> joins (or requests to join) an existing school team by its Team Code."""
    user = update.effective_user
    user_id = user.id

    await asyncio.to_thread(db_update_user_telegram_info, user_id, user.username, user.first_name)
    asyncio.create_task(_delete_silent(context.bot, update.message.chat_id, update.message.message_id))

    nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("👤 MY PROFILE", callback_data="privacy_menu|0")]])

    if not context.args:
        await update.message.reply_text(
            "⚠️ Please specify a team's Code. Example: <code>/school ABYSSINIA</code>\n\n"
            "No code yet, or want to create your own team? Type /profile → 🏰 STUDY ALLIANCE TEAMS.",
            parse_mode="HTML", reply_markup=nav_kb
        )
        return

    tag = context.args[0].strip()
    join_data = await asyncio.to_thread(db_join_organization, user_id, tag)

    if not join_data:
        await update.message.reply_text(
            f"⚠️ No team found with the code <code>#{tag.upper()}</code>. Double-check with your school admin, "
            f"or type /profile → 🏰 STUDY ALLIANCE TEAMS to create your own.",
            parse_mode="HTML", reply_markup=nav_kb
        )
        return

    if join_data["role_assigned"] == "pending":
        await update.message.reply_text(
            f"📥 <b>Request sent!</b> <b>{join_data['org_name']}</b> (<code>#{tag.upper()}</code>) requires admin "
            f"approval — you'll be added to the roster once the team creator confirms.",
            parse_mode="HTML", reply_markup=nav_kb
        )
    else:
        await update.message.reply_text(
            f"✅ <b>You're in!</b> You're now registered under <b>{join_data['org_name']}</b> "
            f"(<code>#{tag.upper()}</code>). Every correct answer you submit now also scores for your team!",
            parse_mode="HTML", reply_markup=nav_kb
        )

async def name_command(update: Update, context):
    """Sets a custom scoreboard nickname for the player."""
    user = update.effective_user
    user_id = user.id

    await asyncio.to_thread(db_update_user_telegram_info, user_id, user.username, user.first_name)
    asyncio.create_task(_delete_silent(context.bot, update.message.chat_id, update.message.message_id))

    nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("👤 MY PROFILE", callback_data="privacy_menu|0")]])

    if not context.args:
        await update.message.reply_text(
            "📝 <b>How to set your Public Scoreboard Name:</b>\n\n"
            "Type <code>/name YOUR_NICKNAME</code> to set a custom scoreboard nickname!\n"
            "<i>Example:</i> <code>/name Einstein_12</code>\n\n"
            "If you want to clear your custom nickname and use your Telegram username or first name instead, type <code>/name clear</code>.",
            parse_mode="HTML", reply_markup=nav_kb
        )
        return

    nickname = " ".join(context.args).strip()
    if nickname.lower() == "clear":
        await asyncio.to_thread(db_set_user_nickname, user_id, None)
        await update.message.reply_text("✅ Your custom nickname has been cleared. The system will fall back to your Telegram username or first name on public standings.", parse_mode="HTML", reply_markup=nav_kb)
        return

    clean_name = re.sub(r'[^\w\s\-@]', '', nickname)[:20].strip()
    if not clean_name:
        await update.message.reply_text("⚠️ Invalid nickname format. Please use alphanumeric characters, underscores, or dashes (max 20 characters).", reply_markup=nav_kb)
        return

    success = await asyncio.to_thread(db_set_user_nickname, user_id, clean_name)
    if success:
        await update.message.reply_text(
            f"✅ <b>Success!</b> Your public display handle has been updated to: <b>{clean_name}</b>.\n"
            f"This name will now be used on round podiums and weekly grade leaderboards! 🏆",
            parse_mode="HTML", reply_markup=nav_kb
        )

async def leaderboard_command(update: Update, context):
    user = update.effective_user
    user_id = user.id
    asyncio.create_task(_delete_silent(context.bot, update.message.chat_id, update.message.message_id))
    await asyncio.to_thread(db_update_user_telegram_info, user_id, user.username, user.first_name)

    args = context.args
    channel_username = CONFIG.get("channel", "EthiopiaEntranceExam").lstrip('@')
    nav_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📣 RETURN TO CHANNEL", url=f"https://t.me/{channel_username}")],
        [InlineKeyboardButton("👤 MY PROFILE", callback_data="privacy_menu|0"),
         InlineKeyboardButton("🔙 CLOSE", callback_data="close_portal|0")]
    ])

    if args and args[0].lower() == "school":
        alliance_top = await asyncio.to_thread(db_get_alliance_leaderboard)
        leaderboard_text = ["🏆 <b>GLOBAL STUDY ALLIANCE STANDINGS</b> 🏆\n", "🔥 <b>TOP 10 SCHOOLS & CLANS:</b>\n"]
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        if not alliance_top:
            leaderboard_text.append("<i>No schools have registered points yet. Be the first with /school school_name!</i>")
        else:
            for i, row in enumerate(alliance_top):
                leaderboard_text.append(f" {medals[i]} <b>#{row['alliance_tag']}</b> — <b>{row['total_score']} Marks</b> ({row['active_members']} members)")
        await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content="\n".join(leaderboard_text), reply_markup=nav_kb)
        return

    profile = await asyncio.to_thread(db_get_user_profile, user_id)
    if not profile:
        await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content="⚠️ Please register your grade first by typing /start.")
        return

    grade = profile['grade']
    user_marks = profile['total_marks']
    mastery = get_grade_mastery_title(user_marks)
    weekly_top = await asyncio.to_thread(db_get_weekly_leaderboard, grade)

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
        leaderboard_text.append(f" {medals[i]} {format_public_name(row)} — <b>{row['total_score']} Marks</b>")

    await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content="\n".join(leaderboard_text), reply_markup=nav_kb)


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

    await send_rich_message_safe(
        context.bot, chat_id=update.message.chat_id,
        html_content=(
            "🤝 <b>INVITE FRIENDS, EARN BONUS MARKS!</b>\n\n"
            "Share your link. When a friend joins and answers correctly, you get "
            "<b>+1 Mark</b> per correct answer — and a smaller share two levels deep too.\n\n"
            f"🔗 <b>Your link:</b>\n<code>{invite_link}</code>"
        ),
        reply_markup=kb
    )

async def help_command(update: Update, context):
    from src.rendering.html_views import build_help_menu_text, build_help_menu_keyboard
    asyncio.create_task(_delete_silent(context.bot, update.message.chat_id, update.message.message_id))
    await send_rich_message_safe(
        context.bot, chat_id=update.message.chat_id,
        html_content=build_help_menu_text(), reply_markup=build_help_menu_keyboard()
    )

async def feedback_command(update: Update, context):
    from src.rendering.html_views import build_feedback_menu_text
    buttons = [[InlineKeyboardButton(label, callback_data=f"fb_cat|{key}")] for key, label in FEEDBACK_CATEGORIES.items()]
    buttons.append([InlineKeyboardButton("📋 MY FEEDBACK & REQUESTS", callback_data="my_feedback|0")])
    buttons.append([
        InlineKeyboardButton("👤 GO TO PROFILE", callback_data="privacy_menu|0"),
        InlineKeyboardButton("❌ CANCEL", callback_data="close_portal|0")
    ])

    asyncio.create_task(_delete_silent(context.bot, update.message.chat_id, update.message.message_id))
    await send_rich_message_safe(
        context.bot, chat_id=update.message.chat_id,
        html_content=build_feedback_menu_text(),
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def myfeedback_command(update: Update, context):
    user = update.effective_user
    user_id = user.id
    await asyncio.to_thread(db_update_user_telegram_info, user_id, user.username, user.first_name)

    items = await asyncio.to_thread(db_get_user_feedback_list, user_id, 5, 0)
    total = await asyncio.to_thread(db_count_user_feedback, user_id)
    text = build_user_feedback_list_text(items, total)

    item_rows = [
        [InlineKeyboardButton(f"#{fb['id']} · {fb['message'][:24]}", callback_data=f"fb_view|{fb['id']}|0")]
        for fb in items
    ]
    if total > 5:
        item_rows.append([InlineKeyboardButton("NEXT ➡️", callback_data="my_feedback|5")])
    item_rows.append([InlineKeyboardButton("🔙 BACK TO FEEDBACK MENU", callback_data="fb_menu|0")])
    item_rows.append([InlineKeyboardButton("👤 BACK TO PROFILE", callback_data="privacy_menu|0")])

    asyncio.create_task(_delete_silent(context.bot, update.message.chat_id, update.message.message_id))
    await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content=text, reply_markup=InlineKeyboardMarkup(item_rows))

def build_user_directory_text(users: list) -> str:
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
        [InlineKeyboardButton("💬 Reply to User", callback_data=f"fb_reply|{fb_id}")]
    ])
    for admin_id in admin_ids:
        try:
            await send_rich_message_safe(context.bot, chat_id=admin_id, html_content=f"🆕 <b>NEW FEEDBACK</b>\n\n{text}", reply_markup=kb)
        except Exception:
            pass

async def feedback_admin_command(update: Update, context):
    user_id = update.effective_user.id
    from src.database import db_is_admin
    if not await asyncio.to_thread(db_is_admin, user_id):
        return
    stats = await asyncio.to_thread(db_get_feedback_stats)
    from src.config import FEEDBACK_CATEGORIES
    buttons = [[InlineKeyboardButton(label, callback_data=f"fb_browse|{key}|open")] for key, label in FEEDBACK_CATEGORIES.items()]
    buttons.append([InlineKeyboardButton("📋 ALL OPEN ITEMS", callback_data="fb_browse|all|open")])
    await send_rich_message_safe(
        context.bot, chat_id=update.message.chat_id,
        html_content=build_feedback_stats_text(stats),
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def admin_dashboard_command(update: Update, context):
    user_id = update.effective_user.id
    from src.database import db_is_admin, db_get_admin_dashboard_stats
    if not await asyncio.to_thread(db_is_admin, user_id):
        return
    from src.rendering.html_views import build_admin_dashboard_text
    stats = await asyncio.to_thread(db_get_admin_dashboard_stats)
    text = build_admin_dashboard_text(stats)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("👥 VIEW USER DIRECTORY", callback_data="admin_users|0"),
        InlineKeyboardButton("💬 VIEW FEEDBACK", callback_data="fb_browse|all|open:0")
    ]])
    await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content=text, reply_markup=kb)

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

async def handle_fsm_message(update: Update, context):
    """Global message filter intercepting user response messages for active state configurations."""
    user = update.effective_user
    user_id = user.id
    state = USER_STATES.get(user_id)

    if not state or state == "IDLE":
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
        prior_state = state
        USER_STATES[user_id] = "IDLE"
        USER_PAYLOADS.pop(user_id, None)

        if prior_state in ("AWAITING_FEEDBACK_TEXT", "AWAITING_ADMIN_REPLY", "AWAITING_USER_FEEDBACK_REPLY"):
            cancel_return_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 BACK TO FEEDBACK MENU", callback_data="fb_menu|0")],
                [InlineKeyboardButton("👤 MY PROFILE", callback_data="privacy_menu|0")]
            ])
            await _fsm_advance(context, update.message.chat_id, edit_mid, "❌ <b>Action cancelled.</b>", cancel_return_kb)
        else:
            await _fsm_advance(context, update.message.chat_id, edit_mid, "❌ <b>Action cancelled.</b> Session discarded.", profile_nav_kb)
        return

    try:
        if state == "AWAITING_NICKNAME":
            clean_name = re.sub(r'[^\w\s\-@]', '', text_input)[:20].strip()
            if not clean_name:
                await _fsm_advance(context, update.message.chat_id, edit_mid, "⚠️ Invalid format. Only letters, spaces and underscores are allowed.\n\n<i>Try again, or /cancel.</i>", cancel_kb)
                return

            await asyncio.to_thread(db_set_user_nickname, user_id, clean_name)
            USER_STATES[user_id] = "IDLE"
            USER_PAYLOADS.pop(user_id, None)
            await _fsm_advance(context, update.message.chat_id, edit_mid, f"✅ Nickname registered successfully: <b>{clean_name}</b>!", profile_nav_kb)

        elif state == "AWAITING_ORG_NAME":
            clean_org_name = re.sub(r'[^\w\s\-]', '', text_input)[:50].strip()
            if not clean_org_name:
                await _fsm_advance(context, update.message.chat_id, edit_mid, "⚠️ Invalid formal name.\n\n<i>Try again, or /cancel.</i>", cancel_kb)
                return

            similar = await asyncio.to_thread(db_find_similar_organizations, clean_org_name)
            if similar:
                USER_PAYLOADS[user_id] = {"org_name": clean_org_name, "edit_mid": edit_mid}
                USER_STATES[user_id] = "IDLE"

                lines = ["🔎 <b>Found similar existing team(s):</b>\n"]
                buttons = []
                for org in similar:
                    loc = f" — {org.get('city','')}, {org.get('country','')}" if org.get('city') else ""
                    lines.append(f"🏫 <b>{org['org_name']}</b> (#{org['org_tag']}){loc}")
                    buttons.append([InlineKeyboardButton(f"🔑 Join #{org['org_tag']} instead", callback_data=f"quickjoin_org|{org['org_id']}")])
                buttons.append([InlineKeyboardButton("✨ No, create new anyway", callback_data="force_create_org|0")])
                buttons.append([InlineKeyboardButton("❌ CANCEL", callback_data="privacy_menu|0")])
                lines.append("\n<i>Joining an existing team keeps everyone's scores together instead of splitting them across duplicates.</i>")

                await _fsm_advance(context, update.message.chat_id, edit_mid, "\n".join(lines), InlineKeyboardMarkup(buttons))
                return

            USER_PAYLOADS[user_id] = {"org_name": clean_org_name, "edit_mid": edit_mid}
            USER_STATES[user_id] = "AWAITING_ORG_TAG"
            await _fsm_advance(
                context, update.message.chat_id, edit_mid,
                f"🏫 Name Accepted: <b>{clean_org_name}</b>\n\n"
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
            USER_STATES[user_id] = "AWAITING_ORG_CITY"
            await _fsm_advance(
                context, update.message.chat_id, edit_mid,
                f"🔑 Short Domain Code accepted: <code>#{clean_tag}</code>\n\n"
                "✍ <b>PROMPT: Team City Location</b>\n"
                "Please enter the city where your school or academy is located:\n"
                "<i>(Example: Addis Ababa)</i>",
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
                f"🌆 City Accepted: <b>{clean_city}</b>\n\n"
                "✍ <b>PROMPT: Team Country Location</b>\n"
                "Please enter the country where your school or academy is located:\n"
                "<i>(Example: Ethiopia)</i>",
                cancel_kb
            )

        elif state == "AWAITING_ORG_COUNTRY":
            clean_country = re.sub(r'[^\w\s\-]', '', text_input)[:50].strip()
            if not clean_country:
                await _fsm_advance(context, update.message.chat_id, edit_mid, "⚠️ Invalid country name.\n\n<i>Try again, or /cancel.</i>", cancel_kb)
                return

            org_name = USER_PAYLOADS[user_id]["org_name"]
            org_tag = USER_PAYLOADS[user_id]["org_tag"]
            org_city = USER_PAYLOADS[user_id]["org_city"]

            try:
                await asyncio.to_thread(db_create_organization, org_name, org_tag, user_id, "School", True, org_city, clean_country)
                USER_STATES[user_id] = "IDLE"
                USER_PAYLOADS.pop(user_id, None)
                await _fsm_advance(
                    context, update.message.chat_id, edit_mid,
                    f"✅ <b>Alliance Registered Successfully!</b>\n\n"
                    f"🏫 Institution: <b>{org_name}</b>\n"
                    f"🔑 Short Domain Tag: <code>#{org_tag}</code>\n"
                    f"📍 Location: <b>{org_city}, {clean_country}</b>\n\n"
                    f"Share your team's invite link (from the team page) so students can join directly.",
                    profile_nav_kb
                )
            except Exception as e:
                if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                    await _fsm_advance(context, update.message.chat_id, edit_mid, f"⚠️ Error: <code>#{org_tag}</code> is already taken. Enter a unique tag:", cancel_kb)
                else:
                    await _fsm_advance(context, update.message.chat_id, edit_mid, "⚠️ Setup failed due to a database exception.\n\n<i>Try again, or /cancel.</i>", cancel_kb)

        elif state == "AWAITING_ORG_JOIN":
            clean_tag = re.sub(r'\W', '', text_input).upper().strip()
            join_data = await asyncio.to_thread(db_join_organization, user_id, clean_tag)
            if join_data:
                USER_STATES[user_id] = "IDLE"
                USER_PAYLOADS.pop(user_id, None)

                if join_data["role_assigned"] == "pending":
                    response_text = (
                        f"📥 <b>ADMISSION REQ SENT!</b>\n\n"
                        f"You requested to join <b>{join_data['org_name']}</b> (<code>#{clean_tag}</code>). "
                        f"The Creator/Admin has been notified."
                    )
                    try:
                        approve_kb = InlineKeyboardMarkup([
                            [InlineKeyboardButton("🟢 APPROVE MEMBER", callback_data=f"process_req|{join_data['org_id']}|{user_id}|1"),
                             InlineKeyboardButton("🔴 REJECT MEMBER", callback_data=f"process_req|{join_data['org_id']}|{user_id}|0")]
                        ])
                        await context.bot.send_message(
                            chat_id=int(join_data["creator_id"]),
                            text=(
                                f"📥 <b>NEW ADMISSION REQUEST</b>\n"
                                f"Student <b>{html.escape(user.first_name)}</b> is requesting to join <b>{join_data['org_name']}</b>."
                            ),
                            reply_markup=approve_kb, parse_mode="HTML"
                        )
                    except Exception:
                        pass
                else:
                    response_text = f"✅ <b>Integrated Successfully!</b> You're now registered under <b>{join_data['org_name']}</b> (<code>#{clean_tag}</code>)."

                await _fsm_advance(context, update.message.chat_id, edit_mid, response_text, profile_nav_kb)
            else:
                await _fsm_advance(context, update.message.chat_id, edit_mid, f"⚠️ Alliance code <code>#{clean_tag}</code> not found. Please enter a valid Tag:", cancel_kb)

        elif state == "AWAITING_LOCATION_CITY":
            clean_city = re.sub(r'[^\w\s\-]', '', text_input)[:50].strip()
            if not clean_city:
                await _fsm_advance(context, update.message.chat_id, edit_mid, "⚠️ Invalid city name.\n\n<i>Try again, or /cancel.</i>", cancel_kb)
                return

            USER_PAYLOADS[user_id]["loc_city"] = clean_city
            USER_STATES[user_id] = "AWAITING_LOCATION_COUNTRY"
            await _fsm_advance(
                context, update.message.chat_id, edit_mid,
                f"🌆 City Accepted: <b>{clean_city}</b>\n\n"
                "✍ <b>PROMPT: YOUR COUNTRY</b>\nPlease type the country you're studying in:\n<i>(Example: Ethiopia)</i>",
                cancel_kb
            )

        elif state == "AWAITING_LOCATION_COUNTRY":
            clean_country = re.sub(r'[^\w\s\-]', '', text_input)[:50].strip()
            if not clean_country:
                await _fsm_advance(context, update.message.chat_id, edit_mid, "⚠️ Invalid country name.\n\n<i>Try again, or /cancel.</i>", cancel_kb)
                return

            clean_city = USER_PAYLOADS[user_id].get("loc_city", "")
            await asyncio.to_thread(db_update_user_location, user_id, clean_city, clean_country)
            USER_STATES[user_id] = "IDLE"
            USER_PAYLOADS.pop(user_id, None)
            await _fsm_advance(
                context, update.message.chat_id, edit_mid,
                f"✅ <b>Location updated!</b>\n📍 {clean_city}, {clean_country}",
                profile_nav_kb
            )

        elif state == "AWAITING_FEEDBACK_TEXT":
            category = session.get("category", "general")
            clean_msg = text_input[:1000].strip()
            if not clean_msg:
                await _fsm_advance(context, update.message.chat_id, edit_mid, "⚠️ Message can't be empty.\n\n<i>Try again, or /cancel.</i>", cancel_kb)
                return

            fid = await asyncio.to_thread(db_submit_feedback, user_id, category, clean_msg)
            USER_STATES[user_id] = "IDLE"
            USER_PAYLOADS.pop(user_id, None)

            await _fsm_advance(
                context, update.message.chat_id, edit_mid,
                f"✅ <b>Thanks! Feedback #{fid} submitted.</b>\n\n"
                f"You'll get a message here if there's a reply or update on it.",
                profile_nav_kb
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
            kb = _build_feedback_detail_keyboard(target_fb_id, return_state)
            await _fsm_advance(context, update.message.chat_id, edit_mid, build_feedback_thread_text(fb, thread), kb)
        elif state == "AWAITING_USER_FEEDBACK_REPLY":
            fb_id = session.get("fb_id")
            return_offset = session.get("return_offset", "0")
            reply_text = text_input[:1000].strip()
            if not reply_text:
                return

            await asyncio.to_thread(db_add_feedback_message, fb_id, "user", user_id, reply_text)
            USER_STATES[user_id] = "IDLE"
            USER_PAYLOADS.pop(user_id, None)

            from src.rendering.html_views import build_feedback_thread_text
            fb = await asyncio.to_thread(db_get_feedback_by_id, fb_id)
            thread = await asyncio.to_thread(db_get_feedback_thread, fb_id)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("💬 REPLY", callback_data=f"fb_user_reply|{fb_id}|{return_offset}")],
                [InlineKeyboardButton("🔙 BACK TO LIST", callback_data=f"my_feedback|{return_offset}")]
            ])
            await _fsm_advance(context, update.message.chat_id, edit_mid, build_feedback_thread_text(fb, thread), kb)

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
    except Exception:
        traceback.print_exc()
        await _fsm_advance(context, update.message.chat_id, edit_mid, "⚠️ Connection Error: Failed to commit your input.", profile_nav_kb)

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
    
async def unknown_command_handler(update: Update, context):
    """Catches any /command not matched by a registered handler above it."""
    chat_id = update.message.chat_id
    cmd_mid = update.message.message_id

    asyncio.create_task(_delete_silent(context.bot, chat_id, cmd_mid))

    notice = await context.bot.send_message(
        chat_id=chat_id,
        text="❓ <b>Unknown command.</b> Type /help to see everything I understand.",
        parse_mode="HTML"
    )
    asyncio.create_task(_delayed_delete(context.bot, chat_id, notice.message_id, delay_seconds=8))


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
    app.add_handler(CallbackQueryHandler(lambda u, c: handle_callback(update=u, context=c, engine=engine)))

    # Priority FSM handler filtering text messages during active state dialog sessions
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_fsm_message), group=-1)

    # Catch-all: any /command not matched above. Must be added LAST among command handlers.
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


