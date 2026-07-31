# src/tournament.py
"""
Resilient tournament manager handling zero-lag transitions, startup recovery, 
and blocking emergency sweeps for graceful system shutdowns.
"""
import asyncio
import traceback
import httpx
from datetime import datetime, timedelta, timezone

from src.config import CONFIG, Style
from src.database import (
    QuizEngine,
    db_get_overdue_tournament_rounds,
    db_get_active_tournament_rounds,
    db_get_tournament_queue,
    db_pop_tournament_question,
    db_clear_tournament_queue,
    db_get_question_by_id,
    db_get_responses_for_message,
    db_get_cached_file_id,
    db_save_cached_file_id,
    process_user_score,
)
from src.rendering import UIFactory, fetch_kroki_image
from src.rendering.rich_helpers import send_rich_message_safe, edit_rich_message_safe

_ACTIVE_COUNTDOWNS = set()
_FINALIZING_ROUNDS = set()
_LAUNCHING_ROUND = False


async def run_round_countdown(app, engine: QuizEngine, ann_mid: int, display_id: int, deadline: datetime):
    """Fires non-blocking background edits to show ticking time-remaining to students."""
    import src.config
    if ann_mid in _ACTIVE_COUNTDOWNS or src.config.SHUTTING_DOWN:
        return
    _ACTIVE_COUNTDOWNS.add(ann_mid)
    try:
        channel_id = engine.config['channel']
        while True:
            if src.config.SHUTTING_DOWN:
                break
            now = datetime.now(timezone.utc)
            remaining = int((deadline - now).total_seconds())
            if remaining <= 0:
                break

            # Round down to the nearest 10-second marker for cleaner visual updates
            display_seconds = max(0, (remaining // 10) * 10)
            if display_seconds <= 0:
                break

            updated_text = (
                f"⚔️ <b>LIVE TOURNAMENT CHALLENGE • REF {display_id}</b>\n"
                f"⏳ <b>{display_seconds} SECONDS REMAINING</b>\n\n"
                f"<i>The lobby is open! Submit your answer before the timer expires! Speed wins bonus marks!</i>"
            )
            try:
                await app.bot.edit_message_text(
                    chat_id=channel_id,
                    message_id=ann_mid,
                    text=updated_text,
                    parse_mode="HTML"
                )
            except Exception:
                pass  # Ignore network drops or manually deleted announcements

            # Align dynamic sleep with countdown markers
            sleep_time = min(10, remaining % 10 or 10)
            await asyncio.sleep(sleep_time)
    finally:
        _ACTIVE_COUNTDOWNS.discard(ann_mid)


async def push_dm_update(bot, u_id, p_mid, sel_opt, is_correct, message_id, q, last_seq):
    """Asynchronously evaluates student stats and edits their private DM placeholder message."""
    explanation_html, kb, media_bytes, cached_file_id = None, None, None, None
    try:
        perf_card = await asyncio.to_thread(
            process_user_score, u_id, message_id, q['id'], is_correct, sel_opt
        )
        explanation_html = UIFactory.build_answered_view(
            q, str(last_seq), sel_opt, show_derivation=True, show_perf=False, perf_card=perf_card
        )
        kb = UIFactory.build_answered_keyboard(
            last_seq, sel_opt, show_derivation=True, show_perf=False, is_photo=False, message_id=message_id
        )

        if UIFactory.has_real_diagram(q):
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

        m = await edit_rich_message_safe(
            bot, chat_id=u_id, message_id=p_mid, html_content=explanation_html,
            reply_markup=kb, media_bytes=media_bytes, file_id=cached_file_id
        )
        if media_bytes and m and m.photo and not cached_file_id:
            await asyncio.to_thread(db_save_cached_file_id, cache_key, m.photo[-1].file_id)
    except Exception:
        traceback.print_exc()
        if explanation_html:
            try:
                await send_rich_message_safe(
                    bot, chat_id=u_id, html_content=explanation_html,
                    reply_markup=kb, media_bytes=media_bytes, file_id=cached_file_id
                )
            except Exception:
                pass


async def launch_tournament_round(app, engine: QuizEngine, q: dict, last_seq: int, round_seconds: int = 60):
    """Sends the live tournament challenge to the main channel and registers its timeline metadata."""
    global _LAUNCHING_ROUND
    _LAUNCHING_ROUND = True
    try:
        announcement_text = (
            f"⚔️ <b>LIVE TOURNAMENT CHALLENGE • REF {last_seq}</b>\n"
            f"⏳ <b>{round_seconds} SECONDS REMAINING</b>\n\n"
            f"<i>The lobby is open! Submit your answer before the timer expires! Speed wins bonus marks!</i>"
        )
        ann_msg = await app.bot.send_message(chat_id=engine.config['channel'], text=announcement_text, parse_mode="HTML")

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

        deadline = datetime.now(timezone.utc) + timedelta(seconds=round_seconds)
        msg_type = "photo" if img_url else "text"

        await asyncio.to_thread(
            engine.db_save_track, m.message_id, q['id'], "tournament_active", last_seq,
            "premium", msg_type, ann_msg.message_id, deadline
        )
        print(f"{Style.GREEN}[TOURNAMENT] Round launched. REF: {last_seq} | Deadline: {deadline.isoformat()}{Style.RESET}", flush=True)

        # Spawn background updates
        asyncio.create_task(run_round_countdown(app, engine, ann_msg.message_id, last_seq, deadline))
    finally:
        _LAUNCHING_ROUND = False


async def finalize_tournament_round(app, engine: QuizEngine, track: dict):
    """Concludes the round on the channel and resolves pending student DMs concurrently."""
    mid = track['message_id']
    if mid in _FINALIZING_ROUNDS:
        return
    _FINALIZING_ROUNDS.add(mid)

    try:
        last_seq = track['display_id']
        ann_mid = track.get('followup_mid')
        is_photo = (track.get('msg_type') == 'photo')

        q = await asyncio.to_thread(db_get_question_by_id, track['q_id'])
        if not q:
            print(f"{Style.RED}[TOURNAMENT] Question {track['q_id']} missing for REF {last_seq}. Marking track deleted.{Style.RESET}")
            await asyncio.to_thread(engine.db_update_track_status, mid, "deleted")
            return

        print(f"{Style.YELLOW}[TOURNAMENT] Closing overdue round REF: {last_seq}...{Style.RESET}", flush=True)

        if ann_mid:
            try:
                await app.bot.edit_message_text(
                    chat_id=engine.config['channel'], message_id=int(ann_mid),
                    text=f"⚔️ <b>LIVE TOURNAMENT CHALLENGE • REF {last_seq}</b>\n🏁 <b>ROUND FINISHED!</b>", parse_mode="HTML"
                )
            except Exception:
                pass

        user_responses = await asyncio.to_thread(db_get_responses_for_message, mid)

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
                # Commit status to database only AFTER the channel assets are completely delivered and swapped [1]
                await asyncio.to_thread(engine.db_update_track_status, new_msg.message_id, "closed", clear_followup=True)
            except Exception as e:
                print(f"{Style.RED}[TOURNAMENT] Error publishing solution for REF {last_seq}: {e}{Style.RESET}")
                await asyncio.to_thread(engine.db_update_track_status, mid, "closed", clear_followup=True)
        else:
            try:
                closed_view = UIFactory.build_closed_static_view(q, last_seq, compact=False)
                await edit_rich_message_safe(
                    app.bot, chat_id=engine.config['channel'], message_id=int(mid),
                    html_content=closed_view, reply_markup=None
                )
                # Commit status only AFTER the message text edit has completed successfully [1]
                await asyncio.to_thread(engine.db_update_track_status, mid, "closed", clear_followup=True)
            except Exception as e:
                print(f"{Style.RED}[TOURNAMENT] Error publishing flat solution for REF {last_seq}: {e}{Style.RESET}")
                await asyncio.to_thread(engine.db_update_track_status, mid, "closed", clear_followup=True)

        # Gather and await explanation deliveries to students concurrently
        dm_tasks = []
        for resp in user_responses:
            u_id, p_mid, sel_opt = resp['user_id'], resp['private_message_id'], resp['selected_option']
            if p_mid:
                dm_tasks.append(
                    push_dm_update(app.bot, u_id, p_mid, sel_opt, resp['is_correct'], final_msg_id, q, last_seq)
                )
        if dm_tasks:
            await asyncio.gather(*dm_tasks, return_exceptions=True)

        print(f"{Style.GREEN}[TOURNAMENT] Round REF: {last_seq} closed. {len(user_responses)} DMs processed.{Style.RESET}", flush=True)
    finally:
        _FINALIZING_ROUNDS.discard(mid)


async def emergency_shutdown_cleanup(app, engine: QuizEngine):
    """Emergency finalization hook triggered immediately on SIGTERM/System Shutdown."""
    import src.config
    src.config.SHUTTING_DOWN = True
    print(f"\n{Style.YELLOW}[SHUTDOWN] Signal trapped. Executing emergency round sweep...{Style.RESET}", flush=True)
    try:
        # Clear the queue on shutdown so the tournament does not resume on reboot [1]
        await asyncio.to_thread(db_clear_tournament_queue)
        print(f"{Style.YELLOW}[SHUTDOWN] Queued tournament queue cleared.{Style.RESET}", flush=True)

        active_rounds = await asyncio.to_thread(db_get_active_tournament_rounds)
        if active_rounds:
            print(f"[SHUTDOWN] Resolving {len(active_rounds)} active rounds.", flush=True)
            for track in active_rounds:
                print(f"[SHUTDOWN] Forcing finalization on REF: {track['display_id']}", flush=True)
                await finalize_tournament_round(app, engine, track)
            print(f"{Style.GREEN}[SHUTDOWN] Emergency finalization complete.{Style.RESET}", flush=True)
        else:
            print("[SHUTDOWN] No active tournament rounds were pending cleanup.", flush=True)
    except Exception as e:
        print(f"{Style.RED}[SHUTDOWN ERROR] Sweep execution failed: {e}{Style.RESET}", flush=True)


async def tournament_watcher_loop(app, engine: QuizEngine, poll_seconds: int = 2):
    """Monitors active rounds, resolves overdue tracks, and transitions rounds sequentially with 0-second delay."""
    # --- STARTUP RECOVERY SWEEP ---
    # Treats lingering active rows as interrupted by a shutdown, resolving them instantly on boot.
    print(f"{Style.YELLOW}[TOURNAMENT] Executing startup recovery sweep...{Style.RESET}", flush=True)
    try:
        active_rounds = await asyncio.to_thread(db_get_active_tournament_rounds)
        for track in active_rounds:
            print(f"{Style.YELLOW}[TOURNAMENT] Resolving crashed round REF: {track['display_id']}...{Style.RESET}", flush=True)
            await finalize_tournament_round(app, engine, track)
        
        # Clear lingering tournament queue on startup as a fail-safe against hard crashes [1]
        await asyncio.to_thread(db_clear_tournament_queue)
        print(f"{Style.GREEN}[TOURNAMENT] Recovery sweep complete. Queue cleared.{Style.RESET}", flush=True)
    except Exception as e:
        print(f"{Style.RED}[TOURNAMENT RECOVERY ERROR] {e}{Style.RESET}", flush=True)

    # --- NORMAL RUNTIME ---
    while True:
        import src.config
        if src.config.SHUTTING_DOWN:
            print("[TOURNAMENT] Watcher loop detected shutdown. Exiting cleanly.", flush=True)
            break

        try:
            overdue = await asyncio.to_thread(db_get_overdue_tournament_rounds)
            did_finalize = False
            for track in overdue:
                await finalize_tournament_round(app, engine, track)
                did_finalize = True

            # Invalidate the tracks cache instantly on transitions
            if did_finalize:
                engine._tracks_cache_time = 0

            active_tracks = await asyncio.to_thread(db_get_active_tournament_rounds)
            for active_track in active_tracks:
                ann_mid = active_track.get('followup_mid')
                if ann_mid and int(ann_mid) not in _ACTIVE_COUNTDOWNS:
                    deadline = active_track['round_deadline']
                    if isinstance(deadline, str):
                        deadline = datetime.fromisoformat(deadline)
                    asyncio.create_task(
                        run_round_countdown(
                            app, engine, int(ann_mid), active_track['display_id'], deadline
                        )
                    )

            queue = await asyncio.to_thread(db_get_tournament_queue)
            if queue and queue.get('remaining_ids'):
                # Bypass cache entirely to ensure instantaneous transitions
                engine._tracks_cache_time = 0
                tracks = await asyncio.to_thread(engine.db_get_all_tracks)
                
                # --- Strict Sequential Enforcement ---
                # A new queued tournament round will only launch if there are ZERO active
                # tournament questions remaining on the channel and no questions are currently in-flight [1].
                has_live_round = any(t.get('status') == 'tournament_active' for t in tracks.values())
                
                if not has_live_round and not _LAUNCHING_ROUND:
                    next_qid, next_seq = await asyncio.to_thread(db_pop_tournament_question)
                    if next_qid:
                        q = await asyncio.to_thread(db_get_question_by_id, next_qid)
                        if q:
                            await launch_tournament_round(app, engine, q, next_seq, queue.get('round_seconds', 60))
                            # Skip standard sleep interval to transition to the next question with 0-second delay
                            continue
                        else:
                            print(f"{Style.RED}[TOURNAMENT] Question {next_qid} not found. Skipping.{Style.RESET}")
                    else:
                        await asyncio.to_thread(db_clear_tournament_queue)
                        print(f"{Style.GREEN}[TOURNAMENT] Queue completed.{Style.RESET}", flush=True)
        except Exception as e:
            traceback.print_exc()
            print(f"{Style.RED}[TOURNAMENT WATCHER ERROR] {e}{Style.RESET}", flush=True)

        await asyncio.sleep(poll_seconds)