# src/tournament.py
import os
import asyncio
import traceback
import sys
import httpx
from datetime import datetime, timedelta, timezone
import time

from src.config import CONFIG, Style
from src.database import (
    QuizEngine,
    GLOBAL_ENGINE,
    db_get_overdue_tournament_rounds,
    db_get_active_tournament_rounds,
    db_get_tournament_queue,
    db_pop_tournament_question,
    db_clear_tournament_queue,
    db_get_question_by_id,
    db_get_responses_for_message,
    db_get_cached_file_id,
    db_save_cached_file_id,
    db_get_weekly_leaderboard,
    process_user_score,
    db_try_start_tournament_round,
    db_set_tournament_pause_state,
)
from src.rendering import UIFactory, fetch_kroki_image
from src.rendering.rich_helpers import send_rich_message_safe, edit_rich_message_safe
from src.rendering.html_views import format_public_name

_ACTIVE_COUNTDOWNS = {}
_FINALIZING_ROUNDS = set()
_LAST_COUNTDOWN_TEXT = {}
_LAUNCH_LOCK = asyncio.Lock()

# Central tracking for live interval and delay countdown messaging
_COOLDOWN_MID = None
_LAST_COOLDOWN_VAL = -1
_LAST_DELAY_VAL = -1

# [FIX INITIALIZATION]: Initialize the global closed tracking value referencing the shared database clock.
_LAST_ROUND_CLOSED_AT = 0.0

def run_graceful_shutdown_sync():
    """Synchronous wrapper to execute emergency_shutdown_cleanup from any thread/context."""
    import src.config
    if src.config.SHUTTING_DOWN:
        return
    src.config.SHUTTING_DOWN = True

    loop = src.config.ACTIVE_LOOP
    app = src.config.ACTIVE_APP
    engine = src.config.ACTIVE_ENGINE

    if not (loop and app and engine):
        print(f"[SHUTDOWN] No active references found to run clean shutdown.", flush=True)
        return

    print(f"[SHUTDOWN] Executing graceful shutdown sweep...", flush=True)
    if loop.is_running():
        try:
            future = asyncio.run_coroutine_threadsafe(
                emergency_shutdown_cleanup(app, engine), loop
            )
            future.result(timeout=15.0)
        except Exception as e:
            print(f"[SHUTDOWN ERROR] Thread-safe emergency sweep failed: {e}", flush=True)
    else:
        try:
            loop.run_until_complete(emergency_shutdown_cleanup(app, engine))
        except Exception as e:
            print(f"[SHUTDOWN ERROR] Synchronous emergency sweep failed: {e}", flush=True)

def _render_challenge_text(current_round, total_rounds, ref, remaining_seconds, question_preview, submission_count=None):
    curr_r = current_round if current_round is not None else 1
    tot_r = total_rounds if total_rounds is not None else 1

    remaining_seconds = max(0, remaining_seconds)
    mins, secs = divmod(remaining_seconds, 60)
    time_str = f"{mins}m {secs:02d}s" if mins else f"{secs}s"

    lines = [
        f"⚔️ <b>LIVE TOURNAMENT CHALLENGE</b> • REF {ref} • Round <b>{curr_r}/{tot_r}</b>",
        f"⏳ <b>{time_str} remaining</b>",
    ]
    if submission_count is not None:
        lines.append(f"\n✍️ <b>{submission_count}</b> submission(s) so far. Speed wins bonus marks!")
    else:
        lines.append("\n<i>The lobby is open! Submit your answer before the timer expires!</i>")
    return "\n".join(lines)

