# src/cli.py
import math
import os
import re
import json
import uuid
import asyncio
import traceback
from pathlib import Path
from src.config import CONFIG, Style
from src.database import (
    QuizEngine,
    db_mark_question_as_sent,
    db_get_cached_file_id,
    db_save_cached_file_id,
    db_get_responses_for_message,
    process_user_score,
    db_save_tournament_queue,
    db_get_question_by_id,
    db_get_tournament_queue,
    db_get_upcoming_scheduled_questions,
    db_reschedule_question,
    db_update_tournament_schedule_params,
    db_clear_tournament_queue,
    db_set_tournament_pause_state,
    db_get_active_tournament_rounds,
    db_get_city_leaderboard,
    db_get_country_leaderboard,
    db_get_alliance_leaderboard,
)
from src.rendering import UIFactory, fetch_kroki_image
from src.rendering.rich_helpers import send_rich_message_safe, edit_rich_message_safe, convert_to_legacy_html
from src.typography import lite_math
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import clear as clear_screen
from prompt_toolkit.formatted_text import HTML
import httpx
from telegram import Poll, InputMediaPhoto
from telegram.error import BadRequest
from datetime import datetime, timedelta, timezone

_MISSING_MESSAGE_PHRASES = [
    "message to edit not found",
    "message to delete not found",
    "message can't be edited",
    "message identifier is not specified",
    "message is not modified",
]

def _is_missing_message_error(err: Exception) -> bool:
    msg = str(err).lower()
    return any(phrase in msg for phrase in _MISSING_MESSAGE_PHRASES if phrase != "message is not modified")

def _is_not_modified_error(err: Exception) -> bool:
    return "message is not modified" in str(err).lower()


def parse_duration_to_seconds(text: str, default: int) -> int:
    """Parses delay expressions such as 30s, 5m, or 1h into raw seconds with enhanced input safety."""
    if not text:
        print(f"[DEBUG-PARSER] Empty duration input. Defaulting to: {default}s", flush=True)
        return default

    text = text.strip().lower()
    if text in ['c', 'q', 'cancel', 'exit']:
        print(f"[DEBUG-PARSER] Cancel token detected during duration parse.", flush=True)
        return -99  # Signal an intentional abort

    # Robust numeric extraction to handle spaces, trailing units, or raw numbers
    num_match = re.search(r'([\d.]+)', text)
    if not num_match:
        print(f"[DEBUG-PARSER] No valid numeric value matched in '{text}'. Defaulting to: {default}s", flush=True)
        return default

    val = float(num_match.group(1))
    result = int(val)

    if 'm' in text:
        result = int(val * 60)
    elif 'h' in text:
        result = int(val * 3600)

    print(f"[DEBUG-PARSER] Input text '{text}' successfully parsed to {result} seconds (raw extraction: {val}).", flush=True)
    return result


def _parse_manage_selection(cmd: str, items: list, tracks: dict) -> list:
    targets = []
    invalid_tokens = []

    ref_lookup = {}
    for mid in items:
        v = tracks.get(mid)
        if v:
            ref_lookup[str(v.get('display_id'))] = mid

    for raw_part in cmd.split(','):
        part = raw_part.strip()
        if not part:
            continue

        ref_match = re.match(r'^[r#](\d+)$', part, re.IGNORECASE)
        if ref_match:
            ref_num = ref_match.group(1)
            mid = ref_lookup.get(ref_num)
            if mid:
                targets.append(mid)
            else:
                invalid_tokens.append(f"REF:{ref_num} (not found in current filtered list)")
            continue

        range_match = re.match(r'^(\d+)-(\d+)$', part)
        if range_match:
            start, end = int(range_match.group(1)), int(range_match.group(2))
            if start > end:
                start, end = end, start
            for idx in range(start, end + 1):
                if 1 <= idx <= len(items):
                    targets.append(items[idx - 1])
                else:
                    invalid_tokens.append(str(idx))
            continue

        if part.isdigit():
            idx = int(part)
            if 1 <= idx <= len(items):
                targets.append(items[idx - 1])
            else:
                invalid_tokens.append(part)
            continue

        invalid_tokens.append(part)

    if invalid_tokens:
        print(f"{Style.YELLOW}⚠️  Skipped invalid/unmatched tokens: {', '.join(invalid_tokens)}{Style.RESET}")

    return targets


class CLI:
    def __init__(self):
        self.session = PromptSession()

    async def ask(self, text_html):
        try:
            with patch_stdout():
                result = await self.session.prompt_async(HTML(text_html))
                return result.strip()
        except (EOFError, KeyboardInterrupt):
            return None


async def push_dm_update(bot, u_id, p_mid, sel_opt, is_correct, message_id, q, last_seq):
    """Asynchronously evaluates student stats and delivers the resolved DM solution sheet."""
    explanation_html, kb, media_bytes, cached_file_id = None, None, None, None
    try:
        print(f"[DEBUG-DM-UPDATE-CLI] Initializing DM update for User ID: {u_id}, Placeholder Message ID: {p_mid}, Question ID: {q['id']}", flush=True)
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

        if has_tikz:
            print(f"[DEBUG-DM-DELIVERY-CLI] Question {q['id']} contains a visual diagram. Deleting placeholder text message {p_mid} and pushing a fresh photo message.", flush=True)
            try:
                await bot.delete_message(chat_id=u_id, message_id=p_mid)
            except Exception as del_err:
                print(f"[DEBUG-DM-DELIVERY-CLI-WARNING] Could not delete placeholder text message {p_mid}: {del_err}", flush=True)

            m = await send_rich_message_safe(
                bot, chat_id=u_id, html_content=explanation_html,
                reply_markup=kb, media_bytes=media_bytes, file_id=cached_file_id
            )
        else:
            print(f"[DEBUG-DM-DELIVERY-CLI] Question {q['id']} is text-only. Directly editing placeholder text message {p_mid}.", flush=True)
            m = await edit_rich_message_safe(
                bot, chat_id=u_id, message_id=p_mid, html_content=explanation_html,
                reply_markup=kb, media_bytes=media_bytes, file_id=cached_file_id
            )

        if media_bytes and m and m.photo and not cached_file_id:
            await asyncio.to_thread(db_save_cached_file_id, cache_key, m.photo[-1].file_id)
            print(f"[DEBUG-DM-DELIVERY-CLI] Successfully cached newly compiled file_id={m.photo[-1].file_id} for key={cache_key}", flush=True)

    except Exception as e:
        print(f"[DEBUG-DM-DELIVERY-CLI-ERROR] push_dm_update failed for user {u_id}: {e}", flush=True)
        traceback.print_exc()
        if explanation_html:
            try:
                print(f"[DEBUG-DM-DELIVERY-CLI] Attempting ultimate delivery fallback to User ID {u_id}", flush=True)
                await send_rich_message_safe(
                    bot, chat_id=u_id, html_content=explanation_html,
                    reply_markup=kb, media_bytes=media_bytes, file_id=cached_file_id
                )
            except Exception as fallback_err:
                print(f"[DEBUG-DM-DELIVERY-CLI-ERROR] DM fallback delivery also failed: {fallback_err}", flush=True)


