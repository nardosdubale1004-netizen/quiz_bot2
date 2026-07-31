# Replace src/cli.py with this updated file:

import math
import os
import re
import json
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
    """Parses delay expressions such as 30s, 5m, or 1h into raw seconds."""
    if not text:
        return default
    text = text.strip().lower()
    match = re.match(r'^([\d.]+)\s*([smh]?)$', text)
    if not match:
        return default
    val = float(match.group(1))
    unit = match.group(2)
    if unit == 'm':
        return int(val * 60)
    elif unit == 'h':
        return int(val * 3600)
    return int(val)

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
    try:
        perf_card = await asyncio.to_thread(
            process_user_score,
            u_id, message_id, q['id'],
            is_correct, sel_opt
        )

        explanation_html = UIFactory.build_answered_view(
            q, str(last_seq), sel_opt,
            show_derivation=True, show_perf=False,
            perf_card=perf_card
        )

        kb = UIFactory.build_answered_keyboard(
            last_seq, sel_opt,
            show_derivation=True, show_perf=False,
            is_photo=False, message_id=message_id
        )

        has_tikz = UIFactory.has_real_diagram(q)
        media_bytes = None
        cached_file_id = None

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

        m = await edit_rich_message_safe(
            bot,
            chat_id=u_id,
            message_id=p_mid,
            html_content=explanation_html,
            reply_markup=kb,
            media_bytes=media_bytes,
            file_id=cached_file_id
        )

        if media_bytes and m and m.photo and not cached_file_id:
            await asyncio.to_thread(db_save_cached_file_id, cache_key, m.photo[-1].file_id)

    except Exception:
        try:
            kb = UIFactory.build_answered_keyboard(
                last_seq, sel_opt,
                show_derivation=True, show_perf=False,
                is_photo=False, message_id=message_id
            )
            await send_rich_message_safe(
                bot,
                chat_id=u_id,
                html_content=explanation_html,
                reply_markup=kb,
                media_bytes=media_bytes,
                file_id=cached_file_id
            )
        except Exception:
            pass

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
            if not sub_in or not sub_in.isdigit() or int(sub_in) > len(subjects):
                continue

            target_list = db[subjects[int(sub_in)-1]]

            for i, q in enumerate(target_list):
                m_tag = f"{Style.MAGENTA}[MATH]{Style.RESET} " if (q.get("latex") or UIFactory.is_complex(q['question'])) else ""
                diff = q.get("difficulty", "medium").lower()
                diff_color = f"{Style.GREEN}[EASY]{Style.RESET}" if diff in ["easy", "weak"] else f"{Style.RED}[HARD]{Style.RESET}" if diff == "hard" else f"{Style.YELLOW}[MED]{Style.RESET}"
                print(f"    {i+1}. {diff_color} {m_tag}[{q['id']}] {q['question'][:45]}...")

            range_in = await cli.ask("<b>Showdown Selection (e.g. 1, 3-5 or easy:3): </b>")
            if not range_in:
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

            cooldown_in = await cli.ask("<b>Interval / Cooldown between rounds (e.g. 10s, 30s, 1m - default 15s): </b>")
            cooldown_seconds = parse_duration_to_seconds(cooldown_in, 15)

            delay_in = await cli.ask("<b>Schedule Delay (e.g. 5m, 2h - or press Enter for immediate start): </b>")
            delay_seconds = parse_duration_to_seconds(delay_in, 0)

            scheduled_start = None
            announcement_mid = None

            if delay_seconds > 0:
                now_utc = datetime.now(timezone.utc)
                scheduled_start = now_utc + timedelta(seconds=delay_seconds)
                print(f"{Style.GREEN}Scheduling tournament to start at {scheduled_start.strftime('%Y-%m-%d %H:%M:%S UTC')} (in {delay_in}){Style.RESET}")

                try:
                    channel_id = engine.config['channel']
                    mins_left = int(delay_seconds / 60) or 1
                    ann_text = (
                        f"📢 <b>UPCOMING LIVE TOURNAMENT SHOWDOWN</b> ⚔️\n\n"
                        f"Prepare yourself, scholars! A live tournament series will begin soon.\n\n"
                        f"⏰ <b>Starting in:</b> ~{mins_left} minute(s)\n"
                        f"📋 <b>Total Questions:</b> {len(tournament_qs)}\n"
                        f"⏱️ <b>Round Duration:</b> {round_seconds} seconds\n"
                        f"❄️ <b>Round Interval:</b> {cooldown_seconds} seconds\n\n"
                        f"<i>Set your notifications ON! Correct and rapid answers earn maximum leaderboard marks.</i>"
                    )
                    ann_msg = await app.bot.send_message(chat_id=channel_id, text=ann_text, parse_mode="HTML")
                    announcement_mid = ann_msg.message_id
                except Exception as ann_err:
                    print(f"{Style.YELLOW}Could not post scheduled announcement to channel: {ann_err}{Style.RESET}")

            print(f"\n{Style.GREEN}Selected {len(tournament_qs)} questions. Queuing showdown (crash-safe)...{Style.RESET}")

            tracks = await asyncio.to_thread(engine.db_get_all_tracks)
            last_seq = max((v.get('display_id', 100) for v in tracks.values()), default=100)

            from src.database import db_save_tournament_queue, db_get_question_by_id, db_get_tournament_queue, db_get_active_tournament_rounds
            from src.tournament import launch_tournament_round
            import src.tournament

            async with src.tournament._LAUNCH_LOCK:
                q_ids = [q['id'] for q in tournament_qs]
                total_count = len(tournament_qs)

                active_rounds = await asyncio.to_thread(db_get_active_tournament_rounds)
                existing_queue = await asyncio.to_thread(db_get_tournament_queue)
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
                        cooldown_seconds
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
                            cooldown_seconds
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
                            cooldown_seconds
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