async def run_round_countdown(app, engine: QuizEngine, ann_mid: int, display_id: int, round_seconds: int, current_round: int = 1, total_rounds: int = 1):
    """Updates the challenge text on the channel with smooth, rate-limited countdown edits."""
    import src.config
    print(f"{Style.CYAN}[DEBUG-TIMER] Starting countdown task for message {ann_mid}. Display ID: {display_id}. Duration: {round_seconds}s. Round {current_round}/{total_rounds}.{Style.RESET}", flush=True)
    try:
        from src.typography import lite_math
        channel_id = engine.config['channel']

        # Fix: Search by display_id instead of followup_mid to bypass the initial launch sequence race condition!
        active_rounds = await asyncio.to_thread(db_get_active_tournament_rounds)
        own_track = next((r for r in active_rounds if int(r.get('display_id')) == int(display_id)), None)
        if not own_track:
            print(f"{Style.YELLOW}[DEBUG-TIMER-WARNING] No active track found with display_id={display_id}. Aborting countdown loop.{Style.RESET}", flush=True)
            return

        q = await asyncio.to_thread(db_get_question_by_id, own_track.get('q_id'))
        question_preview = ""
        if q:
            raw_q = q.get('native_question') or lite_math(q.get('question', ''))
            question_preview = raw_q[:220] + ("…" if len(raw_q) > 220 else "")

        STOP_EDITING_WITHIN = 0

        while True:
            if src.config.SHUTTING_DOWN:
                print(f"[DEBUG-TIMER] Shutdown detected. Exiting countdown loop for message {ann_mid}.", flush=True)
                return

            active_rounds = await asyncio.to_thread(db_get_active_tournament_rounds)
            # Re-fetch live track by display_id to capture dynamic message_id swaps smoothly
            live_track = next((r for r in active_rounds if int(r.get('display_id')) == int(display_id)), None)
            if not live_track or live_track.get('status') != 'tournament_active':
                print(f"[DEBUG-TIMER] Track status changed from tournament_active or track deleted. Stopping countdown for message {ann_mid}.", flush=True)
                return

            # Read 'remaining' from the database's actual deadline on every tick.
            remaining = live_track.get('remaining_seconds', 0)
            if remaining <= STOP_EDITING_WITHIN:
                print(f"[DEBUG-TIMER-FINISH] Remaining seconds achieved threshold ({remaining}s <= {STOP_EDITING_WITHIN}s). Ending countdown loop.", flush=True)
                break

            question_mid = live_track.get('message_id')
            submission_count = 0
            if question_mid and str(question_mid).isdigit():
                responses = await asyncio.to_thread(db_get_responses_for_message, question_mid, display_id)
                submission_count = len(responses)
                print(f"[DEBUG-TIMER-TICK] Track {display_id} (mid={question_mid}) has {submission_count} submissions. Remaining: {remaining}s", flush=True)

            text = _render_challenge_text(current_round, total_rounds, display_id, remaining, question_preview, submission_count)
            if _LAST_COUNTDOWN_TEXT.get(ann_mid) != text:
                try:
                    await app.bot.edit_message_text(chat_id=channel_id, message_id=ann_mid, text=text, parse_mode="HTML")
                    _LAST_COUNTDOWN_TEXT[ann_mid] = text
                except Exception as edit_err:
                    print(f"[DEBUG-TIMER] Edit failed for message {ann_mid}: {edit_err}", flush=True)

            # Dynamic sleep interval to strictly respect Telegram's message editing rate limits
            if remaining > 30:
                sleep_chunk = 10
            elif remaining > 10:
                sleep_chunk = 5
            else:
                sleep_chunk = 2

            sleep_chunk = min(sleep_chunk, remaining - 1 if remaining > 1 else 1)
            await asyncio.sleep(sleep_chunk)

    except asyncio.CancelledError:
        print(f"[DEBUG-TIMER] Countdown task for message {ann_mid} was cancelled.", flush=True)
        raise
    except Exception as e:
        traceback.print_exc()
        print(f"{Style.RED}[DEBUG-TIMER ERROR] Countdown task crashed: {e}{Style.RESET}", flush=True)
    finally:
        current = asyncio.current_task()
        if _ACTIVE_COUNTDOWNS.get(ann_mid) is current:
            _ACTIVE_COUNTDOWNS.pop(ann_mid, None)
        _LAST_COUNTDOWN_TEXT.pop(ann_mid, None)
        print(f"[DEBUG-TIMER] Countdown task cleaned up for message {ann_mid}.", flush=True)