async def admin_panel(app, engine: QuizEngine):
    cli = CLI()
    curr_stat, curr_type, page = "active", "bop", 0
    bot_info = await app.bot.get_me()

    while True:
        print(f"{Style.CYAN}{Style.BOLD}\n--- QUIZ MASTER PRO DASHBOARD ---{Style.RESET}")
        print(f" [1] 📤 Send Native Poll (Simple)")
        print(f" [2] 💎 Send Hybrid UI (Smart Math/Premium)")
        print(f" [3] ⚙️  Manage Sent Quizzes (Sync/Toggle)")
        print(f" [4] 📥 Import AI Questions (From Local JSON File)")
        print(f" [5] ⚔️  Launch Live Synchronous Tournament")
        print(f" [6] 📅 Control Center (Scheduled Quizzes & Tournaments)")
        print(f" [7] 🛑 Emergency Stop / Pause Live Tournament")
        print(f" [8] 🎯 Smart Scheduler (Suggest Next Batch)")
        print(f" [9] ⚙️  Bot Settings (Cleanup Timers)")
        print(f" [0] 🚪 Shutdown System")

        choice = await cli.ask("<ansicyan><b>Choice > </b></ansicyan>")
        if choice in [None, "0"]:
            break
        if choice.lower() == 'c':
            clear_screen()
            continue

        if choice == "4":
            print(f"\n{Style.CYAN}--- DYNAMIC DATABASE QUESTIONS IMPORTER ---{Style.RESET}")

            questions_dir = Path("questions")
            json_files = []
            if questions_dir.exists():
                json_files = sorted(list(questions_dir.rglob("*.json")))

            if not json_files:
                print(f"{Style.RED}No JSON question files found inside questions/ directory.{Style.RESET}")
                continue

            print(f"📁 {Style.YELLOW}Detected Question Files:{Style.RESET}")
            for i, file_path in enumerate(json_files):
                print(f"  {i+1}. {Style.WHITE}{file_path.as_posix()}{Style.RESET}")

            file_select = await cli.ask("<b>Select File # to Import (or Enter path manually): </b>")
            if not file_select:
                continue

            selected_file = None
            if file_select.isdigit() and 1 <= int(file_select) <= len(json_files):
                selected_file = str(json_files[int(file_select)-1])
            else:
                selected_file = file_select

            if not os.path.exists(selected_file):
                print(f"{Style.RED}Error: File path not found.{Style.RESET}")
                continue

            try:
                with open(selected_file, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)

                print(f"{Style.YELLOW}Importing questions to cloud Neon PostgreSQL database...{Style.RESET}")
                count = await asyncio.to_thread(engine.db_import_questions, raw_data)
                if count > 0:
                    print(f"{Style.GREEN}✅ SUCCESS: {count} questions successfully imported/synced to Neon database.{Style.RESET}")
                    await asyncio.to_thread(engine.refresh_database, force=True)
                else:
                    print(f"{Style.RED}❌ FAILED: No questions were imported. Check your JSON schema.{Style.RESET}")
            except Exception as e:
                print(f"{Style.RED}❌ FAILED: JSON syntax error: {e}{Style.RESET}")
            continue

        if choice in ["1", "2"]:
            db = await asyncio.to_thread(engine.refresh_database, force=True)
            subjects = list(db.keys())
            if not subjects:
                print(f"{Style.RED}No questions found.{Style.RESET}")
                continue

            for i, s in enumerate(subjects):
                print(f"  {i+1}. {s.upper()} ({len(db[s])} questions)")

            sub_in = await cli.ask("<b>Select Subject #: </b>")
            if not sub_in or not sub_in.isdigit() or int(sub_in) > len(subjects):
                continue

            target_list = db[subjects[int(sub_in)-1]]
            for i, q in enumerate(target_list):
                m_tag = f"{Style.MAGENTA}[MATH]{Style.RESET} " if (q.get("latex") or UIFactory.is_complex(q['question'])) else ""
                diff = q.get("difficulty", "medium").lower()
                diff_color = f"{Style.GREEN}[EASY]{Style.RESET}" if diff in ["easy", "weak"] else f"{Style.RED}[HARD]{Style.RESET}" if diff == "hard" else f"{Style.YELLOW}[MED]{Style.RESET}"
                print(f"    {i+1}. {diff_color} {m_tag}[{q['id']}] {q['question'][:45]}...")

            range_in = await cli.ask("<b>Selection (e.g. 1, 3-5 or easy:3): </b>")
            if not range_in:
                continue

            to_send = []

            if ":" in range_in:
                query_parts = [p.strip().split(":") for p in range_in.split(",")]
                requested = {part[0].lower().strip(): int(part[1].strip()) for part in query_parts if len(part) == 2 and part[1].strip().isdigit()}
                pools = {"easy": [], "medium": [], "hard": []}
                for q in target_list:
                    d = "easy" if q.get("difficulty", "medium").lower() == "weak" else q.get("difficulty", "medium").lower()
                    if d in pools:
                        pools[d].append(q)
                for diff, count in requested.items():
                    if diff in pools:
                        to_send.extend(pools[diff][:count])
            else:
                indices = []
                try:
                    for part in range_in.replace(' ', '').split(','):
                        if '-' in part:
                            start, end = map(int, part.split('-'))
                            indices.extend(range(start-1, end))
                        else:
                            indices.append(int(part)-1)
                except Exception:
                    print(f"{Style.RED}Invalid format.{Style.RESET}")
                    continue
                for idx in indices:
                    if 0 <= idx < len(target_list):
                        to_send.append(target_list[idx])

            tracks = await asyncio.to_thread(engine.db_get_all_tracks)
            last_seq = tracks.get("last_seq", 100)
            if tracks:
                last_seq = max(v.get('display_id', 100) for v in tracks.values())

            for q in to_send:
                last_seq += 1
                try:
                    if choice == "1":
                        question_text = q.get("native_question") or lite_math(q['question'])

                        options_list = q.get("native_options")
                        if not options_list:
                            options_list = [lite_math(o) for o in q['options']]

                        poll_hint = UIFactory.replace_code_with_italic(UIFactory.generate_poll_hint(q))
                        m = await app.bot.send_poll(
                            chat_id=engine.config['channel'],
                            question=question_text[:290],
                            options=[opt[:90] for opt in options_list],
                            type=Poll.QUIZ,
                            correct_option_id=q['correct_option'],
                            explanation=poll_hint,
                            explanation_parse_mode="HTML"
                        )
                        msg_type = "poll"
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
                                    raise Exception("Kroki rendering failure.")

                        m = await send_rich_message_safe(app.bot, chat_id=engine.config['channel'], html_content=caption, reply_markup=kb, media_bytes=media_bytes, file_id=cached_file_id)
                        msg_type = "photo" if img_url else "text"

                        if img_url and not cached_file_id and m.photo:
                            await asyncio.to_thread(db_save_cached_file_id, cache_key, m.photo[-1].file_id)

                    await asyncio.to_thread(engine.db_save_track, m.message_id, q['id'], "active", last_seq, "premium", msg_type)
                    await asyncio.to_thread(db_mark_question_as_sent, q['id'])
                    from src.database import db_mark_question_shown
                    await asyncio.to_thread(db_mark_question_shown, q['id'])

                    print(f"{Style.GREEN}✅ Sent REF: {last_seq} [{msg_type}]{Style.RESET}")

                    await asyncio.sleep(1.5)

                except Exception as e:
                    traceback.print_exc()
                    print(f"{Style.RED}❌ Failed REF: {last_seq} | {e}{Style.RESET}")
                    await asyncio.sleep(2.0)

            local_sent_tracks = engine.load_json("logs/sent_tracks.json")
            local_sent_tracks["last_seq"] = last_seq
            engine.save_json("logs/sent_tracks.json", local_sent_tracks)

        elif choice == "3":
            while True:
                await asyncio.to_thread(engine.refresh_database)
                all_qs = {q['id']: q for sub_list in engine.db.values() for q in sub_list}
                tracks = await asyncio.to_thread(engine.db_get_all_tracks)

                filtered_mids = [mid for mid, data in tracks.items() if mid.isdigit() and data.get("status") == curr_stat and (curr_type == "bop" or (curr_type == "nap" and data.get("type") == "native") or (curr_type == "prp" and data.get("type") == "premium"))]
                items = sorted(filtered_mids, key=int, reverse=True)
                total_pages = math.ceil(len(items) / 10)
                page = max(0, min(page, total_pages - 1)) if total_pages > 0 else 0

                clear_screen()
                print(f"{Style.MAGENTA}{Style.BOLD}--- {curr_stat.upper()} [{curr_type.upper()}] QUIZZES ({len(items)}) ---{Style.RESET}")
                for i, mid in enumerate(items[page*10 : (page+1)*10]):
                    v = tracks[mid]
                    q_obj = all_qs.get(v['q_id'], {'question': 'Unknown ID'})
                    print(f"  {(page*10)+i+1}. {'[NAT]' if v.get('type')=='native' else '[PRM]'} REF:{v.get('display_id')} | {q_obj['question'][:45]}...")

                print(f"\n  Page {page+1} / {max(1, total_pages)}")
                print(f"{Style.CYAN}Nav: [n] Next | [p] Prev | [p<N>] Jump to page N | [sw] Status | [ft] Filter\nAction: [1,3,5-7] Index/Range | [r167] Direct REF (any page) | [all] Current page | [clean] Live Sync | [b] Back{Style.RESET}")

                cmd = await cli.ask("<b>Command > </b>")
                if not cmd or cmd == 'b':
                    break
                if cmd == 'n':
                    page += 1
                    continue
                if cmd == 'p':
                    page -= 1
                    continue
                if cmd == 'sw':
                    curr_stat = "closed" if curr_stat == "active" else "active"
                    page = 0
                    continue
                if cmd == 'ft':
                    f_val = await cli.ask("<b>Filter [nap/prp/bop]: </b>")
                    if f_val in ['nap', 'prp', 'bop']:
                        curr_type = f_val
                        page = 0
                        continue

                page_jump_match = re.match(r'^p(\d+)$', cmd, re.IGNORECASE)
                if page_jump_match:
                    target_page_num = int(page_jump_match.group(1))
                    max_page_num = max(1, total_pages)
                    if 1 <= target_page_num <= max_page_num:
                        page = target_page_num - 1
                    else:
                        print(f"{Style.RED}Page {target_page_num} is out of range (1-{max_page_num}).{Style.RESET}")
                    continue

                if cmd == 'clean':
                    print(f"{Style.YELLOW}Syncing with Telegram...{Style.RESET}")
                    for mid, v in list(tracks.items()):
                        if mid.isdigit() and v.get("status") != "deleted":
                            try:
                                await app.bot.forward_message(bot_info.id, engine.config['channel'], int(mid))
                            except Exception:
                                await asyncio.to_thread(engine.db_update_track_status, mid, "deleted")
                    continue

                if cmd.lower() == 'all':
                    targets = [m for m in items[page*10 : (page+1)*10] if tracks[m].get('type') != 'native']
                else:
                    targets = _parse_manage_selection(cmd, items, tracks)

                if not targets:
                    print(f"{Style.YELLOW}No valid targets matched — nothing to do.{Style.RESET}")
                    continue

                for mid in set(targets):
                    v = tracks[mid]
                    q = all_qs.get(v['q_id'])
                    ref = v.get('display_id', mid)
                    try:
                        if curr_stat == "active":
                            if "followup_mid" in v:
                                try:
                                    await app.bot.delete_message(engine.config['channel'], int(v["followup_mid"]))
                                except Exception:
                                    pass
                                del v["followup_mid"]
                            if v.get('type') == 'native':
                                try:
                                    await app.bot.stop_poll(engine.config['channel'], int(mid))
                                except BadRequest as e:
                                    if _is_missing_message_error(e):
                                        print(f"{Style.YELLOW}├─ [STALE] Poll msg {mid} (REF:{ref}) no longer exists on channel. Marking as deleted.{Style.RESET}")
                                        await asyncio.to_thread(engine.db_update_track_status, mid, "deleted")
                                        continue
                                    raise
                            else:
                                is_photo = (v.get('msg_type') == "photo")
                                if is_photo:
                                    try:
                                        await app.bot.delete_message(chat_id=engine.config['channel'], message_id=int(mid))
                                    except Exception:
                                        pass

                                    fig_block = UIFactory.build_figure_block(q, add_strut=False)
                                    media_bytes, cached_file_id = None, None

                                    if fig_block:
                                        channel_id = CONFIG.get("channel") or "@QuizOva"
                                        sol_latex = UIFactory.assemble_diagram_only_layout(channel_id, ref, fig_block)
                                        sol_img_url = UIFactory.get_latex_url(sol_latex)

                                        cache_key = f"q:{q['id']}:closed_diag"
                                        cached_file_id = await asyncio.to_thread(db_get_cached_file_id, cache_key)

                                        if not cached_file_id:
                                            async with httpx.AsyncClient() as client:
                                                resp = await fetch_kroki_image(client, sol_img_url, sol_latex)
                                                if resp and resp.status_code == 200:
                                                    media_bytes = resp.content

                                    closed_view = UIFactory.build_closed_static_view(q, ref, compact=False)
                                    new_msg = await send_rich_message_safe(
                                        app.bot,
                                        chat_id=engine.config['channel'],
                                        html_content=closed_view,
                                        reply_markup=None,
                                        media_bytes=media_bytes,
                                        file_id=cached_file_id
                                    )

                                    if media_bytes and new_msg and new_msg.photo and not cached_file_id:
                                        await asyncio.to_thread(db_save_cached_file_id, cache_key, new_msg.photo[-1].file_id)

                                    await asyncio.to_thread(engine.db_swap_track_message_id, mid, new_msg.message_id)
                                    await asyncio.to_thread(engine.db_update_track_status, new_msg.message_id, "closed", clear_followup=True)
                                else:
                                    closed_view = UIFactory.build_closed_static_view(q, ref, compact=False)
                                    try:
                                        await edit_rich_message_safe(app.bot, chat_id=engine.config['channel'], message_id=int(mid), html_content=closed_view, reply_markup=None)
                                        await asyncio.to_thread(engine.db_update_track_status, mid, "closed", clear_followup=True)
                                    except BadRequest as e:
                                        if _is_missing_message_error(e):
                                            print(f"{Style.YELLOW}├─ [STALE] Message {mid} (REF:{ref}) no longer exists on channel. Marking as deleted.{Style.RESET}")
                                            await asyncio.to_thread(engine.db_update_track_status, mid, "deleted")
                                            continue
                                        elif _is_not_modified_error(e):
                                            await asyncio.to_thread(engine.db_update_track_status, mid, "closed", clear_followup=True)
                                        else:
                                            raise
                        else:
                            if v.get('type') == 'native':
                                continue
                            img_url, cap = UIFactory.create_question_assets(q, ref)
                            kb = UIFactory.build_keyboard(q, ref)
                            if v.get('msg_type') == "photo":
                                try:
                                    async with httpx.AsyncClient() as client:
                                        media_bytes = None
                                        if img_url:
                                            fig_block = UIFactory.build_figure_block(q, add_strut=False)
                                            if fig_block:
                                                compiled_latex = UIFactory.assemble_diagram_only_layout(UIFactory.WATERMARK, ref, fig_block)
                                                img_url_kroki = UIFactory.get_latex_url(compiled_latex)
                                                resp = await fetch_kroki_image(client, img_url_kroki, compiled_latex)
                                                if resp and resp.status_code == 200:
                                                    media_bytes = resp.content

                                        if media_bytes:
                                            media = InputMediaPhoto(media=media_bytes, caption=convert_to_legacy_html(cap), parse_mode="HTML")
                                            await app.bot.edit_message_media(chat_id=engine.config['channel'], message_id=int(mid), media=media, reply_markup=kb)
                                    await asyncio.to_thread(engine.db_update_track_status, mid, "active")
                                except BadRequest as e:
                                    if _is_missing_message_error(e):
                                        print(f"{Style.YELLOW}├─ [STALE] Message {mid} (REF:{ref}) no longer exists on channel. Marking as deleted.{Style.RESET}")
                                        await asyncio.to_thread(engine.db_update_track_status, mid, "deleted")
                                        continue
                                    elif _is_not_modified_error(e):
                                        await asyncio.to_thread(engine.db_update_track_status, mid, "active")
                                    else:
                                        raise
                            else:
                                try:
                                    await edit_rich_message_safe(app.bot, chat_id=engine.config['channel'], message_id=int(mid), html_content=cap, reply_markup=kb)
                                    await asyncio.to_thread(engine.db_update_track_status, mid, "active")
                                except BadRequest as e:
                                    if _is_missing_message_error(e):
                                        print(f"{Style.YELLOW}├─ [STALE] Message {mid} (REF:{ref}) no longer exists on channel. Marking as deleted.{Style.RESET}")
                                        await asyncio.to_thread(engine.db_update_track_status, mid, "deleted")
                                        continue
                                    elif _is_not_modified_error(e):
                                        await asyncio.to_thread(engine.db_update_track_status, mid, "active")
                                    else:
                                        raise
                    except Exception as e:
                        traceback.print_exc()
                        print(f"Error processing REF:{ref} | {e}")
                await asyncio.sleep(0.5)

        elif choice == "5":
            print(f"\n{Style.MAGENTA}--- LIVE MULTI-PLAYER SHOWDOWN TOURNAMENT ---{Style.RESET}")
            db = await asyncio.to_thread(engine.refresh_database, force=True)
            subjects = list(db.keys())
            if not subjects:
                print(f"{Style.RED}No questions loaded.{Style.RESET}")
                continue

            for i, s in enumerate(subjects):
                print(f"  {i+1}. {s.upper()} ({len(db[s])} questions)")

            sub_in = await cli.ask("<b>Select Subject for Tournament Showdown #: </b>")
            if not sub_in or sub_in.lower() in ['c', 'q', 'cancel', 'exit']:
                print(f"{Style.YELLOW}[DEBUG-FIX] Setup cancelled at Subject selection.{Style.RESET}")
                continue
            if not sub_in.isdigit() or int(sub_in) > len(subjects):
                continue

            target_list = db[subjects[int(sub_in)-1]]

            for i, q in enumerate(target_list):
                m_tag = f"{Style.MAGENTA}[MATH]{Style.RESET} " if (q.get("latex") or UIFactory.is_complex(q['question'])) else ""
                diff = q.get("difficulty", "medium").lower()
                diff_color = f"{Style.GREEN}[EASY]{Style.RESET}" if diff in ["easy", "weak"] else f"{Style.RED}[HARD]{Style.RESET}" if diff == "hard" else f"{Style.YELLOW}[MED]{Style.RESET}"
                print(f"    {i+1}. {diff_color} {m_tag}[{q['id']}] {q['question'][:45]}...")

            range_in = await cli.ask("<b>Showdown Selection (e.g. 1, 3-5 or easy:3): </b>")
            if not range_in or range_in.lower() in ['c', 'q', 'cancel', 'exit']:
                print(f"{Style.YELLOW}[DEBUG-FIX] Setup cancelled at Question selection.{Style.RESET}")
                continue

            tournament_qs = []

            if ":" in range_in:
                query_parts = [p.strip().split(":") for p in range_in.split(",")]
                requested = {}
                for part in query_parts:
                    if len(part) == 2 and part[1].strip().isdigit():
                        requested[part[0].lower().strip()] = int(part[1].strip())
                pools = {"easy": [], "medium": [], "hard": []}
                for q in target_list:
                    d = "easy" if q.get("difficulty", "medium").lower() == "weak" else q.get("difficulty", "medium").lower()
                    if d in pools:
                        pools[d].append(q)
                for diff, count in requested.items():
                    if diff in pools:
                        tournament_qs.extend(pools[diff][:count])
            else:
                indices = []
                try:
                    for part in range_in.replace(' ', '').split(','):
                        if '-' in part:
                            start, end = map(int, part.split('-'))
                            indices.extend(range(start-1, end))
                        else:
                            indices.append(int(part)-1)
                except Exception:
                    print(f"{Style.RED}Invalid selection syntax.{Style.RESET}")
                    continue
                for idx in indices:
                    if 0 <= idx < len(target_list):
                        tournament_qs.append(target_list[idx])

            if not tournament_qs:
                print(f"{Style.RED}No valid questions selected.{Style.RESET}")
                continue

            duration_in = await cli.ask("<b>Round Duration (e.g. 30s, 1m, 5m - default 60s): </b>")
            round_seconds = parse_duration_to_seconds(duration_in, 60)
            if round_seconds == -99:
                print(f"{Style.YELLOW}[DEBUG-FIX] Setup cancelled at Duration parameter.{Style.RESET}")
                continue

            cooldown_in = await cli.ask("<b>Interval / Cooldown between rounds (e.g. 10s, 30s, 1m - default 15s): </b>")
            cooldown_seconds = parse_duration_to_seconds(cooldown_in, 15)
            if cooldown_seconds == -99:
                print(f"{Style.YELLOW}[DEBUG-FIX] Setup cancelled at Cooldown parameter.{Style.RESET}")
                continue

            delay_in = await cli.ask("<b>Schedule Delay (e.g. 5m, 2h - or press Enter for immediate start): </b>")
            delay_seconds = parse_duration_to_seconds(delay_in, 0)
            if delay_seconds == -99:
                print(f"{Style.YELLOW}[DEBUG-FIX] Setup cancelled at Delay parameter.{Style.RESET}")
                continue

            scheduled_start = None
            announcement_mid = None

            # Compile scope metadata and metrics for the announcement campaign card
            difficulty_counts = {"easy": 0, "medium": 0, "hard": 0}
            topic_set = set()
            subject_name = subjects[int(sub_in)-1].upper()

            for q in tournament_qs:
                diff = q.get("difficulty", "medium").lower()
                if diff == "weak":
                    diff = "easy"
                if diff in difficulty_counts:
                    difficulty_counts[diff] += 1
                if q.get("topic"):
                    topic_set.add(q["topic"])

            topics_list = list(topic_set)
            diff_parts = []
            for k, v in difficulty_counts.items():
                if v > 0:
                    diff_parts.append(f"{v} {k.capitalize()}")
            diff_summary = ", ".join(diff_parts)

            target_time_utc_str = "Immediate Start"
            target_time_eat_str = "Immediate Start"

            # Timezone synchronization directly against Cloud Database clock
            if delay_seconds > 0:
                db_epoch = await asyncio.to_thread(engine.db_get_current_epoch)
                now_utc = datetime.fromtimestamp(db_epoch, timezone.utc)
                scheduled_start = now_utc + timedelta(seconds=delay_seconds)

                target_time_utc_str = scheduled_start.strftime('%Y-%m-%d %H:%M:%S UTC')
                eat_tz = timezone(timedelta(hours=3))
                scheduled_eat = scheduled_start.astimezone(eat_tz)
                target_time_eat_str = scheduled_eat.strftime('%Y-%m-%d %H:%M:%S EAT (GMT+3)')

                print(f"\n{Style.CYAN}[DEBUG-FIX-LOG] Time synchronization completed successfully.{Style.RESET}", flush=True)
                print(f" ├─ Local Host System clock:  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}", flush=True)
                print(f" ├─ Neon Database clock context: {now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}", flush=True)
                print(f" └─ Computed Execution Target: {scheduled_start.strftime('%Y-%m-%d %H:%M:%S UTC')} (Trigger in: {delay_seconds} seconds)", flush=True)
            existing_queue = await asyncio.to_thread(db_get_tournament_queue)
            meta_payload = {
                "subject": subject_name,
                "topics": topics_list,
                "difficulty_summary": diff_summary,
                "total_count": len(tournament_qs),
                "round_seconds": round_seconds,
                "cooldown_seconds": cooldown_seconds,
                "target_time_utc": target_time_utc_str,
                "target_time_eat": target_time_eat_str,
                "run_id": uuid.uuid4().hex,
            }

            from src.rendering.html_views import build_tournament_announcement_text
            proposed_ann_text = build_tournament_announcement_text(meta_payload, delay_seconds)

            print(f"\n{Style.YELLOW}{Style.BOLD}⚔️  PROPOSED TOURNAMENT CAMPAIGN SHEET:{Style.RESET}")
            print(convert_to_legacy_html(proposed_ann_text))

            confirm_commit = await cli.ask("<ansiyellow><b>Confirm and launch/schedule this tournament? (y/n) > </b></ansiyellow>")
            
            # Typo-Safe Confirmation parser to strip any trailing backslashes, numbers, or spaces
            clean_confirm = re.sub(r'[^a-zA-Z]', '', confirm_commit).lower().strip() if confirm_commit else ""
            if not clean_confirm or clean_confirm not in ['y', 'yes']:
                print(f"\n{Style.RED}[ABORT] Transaction canceled. No updates made. Returning to Main Cockpit...{Style.RESET}\n")
                await asyncio.sleep(1.5)
                continue

            print(f"\n{Style.GREEN}[TX COMMIT] Transaction confirmed! Initializing database payload...{Style.RESET}")

            if delay_seconds > 0:
                try:
                    channel_id = engine.config['channel']
                    ann_msg = await app.bot.send_message(chat_id=channel_id, text=proposed_ann_text, parse_mode="HTML")
                    announcement_mid = ann_msg.message_id
                    from src.tournament import _pin_safe
                    await _pin_safe(app.bot, channel_id, announcement_mid)
                except Exception as ann_err:
                    print(f"{Style.YELLOW}Could not post scheduled announcement to channel: {ann_err}{Style.RESET}")

            tracks = await asyncio.to_thread(engine.db_get_all_tracks)
            last_seq = max((v.get('display_id', 100) for v in tracks.values()), default=100)

            from src.tournament import launch_tournament_round
            import src.tournament

            async with src.tournament._LAUNCH_LOCK:
                q_ids = [q['id'] for q in tournament_qs]
                total_count = len(tournament_qs)

                active_rounds = await asyncio.to_thread(db_get_active_tournament_rounds)
                
                has_pending_series = bool(existing_queue and existing_queue.get('remaining_ids'))

                if active_rounds or has_pending_series:
                    merged_ids = (existing_queue['remaining_ids'] if existing_queue else []) + q_ids
                    merged_total = (existing_queue['total_count'] if existing_queue else 0) + total_count
                    merged_last_seq = max(existing_queue['last_seq'] if existing_queue else last_seq, last_seq)

                    await asyncio.to_thread(
                        db_save_tournament_queue,
                        merged_ids,
                        merged_last_seq,
                        round_seconds,
                        merged_total,
                        scheduled_start.isoformat() if scheduled_start else (existing_queue.get('scheduled_start').isoformat() if (existing_queue and existing_queue.get('scheduled_start')) else None),
                        announcement_mid or (existing_queue.get('announcement_mid') if existing_queue else None),
                        cooldown_seconds,
                        meta_payload
                    )
                    if scheduled_start:
                        print(f"{Style.GREEN}✅ Tournament successfully scheduled and appended to existing queue!{Style.RESET}")
                    else:
                        print(f"{Style.YELLOW}⚠️ A round is already live/queued. Question(s) appended to the queue.{Style.RESET}")
                else:
                    if scheduled_start is not None:
                        await asyncio.to_thread(
                            db_save_tournament_queue,
                            q_ids,
                            last_seq,
                            round_seconds,
                            total_count,
                            scheduled_start.isoformat(),
                            announcement_mid,
                            cooldown_seconds,
                            meta_payload
                        )
                        print(f"{Style.GREEN}✅ Tournament successfully scheduled and queued!{Style.RESET}")
                    else:
                        first_id = q_ids.pop(0)
                        await asyncio.to_thread(
                            db_save_tournament_queue,
                            q_ids,
                            last_seq + 1,
                            round_seconds,
                            total_count,
                            None,
                            None,
                            cooldown_seconds,
                            meta_payload
                        )
                        first_q = await asyncio.to_thread(db_get_question_by_id, first_id) or tournament_qs[0]
                        await launch_tournament_round(app, engine, first_q, last_seq + 1, round_seconds=round_seconds, current_round=1, total_rounds=total_count)

                verification = await asyncio.to_thread(db_get_tournament_queue)
                print(f"\n{Style.YELLOW}[DATABASE-VERIFICATION] Stored Row in 'tournament_queue':{Style.RESET}")
                if verification:
                    print(f" ├─ remaining_ids: {verification.get('remaining_ids')}")
                    print(f" ├─ total_count: {verification.get('total_count')}")
                    print(f" ├─ scheduled_start: {verification.get('scheduled_start')} (type={type(verification.get('scheduled_start'))})")
                    print(f" └─ cooldown_seconds: {verification.get('cooldown_seconds')} (type={type(verification.get('cooldown_seconds'))})")
                else:
                    print(" └─ [ERROR] No queue record exists in database table!")

        elif choice == "6":
            await render_control_center_panel(app, engine, cli)

        elif choice == "7":
            await render_emergency_stop_panel(app, engine, cli)

        elif choice == "8":
            await render_smart_scheduler(app, engine, cli)
        elif choice == "9":
            await render_bot_settings_panel(engine, cli)