async def render_control_center_panel(app, engine: QuizEngine, cli: CLI):
    """Sub-panel that lists and modifies scheduled single questions and pending tournaments."""
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
                print(f"  {i+1}. [{q['id']}] {Style.WHITE}{q['question'][:45]}...{Style.RESET}\n     ├─ Topic: {q['topic']}\n     └─ Planned For: {Style.CYAN}{t_str}{Style.RESET}")
                
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

        print(f"\n{Style.CYAN}Menu Options:\n [1] Reschedule planned single question │ [2] Unschedule planned question\n [3] Postpone/Reschedule tournament    │ [4] Modify tournament parameters\n [5] Delete/Cancel scheduled tournament │ [6] Trigger question immediate publish\n [b] Back to Main Cockpit{Style.RESET}")
        
        sub_choice = await cli.ask("<b>Scheduler Command > </b>")
        if not sub_choice or sub_choice == 'b':
            break
            
        if sub_choice == "1" and sched_qs:
            idx_in = await cli.ask("<b>Select question # to Reschedule: </b>")
            if idx_in and idx_in.isdigit() and 1 <= int(idx_in) <= len(sched_qs):
                target_q = sched_qs[int(idx_in)-1]
                new_date = await cli.ask("<b>New ISO Timestamp (e.g. 2026-07-20T18:00:00+03:00): </b>")
                if new_date:
                    success = await asyncio.to_thread(db_reschedule_question, target_q['id'], new_date)
                    if success:
                        print(f"{Style.GREEN}✅ Question rescheduled successfully.{Style.RESET}")
                        await asyncio.sleep(1.5)
                        
        elif sub_choice == "2" and sched_qs:
            idx_in = await cli.ask("<b>Select question # to Cancel (removes schedule): </b>")
            if idx_in and idx_in.isdigit() and 1 <= int(idx_in) <= len(sched_qs):
                target_q = sched_qs[int(idx_in)-1]
                success = await asyncio.to_thread(db_reschedule_question, target_q['id'], None)
                if success:
                    print(f"{Style.GREEN}✅ Schedule cleared. Question reverted to manual draft.{Style.RESET}")
                    await asyncio.sleep(1.5)

        elif sub_choice == "3" and has_tourney:
            new_start = await cli.ask("<b>New Tournament Start ISO Date (or 'CLEAR' for immediate start): </b>")
            if new_start:
                val = "CLEAR" if new_start.upper() == "CLEAR" else new_start
                success = await asyncio.to_thread(db_update_tournament_schedule_params, scheduled_start=val)
                if success:
                    print(f"{Style.GREEN}✅ Tournament launch timer rescheduled successfully.{Style.RESET}")
                    await asyncio.sleep(1.5)

        elif sub_choice == "4" and has_tourney:
            dur_in = await cli.ask("<b>Modify Round Duration (seconds or Enter to skip): </b>")
            cool_in = await cli.ask("<b>Modify Cooldown Interval (seconds or Enter to skip): </b>")
            
            dur = int(dur_in) if (dur_in and dur_in.isdigit()) else None
            cool = int(cool_in) if (cool_in and cool_in.isdigit()) else None
            
            if dur is not None or cool is not None:
                success = await asyncio.to_thread(db_update_tournament_schedule_params, round_seconds=dur, cooldown_seconds=cool)
                if success:
                    print(f"{Style.GREEN}✅ Parameters updated successfully.{Style.RESET}")
                    await asyncio.sleep(1.5)

        elif sub_choice == "5" and has_tourney:
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

        elif sub_choice == "6" and sched_qs:
            idx_in = await cli.ask("<b>Select question # to publish IMMEDIATELY: </b>")
            if idx_in and idx_in.isdigit() and 1 <= int(idx_in) <= len(sched_qs):
                target_q = sched_qs[int(idx_in)-1]
                
                # Reschedule to 1 second ago to force scheduler task to ingest it
                now_minus_1s = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
                await asyncio.to_thread(db_reschedule_question, target_q['id'], now_minus_1s)
                print(f"{Style.GREEN}✅ Triggered! The background daemon will publish the question in the next loop cycle.{Style.RESET}")
                await asyncio.sleep(1.5)

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

    print(f"\n{Style.CYAN}Interruption Methods:\n [1] ⏸️  Pause Tournament (Halt active round + freeze queue)\n [2] ▶️  Resume Tournament Queue (Unfreeze execution loop)\n [3] ⏹️  Kill Tournament (Halt active round + wipe out queue)\n [b] Back to Cockpit{Style.RESET}")
    
    action = await cli.ask("<b>Emergency Action > </b>")
    if not action or action == 'b':
        return

    from src.tournament import halt_active_tournament

    if action == "1":
        # Pause: finalize active round with interruption, set is_paused = True
        reason = await cli.ask("<b>Input halt reason displayed to channel (or enter to default): </b>")
        print(f"{Style.YELLOW}Executing pause...{Style.RESET}")
        await halt_active_tournament(app, engine, clear_queue=False, halt_reason=reason)
        print(f"{Style.GREEN}✅ Tournament paused.{Style.RESET}")
        await asyncio.sleep(2.0)

    elif action == "2":
        # Resume: set is_paused = False
        print(f"{Style.YELLOW}Resuming...{Style.RESET}")
        await asyncio.to_thread(db_set_tournament_pause_state, False)
        print(f"{Style.GREEN}✅ Tournament queue resumed.{Style.RESET}")
        await asyncio.sleep(1.5)

    elif action == "3":
        # Kill: finalize active round, clear remaining queue
        confirm = await cli.ask("<b>Are you sure you want to completely kill and delete this entire tournament? (y/n): </b>")
        if confirm and confirm.lower() == 'y':
            reason = await cli.ask("<b>Input halt reason displayed to channel (or enter to default): </b>")
            print(f"{Style.RED}Terminating live tournament...{Style.RESET}")
            await halt_active_tournament(app, engine, clear_queue=True, halt_reason=reason)
            print(f"{Style.RED}✅ Live tournament deleted and wiped.{Style.RESET}")
            await asyncio.sleep(2.0)