async def push_dm_update(bot, u_id, p_mid, sel_opt, is_correct, message_id, q, last_seq):
    """Asynchronously evaluates student stats and delivers the resolved DM solution sheet."""
    explanation_html, kb, media_bytes, cached_file_id = None, None, None, None
    try:
        print(f"[DEBUG-DM-UPDATE] Initializing DM update for User ID: {u_id}, Placeholder Message ID: {p_mid}, Question ID: {q['id']}", flush=True)
        perf_card = await asyncio.to_thread(
            process_user_score, u_id, message_id, q['id'], is_correct, sel_opt
        )
        explanation_html = UIFactory.build_answered_view(
            q, str(last_seq), sel_opt, show_derivation=True, show_perf=False, perf_card=perf_card
        )
        kb = UIFactory.build_answered_keyboard(
            last_seq, sel_opt, show_derivation=True, show_perf=False, is_photo=False, message_id=message_id
        )

        has_tikz = UIFactory.has_real_diagram(q)

        if has_tikz:
            cache_key = f"q:{q['id']}:exp:{sel_opt}"
            cached_file_id = await asyncio.to_thread(db_get_cached_file_id, cache_key)
            if not cached_file_id:
                latex_code, _ = UIFactory.create_explanation_assets(q, sel_opt, last_seq)
                if latex_code:
                    img_url = UIFactory.get_latex_url(latex_code)
                    async with httpx.AsyncClient() as client:
                        resp = await fetch_kroki_image(client, img_url, latex_code)
                        if resp and resp.status_code == 200:
                            media_bytes = resp.content

        # Since the placeholder message in the DM was text-only, if the solution has a diagram (photo),
        # delete the text message and send a new photo message to avoid editing type constraints.
        if has_tikz:
            print(f"[DEBUG-DM-DELIVERY] Question {q['id']} contains a visual diagram. Deleting placeholder text message {p_mid} and pushing a fresh photo message.", flush=True)
            try:
                await bot.delete_message(chat_id=u_id, message_id=p_mid)
            except Exception as del_err:
                print(f"[DEBUG-DM-DELIVERY-WARNING] Could not delete placeholder text message {p_mid}: {del_err}", flush=True)

            m = await send_rich_message_safe(
                bot, chat_id=u_id, html_content=explanation_html,
                reply_markup=kb, media_bytes=media_bytes, file_id=cached_file_id
            )
        else:
            print(f"[DEBUG-DM-DELIVERY] Question {q['id']} is text-only. Directly editing placeholder text message {p_mid}.", flush=True)
            m = await edit_rich_message_safe(
                bot, chat_id=u_id, message_id=p_mid, html_content=explanation_html,
                reply_markup=kb, media_bytes=media_bytes, file_id=cached_file_id
            )

        if media_bytes and m and m.photo and not cached_file_id:
            await asyncio.to_thread(db_save_cached_file_id, cache_key, m.photo[-1].file_id)
            print(f"[DEBUG-DM-DELIVERY] Successfully cached newly compiled file_id={m.photo[-1].file_id} for key={cache_key}", flush=True)

    except Exception as e:
        print(f"[DEBUG-DM-DELIVERY-ERROR] push_dm_update failed for user {u_id}: {e}", flush=True)
        traceback.print_exc()
        if explanation_html:
            try:
                print(f"[DEBUG-DM-DELIVERY] Attempting ultimate delivery fallback to User ID {u_id}", flush=True)
                await send_rich_message_safe(
                    bot, chat_id=u_id, html_content=explanation_html,
                    reply_markup=kb, media_bytes=media_bytes, file_id=cached_file_id
                )
            except Exception as fallback_err:
                print(f"[DEBUG-DM-DELIVERY-ERROR] DM fallback delivery also failed: {fallback_err}", flush=True)

async def launch_tournament_round(app, engine: QuizEngine, q: dict, last_seq: int, round_seconds: int = 60, current_round: int = 1, total_rounds: int = 1):
    """Sends the live tournament challenge to the main channel using pre-emptive database locking."""
    placeholder_mid = f"launching_{last_seq}"
    print(f"{Style.CYAN}[DEBUG-LAUNCH] launch_tournament_round called. display_id: {last_seq}, question: {q['id']}, round: {current_round}/{total_rounds}.{Style.RESET}", flush=True)

    try:
        claimed = await asyncio.to_thread(
            db_try_start_tournament_round, placeholder_mid, q['id'], last_seq,
            round_seconds, current_round, total_rounds
        )
        if not claimed:
            print(f"{Style.YELLOW}[DEBUG-LAUNCH] Round already active elsewhere — aborting duplicate launch for REF {last_seq}.{Style.RESET}", flush=True)
            return

        from src.typography import lite_math
        raw_q = q.get('native_question') or lite_math(q.get('question', ''))
        question_preview = raw_q[:220] + ("…" if len(raw_q) > 220 else "")

        announcement_text = _render_challenge_text(current_round, total_rounds, last_seq, round_seconds, question_preview)
        ann_msg = await app.bot.send_message(chat_id=engine.config['channel'], text=announcement_text, parse_mode="HTML")

        await asyncio.sleep(1.5)

        countdown_task = asyncio.create_task(
            run_round_countdown(app, engine, ann_msg.message_id, last_seq, round_seconds, current_round, total_rounds)
        )
        _ACTIVE_COUNTDOWNS[ann_msg.message_id] = countdown_task

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

        m = await send_rich_message_safe(
            app.bot, chat_id=engine.config['channel'], html_content=caption,
            reply_markup=kb, media_bytes=media_bytes, file_id=cached_file_id
        )
        if img_url and not cached_file_id and m.photo:
            await asyncio.to_thread(db_save_cached_file_id, cache_key, m.photo[-1].file_id)

        msg_type = "photo" if img_url else "text"

        await asyncio.to_thread(engine.db_swap_track_message_id, placeholder_mid, m.message_id)
        await asyncio.to_thread(engine.db_update_track_followup_and_type, m.message_id, ann_msg.message_id, msg_type)
        print(f"{Style.GREEN}[DEBUG-LAUNCH] Round launched successfully. REF: {last_seq} message_id: {m.message_id}{Style.RESET}", flush=True)

    except Exception as e:
        try:
            await asyncio.to_thread(engine.db_delete_track, placeholder_mid)
        except Exception:
            pass
        raise e