async def render_control_center_panel(app, engine: QuizEngine, cli: CLI):
    """Control panel that lists and modifies scheduled single questions and pending tournaments."""
    while True:
        clear_screen()
        print(f"{Style.MAGENTA}{Style.BOLD}--- PLANNED / SCHEDULED TASKS CONTROL CENTER ---{Style.RESET}\n")

        # 1. Fetch Planned Items
        sched_qs = await asyncio.to_thread(db_get_upcoming_scheduled_questions)
        tourney_queue = await asyncio.to_thread(db_get_tournament_queue)

        has_tourney = tourney_queue and tourney_queue.get("remaining_ids")

        print(f"📅 {Style.YELLOW}PART A: Upcoming Scheduled Questions ({len(sched_qs)} items){Style.RESET}")
        if not sched_qs:
            print("  (No single questions scheduled in the future)")
        else:
            for i, q in enumerate(sched_qs):
                t_str = str(q['scheduled_for'])
                print(f"  {i+1}. [{q['id']}] {Style.WHITE}{q['question'][:45]}...{Style.RESET}\n     ├─ Topic: {q['topic']} │ Diff: {q.get('difficulty','medium')}\n     └─ Planned For: {Style.CYAN}{t_str}{Style.RESET}")

        print(f"\n⚔️ {Style.YELLOW}PART B: Pending Tournament Queue{Style.RESET}")
        if not has_tourney:
            print("  (No tournaments currently planned or queued)")
        else:
            rem_cnt = len(tourney_queue['remaining_ids'])
            start_val = tourney_queue.get('scheduled_start') or "Immediate launch next block"
            paused_lbl = f"{Style.RED}[PAUSED]{Style.RESET}" if tourney_queue.get('is_paused') else f"{Style.GREEN}[ACTIVE/READY]{Style.RESET}"
            print(f"  Status: {paused_lbl}")
            print(f"  Planned Start: {Style.CYAN}{start_val}{Style.RESET}")
            print(f"  Remaining Question IDs ({rem_cnt}/{tourney_queue['total_count']}): {tourney_queue['remaining_ids']}")
            print(f"  Timing Bounds: {tourney_queue['round_seconds']}s round duration │ {tourney_queue['cooldown_seconds']}s rest interval")

        print(f"\n{Style.CYAN}Menu Options:\n [1] 📝 Manage Scheduled Single Questions (Reschedule/Edit/Cancel)\n [2] ⚙️  Manage Pending Tournament Parameters & Timers\n [b] Back to Control Center{Style.RESET}")

        sub_choice = await cli.ask("<b>Scheduler Command > </b>")
        if not sub_choice or sub_choice == 'b':
            break

        if sub_choice == "1":
            if not sched_qs:
                print(f"{Style.YELLOW}No scheduled questions found to manage.{Style.RESET}")
                await asyncio.sleep(1.0)
                continue

            idx_in = await cli.ask("<b>Select question # to Manage: </b>")
            if idx_in and idx_in.isdigit() and 1 <= int(idx_in) <= len(sched_qs):
                target_q = sched_qs[int(idx_in)-1]

                # Fetch full question details from database to work with raw fields
                q_full = await asyncio.to_thread(db_get_question_by_id, target_q['id'])
                if not q_full:
                    print(f"{Style.RED}Error: Failed to fetch question payload.{Style.RESET}")
                    await asyncio.sleep(1.5)
                    continue

                while True:
                    clear_screen()
                    print(f"{Style.YELLOW}{Style.BOLD}--- MANAGE SCHEDULED QUESTION: {q_full['id']} ---{Style.RESET}")
                    print(f"  Current Text: {Style.WHITE}{q_full['question']}{Style.RESET}")
                    print(f"  Subject:      {q_full['subject']} │ Topic: {q_full['topic']}")
                    print(f"  Difficulty:   {q_full.get('difficulty', 'medium')} │ Tags: {q_full.get('tags', [])}")
                    print(f"  Options:      {q_full['options']}")
                    print(f"  Scheduled:    {Style.CYAN}{q_full['scheduled_for']}{Style.RESET}")
                    print(f"\n  Actions:")
                    print("   [1] ⏰ Reschedule (Change Date/Time)")
                    print("   [2] 📥 Cancel Schedule (Remove date, revert to manual draft list)")
                    print("   [3] 📝 Edit Question Text")
                    print("   [4] 📋 Edit Options Text")
                    print("   [5] 🏷️  Modify Metadata (Subject, Topic, Difficulty, Tags)")
                    print("   [6] 🚀 Publish Immediately")
                    print("   [b] Back to Control Center")

                    act = await cli.ask("<b>Select Action > </b>")
                    if not act or act == 'b':
                        break

                    conn = engine.get_db_connection()
                    try:
                        if act == "1":
                            new_date = await cli.ask("<b>New ISO Timestamp (e.g. 2026-07-20T18:00:00+03:00): </b>")
                            if new_date:
                                success = await asyncio.to_thread(db_reschedule_question, q_full['id'], new_date)
                                if success:
                                    print(f"{Style.GREEN}[TX COMMIT] Rescheduled successfully to {new_date}.{Style.RESET}")
                                    q_full['scheduled_for'] = new_date
                                    await asyncio.sleep(1.5)

                        elif act == "2":
                            confirm = await cli.ask("<b>Confirm unscheduling to manual drafts? (y/n): </b>")
                            if confirm and confirm.lower() == 'y':
                                success = await asyncio.to_thread(db_reschedule_question, q_full['id'], None)
                                if success:
                                    print(f"{Style.GREEN}[TX COMMIT] Schedule cleared. Question reverted to draft.{Style.RESET}")
                                    q_full['scheduled_for'] = None
                                    await asyncio.sleep(1.5)

                        elif act == "3":
                            new_text = await cli.ask("<b>Enter New Question Text (HTML supported): </b>")
                            if new_text:
                                with conn.cursor() as cur:
                                    cur.execute("UPDATE questions SET question = %s WHERE id = %s;", (new_text, q_full['id']))
                                    conn.commit()
                                print(f"{Style.GREEN}[TX COMMIT] Question text updated.{Style.RESET}")
                                q_full['question'] = new_text
                                await asyncio.sleep(1.5)

                        elif act == "4":
                            print(f"\n  Current options: {q_full['options']}")
                            opt_idx = await cli.ask("<b>Select option index to edit (0 to 3) or Enter to abort: </b>")
                            if opt_idx and opt_idx.isdigit() and 0 <= int(opt_idx) < len(q_full['options']):
                                idx = int(opt_idx)
                                new_opt = await cli.ask(f"<b>Enter new value for Option {chr(65+idx)}: </b>")
                                if new_opt:
                                    new_opts = list(q_full['options'])
                                    new_opts[idx] = new_opt
                                    with conn.cursor() as cur:
                                        cur.execute("UPDATE questions SET options = %s WHERE id = %s;", (new_opts, q_full['id']))
                                        conn.commit()
                                    print(f"{Style.GREEN}[TX COMMIT] Option {chr(65+idx)} updated.{Style.RESET}")
                                    q_full['options'] = new_opts
                                    await asyncio.sleep(1.5)

                        elif act == "5":
                            print("\n  [1] Subject │ [2] Topic │ [3] Difficulty │ [4] Tags")
                            meta_choice = await cli.ask("<b>Select Metadata to edit: </b>")
                            if meta_choice == "1":
                                val = await cli.ask("<b>Enter Subject: </b>")
                                if val:
                                    with conn.cursor() as cur:
                                        cur.execute("UPDATE questions SET subject = %s WHERE id = %s;", (val, q_full['id']))
                                        conn.commit()
                                    q_full['subject'] = val
                            elif meta_choice == "2":
                                val = await cli.ask("<b>Enter Topic: </b>")
                                if val:
                                    with conn.cursor() as cur:
                                        cur.execute("UPDATE questions SET topic = %s WHERE id = %s;", (val, q_full['id']))
                                        conn.commit()
                                    q_full['topic'] = val
                            elif meta_choice == "3":
                                val = await cli.ask("<b>Enter Difficulty (easy/medium/hard): </b>")
                                if val:
                                    with conn.cursor() as cur:
                                        cur.execute("UPDATE questions SET difficulty = %s WHERE id = %s;", (val, q_full['id']))
                                        conn.commit()
                                    q_full['difficulty'] = val
                            elif meta_choice == "4":
                                val = await cli.ask("<b>Enter Tags (comma-separated): </b>")
                                if val:
                                    tags_list = [t.strip() for t in val.split(',')]
                                    with conn.cursor() as cur:
                                        cur.execute("UPDATE questions SET tags = %s WHERE id = %s;", (tags_list, q_full['id']))
                                        conn.commit()
                                    q_full['tags'] = tags_list
                            print(f"{Style.GREEN}[TX COMMIT] Metadata updated.{Style.RESET}")
                            await asyncio.sleep(1.5)

                        elif act == "6":
                            confirm = await cli.ask("<b>Force Immediate Publish via Background Daemon? (y/n): </b>")
                            if confirm and confirm.lower() == 'y':
                                # Set date to 5 seconds ago to immediately trigger background scheduled task
                                past_dt = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
                                success = await asyncio.to_thread(db_reschedule_question, q_full['id'], past_dt)
                                if success:
                                    print(f"{Style.GREEN}[TX COMMIT] Triggered! The background daemon will publish this in the next cycle.{Style.RESET}")
                                    await asyncio.sleep(2.0)
                                    break
                    except Exception as tx_err:
                        conn.rollback()
                        print(f"{Style.RED}[TX ROLLBACK] Transaction failed: {tx_err}{Style.RESET}")
                        await asyncio.sleep(2.0)
                    finally:
                        engine.release_connection(conn)

        elif sub_choice == "2":
            if not has_tourney:
                print(f"{Style.YELLOW}No pending tournaments found to edit.{Style.RESET}")
                await asyncio.sleep(1.0)
                continue

            while True:
                clear_screen()
                print(f"{Style.MAGENTA}{Style.BOLD}--- MANAGE PENDING TOURNAMENT QUEUE ---{Style.RESET}\n")
                print(f"  Status:               {Style.WHITE}{'PAUSED' if tourney_queue.get('is_paused') else 'RUNNING/READY'}{Style.RESET}")
                print(f"  Planned Start:        {Style.CYAN}{tourney_queue.get('scheduled_start')}{Style.RESET}")
                print(f"  Remaining Question Count: {len(tourney_queue['remaining_ids'])} items")
                print(f"  Round Timing Duration: {tourney_queue['round_seconds']} seconds")
                print(f"  Rest Interval Cooldown: {tourney_queue['cooldown_seconds']} seconds")
                print(f"\n  Actions:")
                print("   [1] ⏰ Postpone / Reschedule Start Time")
                print("   [2] 🔧 Modify Timing Parameters (Duration & Cooldown)")
                print("   [3] 🛑 Cancel & Delete Entire Tournament Queue")
                print("   [b] Back to Control Center")

                act = await cli.ask("<b>Select Action > </b>")
                if not act or act == 'b':
                    break

                if act == "1":
                    new_start = await cli.ask("<b>New Tournament Start ISO Date (or 'CLEAR' for immediate start): </b>")
                    if new_start:
                        val = "CLEAR" if new_start.upper() == "CLEAR" else new_start
                        success = await asyncio.to_thread(db_update_tournament_schedule_params, scheduled_start=val)
                        if success:
                            print(f"{Style.GREEN}✅ Tournament schedule updated.{Style.RESET}")
                            tourney_queue = await asyncio.to_thread(db_get_tournament_queue)
                            await asyncio.sleep(1.5)

                elif act == "2":
                    dur_in = await cli.ask("<b>Modify Round Duration (seconds or Enter to skip): </b>")
                    cool_in = await cli.ask("<b>Modify Cooldown Interval (seconds or Enter to skip): </b>")

                    dur = int(dur_in) if (dur_in and dur_in.isdigit()) else None
                    cool = int(cool_in) if (cool_in and cool_in.isdigit()) else None

                    if dur is not None or cool is not None:
                        success = await asyncio.to_thread(db_update_tournament_schedule_params, round_seconds=dur, cooldown_seconds=cool)
                        if success:
                            print(f"{Style.GREEN}✅ Queue parameters updated successfully.{Style.RESET}")
                            tourney_queue = await asyncio.to_thread(db_get_tournament_queue)
                            await asyncio.sleep(1.5)

                elif act == "3":
                    confirm = await cli.ask("<b>Are you sure you want to completely clear the tournament queue? (y/n): </b>")
                    if confirm and confirm.lower() == 'y':
                        ann_mid = tourney_queue.get('announcement_mid')
                        if ann_mid:
                            try:
                                await app.bot.delete_message(chat_id=engine.config['channel'], message_id=int(ann_mid))
                            except Exception:
                                pass
                        await asyncio.to_thread(db_clear_tournament_queue)
                        print(f"{Style.RED}✅ Scheduled tournament deleted and queue flushed.{Style.RESET}")
                        await asyncio.sleep(1.5)
                        break

async def render_smart_scheduler(app, engine: QuizEngine, cli: CLI):
    """Admin sets policy once (subject weights, difficulty target); the algorithm
    proposes a diversified, cooldown-safe batch. Admin reviews a summary — not a
    raw list — and confirms, regenerates, or swaps individual slots.

    Every prompt accepts 'cancel', 'q', or 'exit' (or bare Ctrl+C/EOF) to abort
    immediately with zero side effects — nothing touches the database until the
    final [c]onfirm step on the proposed batch itself.
    """
    from src.database import (
        db_get_scheduling_pool, db_get_recent_post_history, db_mark_question_shown,
        db_get_question_by_id, db_get_cooldown_stats
    )
    from src.scheduler import select_batch

    def _is_abort(raw: str) -> bool:
        return raw is None or raw.strip().lower() in ('cancel', 'q', 'exit')

    clear_screen()
    print(f"{Style.MAGENTA}{Style.BOLD}--- 🎯 SMART SCHEDULER ---{Style.RESET}\n")
    print(
        "This auto-picks a diversified batch instead of you hand-selecting.\n"
        "Two safeguards run underneath every suggestion:\n\n"
        f"{Style.CYAN}📅 COOLDOWN{Style.RESET} — a question sent to the channel within the last N\n"
        "   days is completely EXCLUDED from selection (a hard filter, not a soft\n"
        "   preference). This is what stops students seeing a repeat within three\n"
        "   weeks. Set N=0 to disable it for this run only.\n\n"
        f"{Style.CYAN}⚖️  DIVERSITY & BALANCE{Style.RESET} — among what passes cooldown, the scorer\n"
        "   favors questions unseen the longest, and nudges subject/difficulty mix\n"
        "   back toward your target ratio.\n\n"
        "Type 'cancel' at any prompt to abort — nothing is sent until you confirm\n"
        "the final proposed batch.\n"
    )

    cooldown_in = await cli.ask("<b>Cooldown days before a question can repeat (default 21, 'cancel' to abort): </b>")
    if _is_abort(cooldown_in):
        print(f"{Style.YELLOW}Cancelled.{Style.RESET}")
        return
    cooldown_days = int(cooldown_in) if cooldown_in and cooldown_in.isdigit() else 21

    batch_in = await cli.ask("<b>How many questions to schedule? (default 5, 'cancel' to abort): </b>")
    if _is_abort(batch_in):
        print(f"{Style.YELLOW}Cancelled.{Style.RESET}")
        return
    batch_n = int(batch_in) if batch_in and batch_in.isdigit() else 5

    diff_in = await cli.ask("<b>Difficulty target easy/medium/hard % (default 40/40/20, e.g. '30 50 20', 'cancel' to abort): </b>")
    if _is_abort(diff_in):
        print(f"{Style.YELLOW}Cancelled.{Style.RESET}")
        return
    if diff_in:
        try:
            e, m, h = [int(x) / 100.0 for x in diff_in.split()]
            difficulty_target = {"easy": e, "medium": m, "hard": h}
        except Exception:
            difficulty_target = {"easy": 0.4, "medium": 0.4, "hard": 0.2}
    else:
        difficulty_target = {"easy": 0.4, "medium": 0.4, "hard": 0.2}

    boost_in = await cli.ask("<b>Curriculum boost — topic name to prioritize this run (or Enter to skip, 'cancel' to abort): </b>")
    if _is_abort(boost_in):
        print(f"{Style.YELLOW}Cancelled.{Style.RESET}")
        return
    topic_boosts = {boost_in.strip(): 2.0} if boost_in and boost_in.strip() else {}

    pool = await asyncio.to_thread(db_get_scheduling_pool, cooldown_days)
    if not pool:
        stats = await asyncio.to_thread(db_get_cooldown_stats)

        print(f"{Style.RED}⚠️  No eligible questions at a {cooldown_days}-day cooldown.{Style.RESET}\n")
        print(f"  Total questions in bank: {stats['total']}")
        print(f"  Never shown before:      {stats['never_shown']}")
        if stats['min_days_since_shown'] is not None:
            usable_cooldown = max(0, int(stats['min_days_since_shown']))
            print(f"  Most recently overdue question was last shown {stats['min_days_since_shown']:.1f} day(s) ago.")
            print(f"  → A cooldown of {usable_cooldown} day(s) or lower would return at least one question.\n")
        else:
            print(f"  → No send history found; this is likely a fresh question bank.\n")

        print("[1] Retry with a smaller cooldown")
        print("[2] Bypass cooldown entirely for this run (ignores repeat protection)")
        print("[b] Back to Main Cockpit")
        fallback = await cli.ask("<b>Action > </b>")
        if _is_abort(fallback) or not fallback or fallback == 'b':
            return

        if fallback == "1":
            new_cd_in = await cli.ask("<b>New cooldown in days ('cancel' to abort): </b>")
            if _is_abort(new_cd_in):
                return
            if new_cd_in and new_cd_in.isdigit():
                cooldown_days = int(new_cd_in)
                pool = await asyncio.to_thread(db_get_scheduling_pool, cooldown_days)
        elif fallback == "2":
            cooldown_days = 0
            pool = await asyncio.to_thread(db_get_scheduling_pool, 0)

        if not pool:
            print(f"{Style.RED}Still no eligible questions — the question bank itself may be empty. Import questions first (option 4 on the main menu).{Style.RESET}")
            await cli.ask("\nPress Enter to return...")
            return

    history = await asyncio.to_thread(db_get_recent_post_history, 7)

    while True:
        selected = select_batch(
            pool, history, n=batch_n,
            difficulty_target=difficulty_target,
            topic_boosts=topic_boosts
        )

        if not selected:
            print(f"{Style.RED}Scheduler couldn't fill a batch — pool too small after cooldown filtering.{Style.RESET}")
            await cli.ask("\nPress Enter to return...")
            return

        subj_tally, diff_tally = {}, {}
        clear_screen()
        print(f"{Style.YELLOW}{Style.BOLD}--- PROPOSED BATCH ({len(selected)} questions) ---{Style.RESET}\n")
        for i, q in enumerate(selected):
            diff = q.get("difficulty", "medium")
            subj_tally[q["subject"]] = subj_tally.get(q["subject"], 0) + 1
            diff_tally[diff] = diff_tally.get(diff, 0) + 1
            print(f"  {i+1}. [{q['subject']} • {diff}] {q['question'][:55]}...")

        print(f"\n{Style.CYAN}Mix: {' | '.join(f'{k}: {v}' for k,v in subj_tally.items())}")
        print(f"Difficulty: {' | '.join(f'{k}: {v}' for k,v in diff_tally.items())}{Style.RESET}\n")

        print("[c] Confirm & Send Now  [r] Regenerate  [s] Swap one slot  [x] Cancel & Abort")
        action = await cli.ask("<b>Action > </b>")

        if action is None or action == 'x':
            print(f"{Style.YELLOW}Cancelled — nothing was sent.{Style.RESET}")
            return
        if not action or action == 'b':
            return

        if action == 'r':
            continue

        if action == 's':
            idx_in = await cli.ask(f"<b>Slot to swap (1-{len(selected)}, 'cancel' to abort swap): </b>")
            if idx_in and idx_in.strip().lower() not in ('cancel', 'q', 'exit') and idx_in.isdigit() and 1 <= int(idx_in) <= len(selected):
                idx = int(idx_in) - 1
                remaining_pool = [q for q in pool if q["id"] not in [s["id"] for s in selected]]
                if remaining_pool:
                    replacement = select_batch(remaining_pool, history, n=1, difficulty_target=difficulty_target, topic_boosts=topic_boosts)
                    if replacement:
                        selected[idx] = replacement[0]
            continue

        if action == 'c':
            tracks = await asyncio.to_thread(engine.db_get_all_tracks)
            last_seq = max((v.get('display_id', 100) for v in tracks.values()), default=100)

            for q in selected:
                last_seq += 1
                try:
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

                    m = await send_rich_message_safe(app.bot, chat_id=engine.config['channel'], html_content=caption, reply_markup=kb, media_bytes=media_bytes, file_id=cached_file_id)
                    msg_type = "photo" if img_url else "text"
                    if img_url and not cached_file_id and m.photo:
                        await asyncio.to_thread(db_save_cached_file_id, cache_key, m.photo[-1].file_id)

                    await asyncio.to_thread(engine.db_save_track, m.message_id, q['id'], "active", last_seq, "premium", msg_type)
                    await asyncio.to_thread(db_mark_question_as_sent, q['id'])
                    await asyncio.to_thread(db_mark_question_shown, q['id'])
                    print(f"{Style.GREEN}✅ Sent REF: {last_seq} [{q['subject']} • {q.get('difficulty')}]{Style.RESET}")
                    await asyncio.sleep(1.5)
                except Exception as e:
                    traceback.print_exc()
                    print(f"{Style.RED}❌ Failed REF: {last_seq} | {e}{Style.RESET}")

            local_sent_tracks = engine.load_json("logs/sent_tracks.json")
            local_sent_tracks["last_seq"] = last_seq
            engine.save_json("logs/sent_tracks.json", local_sent_tracks)

            print(f"\n{Style.GREEN}Batch sent. {len(selected)} questions posted, cooldown timers reset.{Style.RESET}")
            await asyncio.sleep(2.0)
            return