async def finalize_tournament_round(app, engine: QuizEngine, track: dict, interrupted: bool = False, halt_reason: str = None):
    """Concludes the round on the channel and resolves pending student DMs concurrently with complete diagnostics."""
    global _LAST_ROUND_CLOSED_AT

    # Synchronize database-side clock timestamp immediately to strictly preserve timezone alignment.
    _LAST_ROUND_CLOSED_AT = await asyncio.to_thread(engine.db_get_current_epoch)
    print(f"[DEBUG-FINALIZE-CLOCK] Logged database-side round closure time: {_LAST_ROUND_CLOSED_AT}", flush=True)

    mid = track['message_id']
    if mid in _FINALIZING_ROUNDS or str(mid).startswith("launching_"):
        return
    _FINALIZING_ROUNDS.add(mid)

    try:
        active_rounds = await asyncio.to_thread(db_get_active_tournament_rounds)
        still_live = any(str(r.get('message_id')) == str(mid) and r.get('status') == 'tournament_active' for r in active_rounds)
        if not still_live:
            print(f"[TOURNAMENT] REF for msg {mid} already resolved. Skipping duplicate finalize.", flush=True)
            return

        last_seq = track['display_id']
        ann_mid = track.get('followup_mid')
        is_photo = (track.get('msg_type') == 'photo')

        print(f"[DEBUG-FINALIZE] Step 1: Locating target question q_id={track['q_id']}...", flush=True)
        q = await asyncio.to_thread(db_get_question_by_id, track['q_id'])
        if not q:
            print(f"{Style.RED}[TOURNAMENT] Question {track['q_id']} missing for REF {last_seq}. Marking track deleted.{Style.RESET}")
            await asyncio.to_thread(engine.db_update_track_status, mid, "deleted")
            return

        current_round = track.get('round_number', 1)
        total_rounds = track.get('total_rounds', 1)

        header = f"⚔️ <b>LIVE TOURNAMENT CHALLENGE • Round {current_round}/{total_rounds} • REF {last_seq}</b>"

        import src.config

        print(f"[DEBUG-FINALIZE] Step 2: Cancelling countdown task safely...", flush=True)
        if ann_mid:
            countdown_task = _ACTIVE_COUNTDOWNS.pop(int(ann_mid), None)
            if countdown_task and not countdown_task.done():
                countdown_task.cancel()
                try:
                    await countdown_task
                except (asyncio.CancelledError, Exception) as cancel_err:
                    print(f" └─ [DEBUG-FIX-SUCCESS] Countdown task safely terminated. Trapped error: {type(cancel_err).__name__}", flush=True)

        if src.config.SHUTTING_DOWN or interrupted:
            print(f"{Style.YELLOW}[TOURNAMENT] Marking round REF: {last_seq} as interrupted...{Style.RESET}", flush=True)
            if ann_mid:
                try:
                    reason_msg = halt_reason if halt_reason else "the server went offline or experienced an unexpected reboot sequence"
                    shutdown_text = (
                        f"{header}\n"
                        f"⚠️ <b>ROUND INTERRUPTED / PAUSED</b>\n\n"
                        f"<i>We apologize, scholars! This round was halted because {reason_msg}. "
                        f"Your submitted selections have been securely saved and scored. "
                        f"We will resume shortly!</i> 🎓"
                    )
                    await app.bot.edit_message_text(chat_id=engine.config['channel'], message_id=int(ann_mid), text=shutdown_text, parse_mode="HTML")
                except Exception:
                    pass
        else:
            print(f"[DEBUG-FINALIZE] Step 3: Fetching user responses from database...", flush=True)
            user_responses = await asyncio.to_thread(db_get_responses_for_message, mid, last_seq)
            total_users = len(user_responses)
            correct_responses = [r for r in user_responses if r['is_correct']]
            correct_count = len(correct_responses)
            accuracy_pct = int((correct_count / total_users) * 100) if total_users > 0 else 0

            podium_lines = []
            medals = ["🥇", "🥈", "🥉"]
            for idx, r in enumerate(correct_responses[:3]):
                formatted_identity = format_public_name(r)
                tag_suffix = f" (<b>#{r['alliance_tag']}</b>)" if r.get('alliance_tag') else ""
                podium_lines.append(f"  {medals[idx]} <b>{formatted_identity}</b>{tag_suffix}")
                print(f"[DEBUG-PODIUM-RENDER] Formatted podium winner: {formatted_identity} for user {r['user_id']}", flush=True)

            podium_block = ("\n🏆 <b>ROUND PODIUM (FASTEST CORRECT):</b>\n" + "\n".join(podium_lines)) if podium_lines else \
                "\n🏆 <b>ROUND PODIUM:</b>\n  <i>No correct answers recorded this round.</i>"

            normal_close_text = (
                f"{header}\n"
                f"🏁 <b>ROUND CLOSED!</b>\n\n"
                f"👥 <b>{total_users}</b> submission(s) • ✅ <b>{accuracy_pct}%</b> correct\n"
                f"{podium_block}"
            )

            if ann_mid:
                try:
                    print(f"[DEBUG-FINALIZE] Step 3a: Editing announcement header card...", flush=True)
                    await app.bot.edit_message_text(chat_id=engine.config['channel'], message_id=int(ann_mid), text=normal_close_text, parse_mode="HTML")
                except Exception as ann_err:
                    print(f"[DEBUG-FINALIZE ERROR] Failed to edit announcement card: {ann_err}", flush=True)

        print(f"[DEBUG-FINALIZE] Step 4: Refreshing and closing message assets on Telegram...", flush=True)
        final_msg_id = mid
        if is_photo:
            try:
                try:
                    await app.bot.delete_message(chat_id=engine.config['channel'], message_id=int(mid))
                except Exception:
                    pass

                fig_block = UIFactory.build_figure_block(q, add_strut=False)
                media_bytes, cached_file_id = None, None
                if fig_block:
                    channel_id = CONFIG.get("channel") or "@QuizOva"
                    sol_latex = UIFactory.assemble_diagram_only_layout(channel_id, last_seq, fig_block)
                    sol_img_url = UIFactory.get_latex_url(sol_latex)
                    cache_key = f"q:{q['id']}:closed_diag"
                    cached_file_id = await asyncio.to_thread(db_get_cached_file_id, cache_key)
                    if not cached_file_id:
                        async with httpx.AsyncClient() as client:
                            resp = await fetch_kroki_image(client, sol_img_url, sol_latex)
                            if resp and resp.status_code == 200:
                                media_bytes = resp.content

                closed_view = UIFactory.build_closed_static_view(q, last_seq, compact=False)
                new_msg = await send_rich_message_safe(
                    app.bot, chat_id=engine.config['channel'], html_content=closed_view,
                    reply_markup=None, media_bytes=media_bytes, file_id=cached_file_id
                )
                if media_bytes and new_msg and new_msg.photo and not cached_file_id:
                    await asyncio.to_thread(db_save_cached_file_id, cache_key, new_msg.photo[-1].file_id)

                final_msg_id = new_msg.message_id
                await asyncio.to_thread(engine.db_swap_track_message_id, mid, new_msg.message_id)
                await asyncio.to_thread(engine.db_update_track_status, new_msg.message_id, "closed", clear_followup=True)
            except Exception as e:
                print(f"{Style.RED}[TOURNAMENT] Error publishing solution for REF {last_seq}: {e}{Style.RESET}")
                fallback_id = locals().get('new_msg')
                target_mid = fallback_id.message_id if fallback_id else mid
                final_msg_id = target_mid
                await asyncio.to_thread(engine.db_update_track_status, target_mid, "closed", clear_followup=True)
        else:
            try:
                closed_view = UIFactory.build_closed_static_view(q, last_seq, compact=False)
                await edit_rich_message_safe(
                    app.bot, chat_id=engine.config['channel'], message_id=int(mid),
                    html_content=closed_view, reply_markup=None
                )
                await asyncio.to_thread(engine.db_update_track_status, mid, "closed", clear_followup=True)
            except Exception as e:
                print(f"{Style.RED}[TOURNAMENT] Error publishing flat solution for REF {last_seq}: {e}{Style.RESET}")
                await asyncio.to_thread(engine.db_update_track_status, mid, "closed", clear_followup=True)

        print(f"[DEBUG-FINALIZE] Step 5: Delivering explanation DM sheets to players...", flush=True)
        user_responses = await asyncio.to_thread(db_get_responses_for_message, final_msg_id, last_seq)
        print(f"[DEBUG-FINALIZE-FIX] Querying user responses using final_msg_id={final_msg_id} (post-swap) instead of old mid={mid} to ensure we load all student records successfully. Count found: {len(user_responses)}", flush=True)

        dm_tasks = []
        for resp in user_responses:
            u_id, p_mid, sel_opt = resp['user_id'], resp['private_message_id'], resp['selected_option']
            if p_mid:
                dm_tasks.append(push_dm_update(app.bot, u_id, p_mid, sel_opt, resp['is_correct'], final_msg_id, q, last_seq))
        if dm_tasks:
            await asyncio.gather(*dm_tasks, return_exceptions=True)

        print(f"[DEBUG-FINALIZE] Step 6: Checking and generating final tournament scoreboard...", flush=True)
        try:
            queue = await asyncio.to_thread(db_get_tournament_queue)
            if not src.config.SHUTTING_DOWN and not interrupted and (not queue or not queue.get('remaining_ids')):
                print(f"{Style.GREEN}[TOURNAMENT] Tournament complete. Rendering final report card...{Style.RESET}", flush=True)

                grade_val = q.get('grade')
                try:
                    target_grade = int(grade_val) if grade_val is not None else 12
                except (ValueError, TypeError):
                    target_grade = 12

                try:
                    top_scorers = await asyncio.to_thread(db_get_weekly_leaderboard, target_grade)
                    champions_lines = []
                    medals = ["🥇", "🥈", "🥉"]
                    for idx, row in enumerate(top_scorers[:3]):
                        u_label = format_public_name(row)
                        champions_lines.append(f"  {medals[idx]} <b>{u_label}</b> — <b>{row['total_score']} Marks</b>")
                    champions_block = ("\n🏆 <b>TOURNAMENT SERIES CHAMPIONS:</b>\n" + "\n".join(champions_lines)) if champions_lines else ""

                    final_completed_text = (
                        f"🏁 <b>TOURNAMENT COMPLETED!</b> 🏁\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"All rounds in this showdown series have been resolved!\n"
                        f"{champions_block}\n\n"
                        f"<i>Daily practice builds permanent mastery. See you at the next live challenge!</i> 🎓"
                    )
                    await app.bot.send_message(chat_id=engine.config['channel'], text=final_completed_text, parse_mode="HTML")
                except Exception as score_err:
                    print(f"[TOURNAMENT ERROR] Failed to generate final report card: {score_err}", flush=True)
        except Exception as queue_err:
            print(f"[TOURNAMENT ERROR] Queue wrap-up failed: {queue_err}", flush=True)

        print(f"{Style.GREEN}[TOURNAMENT] Round REF: {last_seq} closed. {len(user_responses)} DMs processed.{Style.RESET}", flush=True)
    finally:
        _FINALIZING_ROUNDS.discard(mid)