async def render_emergency_stop_panel(app, engine: QuizEngine, cli: CLI):
    """Emergency control system that pauses, resumes, or stops active tournaments."""
    clear_screen()
    print(f"{Style.RED}{Style.BOLD}--- TOURNAMENT EMERGENCY INTERRUPT SYSTEM ---{Style.RESET}\n")

    active_rounds = await asyncio.to_thread(db_get_active_tournament_rounds)
    queue = await asyncio.to_thread(db_get_tournament_queue)

    has_active_round = len(active_rounds) > 0
    has_queue = queue and queue.get('remaining_ids')

    if not has_active_round and not has_queue:
        print("💡 No live or queued tournaments detected. Everything is idle.")
        await cli.ask("\nPress Enter to return...")
        return

    if has_active_round:
        print(f"🔥 {Style.YELLOW}WARNING: Active Round detected!{Style.RESET}")
        for t in active_rounds:
            print(f"  Round: {t.get('round_number')}/{t.get('total_rounds')} │ REF: {t.get('display_id')} │ Message ID: {t.get('message_id')}")
    else:
        print("💤 There are no currently active rounds, but a pending queue exists.")

    if queue:
        paused_status = f"{Style.RED}[PAUSED]{Style.RESET}" if queue.get('is_paused') else f"{Style.GREEN}[RUNNING/WAITING]{Style.RESET}"
        print(f"Tournament Queue Status: {paused_status}")
        print(f"Remaining Questions in Queue: {len(queue['remaining_ids'])} items")

    print(f"\n{Style.CYAN}Menu Options:\n [1] ⏸️  Pause Tournament (Halt active round + freeze queue)\n [2] ▶️  Resume Tournament Queue (Unfreeze execution loop)\n [3] ⏹️  Kill Tournament (Halt active round + wipe out queue)\n [b] Back to Cockpit{Style.RESET}")

    action = await cli.ask("<b>Emergency Action > </b>")
    if not action or action == 'b':
        return

    from src.tournament import halt_active_tournament

    if action == "1":
        reason = await cli.ask("<b>Input halt reason displayed to channel (or enter to default): </b>")
        print(f"{Style.YELLOW}Executing pause...{Style.RESET}")
        await halt_active_tournament(app, engine, clear_queue=False, halt_reason=reason)
        print(f"{Style.GREEN}✅ Tournament paused.{Style.RESET}")
        await asyncio.sleep(2.0)

    elif action == "2":
        print(f"{Style.YELLOW}Resuming...{Style.RESET}")
        await asyncio.to_thread(db_set_tournament_pause_state, False)
        print(f"{Style.GREEN}✅ Tournament queue resumed.{Style.RESET}")
        await asyncio.sleep(1.5)

    elif action == "3":
        confirm = await cli.ask("<b>Are you sure you want to completely kill and delete this entire tournament? (y/n): </b>")
        if confirm and confirm.lower() == 'y':
            reason = await cli.ask("<b>Input halt reason displayed to channel (or enter to default): </b>")
            print(f"{Style.RED}Terminating live tournament...{Style.RESET}")
            await halt_active_tournament(app, engine, clear_queue=True, halt_reason=reason)
            print(f"{Style.RED}✅ Live tournament deleted and wiped.{Style.RESET}")
            await asyncio.sleep(2.0)


async def render_bot_settings_panel(engine: QuizEngine, cli: CLI):
    """CRUD panel for bot-wide runtime settings stored in the `bot_state` table —
    the two auto-cleanup timers that keep the channel and DMs uncluttered."""
    from src.database import db_get_bot_state, db_set_bot_state

    while True:
        clear_screen()
        round_ttl = await asyncio.to_thread(db_get_bot_state, "round_complete_ttl_seconds", 300)
        nudge_ttl = await asyncio.to_thread(db_get_bot_state, "no_answer_nudge_ttl_seconds", 45)

        print(f"{Style.MAGENTA}{Style.BOLD}--- ⚙️  BOT SETTINGS ---{Style.RESET}\n")
        print("Controls how long transient messages stay visible before auto-deleting.\n")
        print(f"  [1] Round-complete card lifetime:  {Style.CYAN}{round_ttl}s{Style.RESET}")
        print(f"      (how long the 🏁 ROUND COMPLETE / interrupted-round card stays")
        print(f"       visible in the channel before auto-deleting)\n")
        print(f"  [2] DM 'no answer yet' nudge lifetime: {Style.CYAN}{nudge_ttl}s{Style.RESET}")
        print(f"      (how long the 📭 nudge stays before auto-deleting itself)\n")
        print("  [b] Back to Main Cockpit")

        choice = await cli.ask("<b>Setting to edit > </b>")
        if not choice or choice == 'b':
            return

        if choice == "1":
            val = await cli.ask(f"<b>New round-complete lifetime in seconds (current {round_ttl}, 'cancel' to abort): </b>")
            if val and val.strip().lower() not in ('cancel', 'q', 'exit') and val.isdigit():
                await asyncio.to_thread(db_set_bot_state, "round_complete_ttl_seconds", int(val))
                print(f"{Style.GREEN}✅ Updated.{Style.RESET}")
                await asyncio.sleep(1.0)
        elif choice == "2":
            val = await cli.ask(f"<b>New nudge lifetime in seconds (current {nudge_ttl}, 'cancel' to abort): </b>")
            if val and val.strip().lower() not in ('cancel', 'q', 'exit') and val.isdigit():
                await asyncio.to_thread(db_set_bot_state, "no_answer_nudge_ttl_seconds", int(val))
                print(f"{Style.GREEN}✅ Updated.{Style.RESET}")
                await asyncio.sleep(1.0)