async def emergency_shutdown_cleanup(app, engine: QuizEngine):
    """Emergency finalization hook triggered immediately on SIGTERM/System Shutdown."""
    import src.config
    src.config.SHUTTING_DOWN = True
    print(f"\n{Style.YELLOW}[SHUTDOWN] Signal trapped. Executing emergency round sweep...{Style.RESET}", flush=True)
    try:
        active_rounds = await asyncio.to_thread(db_get_active_tournament_rounds)
        queue = await asyncio.to_thread(db_get_tournament_queue)
        has_more_queued = bool(queue and queue.get('remaining_ids'))

        if active_rounds:
            print(f"[SHUTDOWN] Resolving {len(active_rounds)} active round(s).", flush=True)
            for track in active_rounds:
                print(f"[SHUTDOWN] Forcing finalization on REF: {track['display_id']}", flush=True)
                await finalize_tournament_round(app, engine, track, interrupted=True)
            print(f"{Style.GREEN}[SHUTDOWN] Emergency finalization complete.{Style.RESET}", flush=True)
        else:
            print("[SHUTDOWN] No active tournament rounds were pending cleanup.", flush=True)

        if has_more_queued:
            print(f"{Style.GREEN}[SHUTDOWN] {len(queue['remaining_ids'])} queued question(s) preserved — will resume on restart.{Style.RESET}", flush=True)
    except Exception as e:
        print(f"[SHUTDOWN ERROR] Sweep execution failed: {e}{Style.RESET}", flush=True)

async def halt_active_tournament(app, engine: QuizEngine, clear_queue: bool = False, halt_reason: str = None):
    """Gracefully halts the currently executing tournament round and updates its status."""
    print(f"{Style.YELLOW}[TOURNAMENT] Initiating manual halt sequence...{Style.RESET}", flush=True)
    active_rounds = await asyncio.to_thread(db_get_active_tournament_rounds)

    if active_rounds:
        for track in active_rounds:
            print(f"[TOURNAMENT] Interrupting active round: REF {track['display_id']}", flush=True)
            await finalize_tournament_round(app, engine, track, interrupted=True, halt_reason=halt_reason)
    else:
        print("[TOURNAMENT] No active rounds found to interrupt.", flush=True)

    if clear_queue:
        await asyncio.to_thread(db_clear_tournament_queue)
        print(f"{Style.RED}[TOURNAMENT] Remaining queue deleted successfully.{Style.RESET}", flush=True)
    else:
        await asyncio.to_thread(db_set_tournament_pause_state, True)
        print(f"[TOURNAMENT] Tournament queue execution paused.{Style.RESET}", flush=True)

async def tournament_watcher_loop(app, engine: QuizEngine, poll_seconds: int = 2):
    global _COOLDOWN_MID, _LAST_COOLDOWN_VAL, _LAST_DELAY_VAL
    print(f"{Style.YELLOW}[TOURNAMENT] Executing startup recovery sweep...{Style.RESET}", flush=True)
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sent_tracks WHERE status = 'tournament_active' AND message_id LIKE 'launching_%%';")
            conn.commit()
        GLOBAL_ENGINE.release_connection(conn)

        active_rounds = await asyncio.to_thread(db_get_active_tournament_rounds)
        for track in active_rounds:
            print(f"{Style.YELLOW}[TOURNAMENT] Resolving crashed round REF: {track['display_id']} as interrupted...{Style.RESET}", flush=True)
            await finalize_tournament_round(app, engine, track, interrupted=True)

        queue = await asyncio.to_thread(db_get_tournament_queue)
        if queue and queue.get('remaining_ids'):
            print(f"{Style.GREEN}[TOURNAMENT] Scheduled/queued series found ({len(queue['remaining_ids'])} remaining). Resuming tournament queue cleanly.{Style.RESET}", flush=True)
        print(f"{Style.GREEN}[TOURNAMENT] Recovery sweep complete.{Style.RESET}", flush=True)
    except Exception as e:
        print(f"{Style.RED}[TOURNAMENT RECOVERY ERROR] {e}{Style.RESET}", flush=True)

    while True:
        import src.config
        if src.config.SHUTTING_DOWN:
            print(f"[TOURNAMENT] Watcher loop detected shutdown. Exiting cleanly.", flush=True)
            break

        try:
            db_epoch = await asyncio.to_thread(engine.db_get_current_epoch)
            now_utc = datetime.fromtimestamp(db_epoch, timezone.utc)

            # Watchdog heartbeat print
            print(f"[WATCHDOG-TICK] {now_utc.strftime('%H:%M:%S')} UTC | Host: {time.time():.1f} | DB: {db_epoch:.1f}", flush=True)

            overdue = await asyncio.to_thread(db_get_overdue_tournament_rounds)
            did_finalize = False
            for track in overdue:
                await finalize_tournament_round(app, engine, track)
                did_finalize = True

            if did_finalize:
                QuizEngine._tracks_cache_time = 0

            active_tracks = await asyncio.to_thread(db_get_active_tournament_rounds)
            has_live_round = len(active_tracks) > 0

            queue = await asyncio.to_thread(db_get_tournament_queue)

            for active_track in active_tracks:
                ann_mid = active_track.get('followup_mid')
                if ann_mid and int(ann_mid) not in _ACTIVE_COUNTDOWNS:
                    remaining = max(0, active_track.get('remaining_seconds', 0))
                    if remaining > 0:
                        current_round = active_track.get('round_number', 1)
                        total_rounds = active_track.get('total_rounds', 1)

                        resumed_task = asyncio.create_task(
                            run_round_countdown(app, engine, int(ann_mid), active_track['display_id'], remaining, current_round, total_rounds)
                        )
                        _ACTIVE_COUNTDOWNS[int(ann_mid)] = resumed_task

            # Execution block of the queue scheduler
            if queue and queue.get('remaining_ids') and not has_live_round and not did_finalize and not _LAUNCH_LOCK.locked():

                if queue.get('is_paused', False):
                    await asyncio.sleep(poll_seconds)
                    continue

                sched_start = queue.get('scheduled_start')

                if sched_start:
                    if isinstance(sched_start, str):
                        sched_dt = datetime.fromisoformat(sched_start)
                    else:
                        sched_dt = sched_start

                    if sched_dt.tzinfo is None:
                        sched_dt = sched_dt.replace(tzinfo=timezone.utc)

                    sched_dt_utc = sched_dt.astimezone(timezone.utc)

                    remaining_delay = max(0, int((sched_dt_utc - now_utc).total_seconds()))

                    if now_utc < sched_dt_utc:
                        print(f" ├─ [DELAY ACTIVE] Target scheduled: {sched_dt_utc.strftime('%H:%M:%S')} UTC | Remaining: {remaining_delay}s", flush=True)
                        ann_mid = queue.get('announcement_mid')
                        if ann_mid:
                            mins, secs = divmod(remaining_delay, 60)
                            time_str = f"{mins}m {secs:02d}s" if mins else f"{secs}s"
                            text = (
                                f"📢 <b>UPCOMING LIVE TOURNAMENT SHOWDOWN</b> ⚔️\n\n"
                                f"Prepare yourself, scholars! A live tournament series will begin soon.\n\n"
                                f"⏰ <b>Starting in:</b> {time_str}\n"
                                f"📋 <b>Total Questions:</b> {queue.get('total_count', 1)}\n"
                                f"⏱️ <b>Round Duration:</b> {queue.get('round_seconds', 60)} seconds\n"
                                f"❄️ <b>Round Interval:</b> {queue.get('cooldown_seconds', 15)} seconds\n\n"
                                f"<i>Set your notifications ON! Correct and rapid answers earn maximum leaderboard marks.</i>"
                            )

                            update_interval = 10 if remaining_delay > 30 else 2
                            if remaining_delay % update_interval == 0 or _LAST_DELAY_VAL == -1:
                                if _LAST_DELAY_VAL != remaining_delay:
                                    try:
                                        await app.bot.edit_message_text(chat_id=engine.config['channel'], message_id=int(ann_mid), text=text, parse_mode="HTML")
                                        _LAST_DELAY_VAL = remaining_delay
                                    except Exception:
                                        pass

                        await asyncio.sleep(poll_seconds)
                        continue
                    else:
                        print(f"{Style.RED}[DIAGNOSTIC-WATCHER] Scheduled threshold achieved! Releasing question segment...{Style.RESET}\n", flush=True)
                        _LAST_DELAY_VAL = -1
                        conn = GLOBAL_ENGINE.get_db_connection()
                        with conn.cursor() as cur:
                            cur.execute("UPDATE tournament_queue SET scheduled_start = NULL WHERE id = 1;")
                            conn.commit()
                        GLOBAL_ENGINE.release_connection(conn)

                        ann_mid = queue.get('announcement_mid')
                        if ann_mid:
                            try:
                                await app.bot.delete_message(chat_id=engine.config['channel'], message_id=int(ann_mid))
                            except Exception:
                                pass

                cooldown = queue.get('cooldown_seconds', 15)

                time_since_close = db_epoch - _LAST_ROUND_CLOSED_AT

                if _LAST_ROUND_CLOSED_AT > 0.0 and time_since_close < cooldown:
                    remaining_cooldown = max(0, int(cooldown - time_since_close))

                    if remaining_cooldown > 3:
                        if remaining_cooldown % 5 == 0 or _COOLDOWN_MID is None:
                            cooldown_text = f"⏳ <b>PREPARING NEXT ROUND...</b>\n\nNext showdown challenge will launch in <b>{remaining_cooldown} seconds</b>. Get ready!"
                            if _COOLDOWN_MID is None:
                                try:
                                    msg = await app.bot.send_message(chat_id=engine.config['channel'], text=cooldown_text, parse_mode="HTML")
                                    _COOLDOWN_MID = msg.message_id
                                    print(f"[DEBUG-TIMER-COOLDOWN] Created cooldown message ID: {msg.message_id}", flush=True)
                                except Exception:
                                    pass
                            elif _LAST_COOLDOWN_VAL != remaining_cooldown:
                                try:
                                    await app.bot.edit_message_text(chat_id=engine.config['channel'], message_id=_COOLDOWN_MID, text=cooldown_text, parse_mode="HTML")
                                    _LAST_COOLDOWN_VAL = remaining_cooldown
                                except Exception:
                                    pass

                    await asyncio.sleep(poll_seconds)
                    continue

                if _COOLDOWN_MID:
                    try:
                        await app.bot.delete_message(chat_id=engine.config['channel'], message_id=_COOLDOWN_MID)
                    except Exception:
                        pass
                    _COOLDOWN_MID = None
                    _LAST_COOLDOWN_VAL = -1

                async with _LAUNCH_LOCK:
                    fresh_active = await asyncio.to_thread(db_get_active_tournament_rounds)
                    if fresh_active:
                        continue

                    fresh_queue = await asyncio.to_thread(db_get_tournament_queue)
                    if not fresh_queue or not fresh_queue.get('remaining_ids'):
                        await asyncio.to_thread(db_clear_tournament_queue)
                        continue

                    next_qid, next_seq = await asyncio.to_thread(db_pop_tournament_question)
                    if next_qid:
                        q = await asyncio.to_thread(db_get_question_by_id, next_qid)
                        if q:
                            total_rounds = fresh_queue['total_count']
                            remaining_count = len(fresh_queue['remaining_ids']) - 1
                            current_round = total_rounds - remaining_count

                            grace_msg = None
                            try:
                                grace_msg = await app.bot.send_message(
                                    chat_id=engine.config['channel'],
                                    text=f"🚀 <b>Get ready! Round {current_round}/{total_rounds} starts in 3 seconds...</b>",
                                    parse_mode="HTML"
                                )
                            except Exception:
                                pass
                            await asyncio.sleep(3)
                            if grace_msg:
                                try:
                                    await app.bot.delete_message(chat_id=engine.config['channel'], message_id=grace_msg.message_id)
                                except Exception:
                                    pass

                            await launch_tournament_round(app, engine, q, next_seq, fresh_queue.get('round_seconds', 60), current_round, total_rounds)
                        else:
                            print(f"{Style.RED}[TOURNAMENT] Question {next_qid} not found. Skipping.{Style.RESET}")
                    else:
                        # [FIX TOURNAMENT TIMING INTERRUPT]: Do not wipe out the tournament queue on None return signals.
                        # Active rounds returning None temporarily are normal loop states, not end-of-queue triggers.
                        print(f"[DEBUG-TOURNAMENT-FIX] Pop returned None (active live round or lock is blocking). Queue preserved.", flush=True)

        except Exception as e:
            traceback.print_exc()
            print(f"{Style.RED}[TOURNAMENT WATCHER ERROR] {e}{Style.RESET}", flush=True)

        await asyncio.sleep(poll_seconds)