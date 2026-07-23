# src/cli.py
import math
import os
import json
import asyncio
import traceback
from pathlib import Path
from src.config import CONFIG, Style
from src.database import QuizEngine
from src.rendering import UIFactory, fetch_kroki_image
from src.rendering.rich_helpers import send_rich_message_safe, edit_rich_message_safe, convert_to_legacy_html
from src.typography import lite_math
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import clear as clear_screen
from prompt_toolkit.formatted_text import HTML
import httpx
from telegram import Poll

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
        print(f" [0] 🚪 Shutdown System")

        choice = await cli.ask("<ansicyan><b>Choice > </b></ansicyan>")
        if choice in [None, "0"]: break
        if choice.lower() == 'c':
            clear_screen()
            continue

        # --- 4: AI QUESTIONS DYNAMIC DATABASE IMPORTER ---
        if choice == "4":
            print(f"\n{Style.CYAN}--- DYNAMIC DATABASE QUESTIONS IMPORTER ---{Style.RESET}")

            # 1. Automatically scan the questions/ directory recursively for any .json files
            questions_dir = Path("questions")
            json_files = []
            if questions_dir.exists():
                json_files = sorted(list(questions_dir.rglob("*.json")))

            if not json_files:
                print(f"{Style.RED}No JSON question files found inside questions/ directory.{Style.RESET}")
                continue

            print(f"📁 {Style.YELLOW}Detected Question Files:{Style.RESET}")
            for i, file_path in enumerate(json_files):
                # Cleanly display relative paths, e.g. "1. questions/mathematics/math_batch_2026_07_19.json"
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
                count = engine.db_import_questions(raw_data)
                if count > 0:
                    print(f"{Style.GREEN}✅ SUCCESS: {count} questions successfully imported/synced to Neon database.{Style.RESET}")
                else:
                    print(f"{Style.RED}❌ FAILED: No questions were imported. Check your JSON schema.{Style.RESET}")
            except Exception as e:
                print(f"{Style.RED}❌ FAILED: JSON syntax error: {e}{Style.RESET}")
            continue

        if choice in ["1", "2"]:
            db = engine.refresh_database()
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
            to_send = []

            if ":" in range_in:
                query_parts = [p.strip().split(":") for p in range_in.split(",")]
                requested = {part[0].lower().strip(): int(part[1].strip()) for part in query_parts if len(part) == 2 and part[1].strip().isdigit()}
                pools = {"easy": [], "medium": [], "hard": []}
                for q in target_list:
                    d = "easy" if q.get("difficulty", "medium").lower() == "weak" else q.get("difficulty", "medium").lower()
                    if d in pools: pools[d].append(q)
                for diff, count in requested.items():
                    if diff in pools: to_send.extend(pools[diff][:count])
            else:
                indices = []
                try:
                    for part in range_in.replace(' ', '').split(','):
                        if '-' in part:
                            start, end = map(int, part.split('-'))
                            indices.extend(range(start-1, end))
                        else:
                            indices.append(int(part)-1)
                except:
                    print(f"{Style.RED}Invalid format.{Style.RESET}")
                    continue
                for idx in indices:
                    if 0 <= idx < len(target_list): to_send.append(target_list[idx])

            tracks = engine.db_get_all_tracks()
            last_seq = tracks.get("last_seq", 100)
            if tracks:
                last_seq = max(v.get('display_id', 100) for v in tracks.values())

            for q in to_send:
                last_seq += 1
                try:
                    if choice == "1":
                        # Check for dedicated native question and options overrides, falling back to lite_math
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
                        
                        media_bytes = None
                        if img_url:
                            async with httpx.AsyncClient() as client:
                                resp = await fetch_kroki_image(client, img_url)
                                if resp and resp.status_code == 200:
                                    media_bytes = resp.content
                                else:
                                    raise Exception("Kroki rendering failure.")
                        
                        m = await send_rich_message_safe(app.bot, chat_id=engine.config['channel'], html_content=caption, reply_markup=kb, media_bytes=media_bytes)
                        msg_type = "photo" if img_url else "text"

                    # Save state dynamically to PostgreSQL Neon table
                    engine.db_save_track(m.message_id, q['id'], "active", last_seq, "premium", msg_type)
                    print(f"{Style.GREEN}✅ Sent REF: {last_seq} [{msg_type}]{Style.RESET}")
                except Exception as e:
                    traceback.print_exc()
                    print(f"{Style.RED}❌ Failed REF: {last_seq} | {e}{Style.RESET}")

            # Save fallback sequence
            local_sent_tracks = engine.load_json("logs/sent_tracks.json")
            local_sent_tracks["last_seq"] = last_seq
            engine.save_json("logs/sent_tracks.json", local_sent_tracks)

        elif choice == "3":
            while True:
                engine.refresh_database()
                all_qs = {q['id']: q for sub_list in engine.db.values() for q in sub_list}
                tracks = engine.db_get_all_tracks()

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
                print(f"{Style.CYAN}Nav: [n] Next | [p] Prev | [sw] Status | [ft] Filter\nAction: [Index], [ref#], [all] | [clean] Live Sync | [b] Back{Style.RESET}")

                cmd = await cli.ask("<b>Command > </b>")
                if not cmd or cmd == 'b': break
                if cmd == 'n': page += 1; continue
                if cmd == 'p': page -= 1; continue
                if cmd == 'sw':
                    curr_stat = "closed" if curr_stat == "active" else "active"
                    page = 0; continue
                if cmd == 'ft':
                    f_val = await cli.ask("<b>Filter [nap/prp/bop]: </b>")
                    if f_val in ['nap', 'prp', 'bop']:
                        curr_type = f_val
                        page = 0; continue

                if cmd == 'clean':
                    print(f"{Style.YELLOW}Syncing with Telegram...{Style.RESET}")
                    modified = False
                    for mid, v in list(tracks.items()):
                        if mid.isdigit() and v.get("status") != "deleted":
                            try: await app.bot.forward_message(bot_info.id, engine.config['channel'], int(mid))
                            except Exception:
                                # Update Postgres directly
                                engine.db_update_track_status(mid, "deleted")
                                modified = True
                    continue

                targets = [items[int(part)-1] for part in cmd.split(',') if part.strip().isdigit() and 0 <= int(part)-1 < len(items)] if cmd.lower() != 'all' else [m for m in items[page*10 : (page+1)*10] if tracks[m].get('type') != 'native']

                for mid in set(targets):
                    v = tracks[mid]
                    q = all_qs.get(v['q_id'])
                    ref = v.get('display_id', mid)
                    try:
                        if curr_stat == "active":
                            if "followup_mid" in v:
                                try: await app.bot.delete_message(engine.config['channel'], int(v["followup_mid"]))
                                except Exception: pass
                                del v["followup_mid"]
                            if v.get('type') == 'native':
                                await app.bot.stop_poll(engine.config['channel'], int(mid))
                            else:
                                is_photo = (v.get('msg_type') == "photo")
                                if is_photo:
                                    print(f" {Style.CYAN}├─ [CLOSE] Rendering widescreen Solution Sheet graphic for REF: {ref}...{Style.RESET}")
                                    sol_latex = UIFactory.build_widescreen_solution_latex(q, ref)
                                    sol_img_url = UIFactory.get_latex_url(sol_latex)
                                    async with httpx.AsyncClient() as client:
                                        resp = await fetch_kroki_image(client, img_url, sol_latex)
                                        if resp and resp.status_code == 200:
                                            # Passes the continuation parameter to generate a completely distinct, un-duplicated derivation sheet
                                            closed_view = UIFactory.build_closed_static_view(q, ref, compact=True)
                                            media = InputMediaPhoto(media=resp.content, caption=convert_to_legacy_html(closed_view), parse_mode="HTML")
                                            await app.bot.edit_message_media(chat_id=engine.config['channel'], message_id=int(mid), media=media, reply_markup=None)
                                            full_text = UIFactory.build_closed_static_view(q, ref, compact=False, continuation=True)
                                            if len(full_text) > len(media.caption):
                                                follow_up = await app.bot.send_message(chat_id=engine.config['channel'], text=full_text, parse_mode="HTML", disable_web_page_preview=True, reply_to_message_id=int(mid))
                                                engine.db_save_track(mid, v["q_id"], "closed", ref, v["type"], v["msg_type"], followup_mid=follow_up.message_id)
                                        else:
                                            await app.bot.edit_message_caption(chat_id=engine.config['channel'], message_id=int(mid), caption=convert_to_legacy_html(UIFactory.build_closed_static_view(q, ref, compact=True)), parse_mode="HTML", reply_markup=None)
                                            engine.db_update_track_status(mid, "closed", followup_mid=None)
                                else:
                                    await edit_rich_message_safe(app.bot, chat_id=engine.config['channel'], message_id=int(mid), html_content=UIFactory.build_closed_static_view(q, ref, compact=False), reply_markup=None)
                                    engine.db_update_track_status(mid, "closed", followup_mid=None)
                            engine.db_update_track_status(mid, "closed")
                        else:
                            if v.get('type') == 'native': continue
                            img_url, cap = UIFactory.create_question_assets(q, ref)
                            kb = UIFactory.build_keyboard(q, ref)
                            if v.get('msg_type') == "photo":
                                async with httpx.AsyncClient() as client:
                                    # If has diagram, compile the vector TikZ image cleanly
                                    media_bytes = None
                                    if img_url:
                                        question_block = UIFactory.build_question_text_block(q, ref)
                                        figure_block = UIFactory.build_figure_block(q, add_strut=True)
                                        options_block = UIFactory.build_options_block(q)
                                        compiled_latex = UIFactory.assemble_layout(UIFactory.WATERMARK, question_block, figure_block, options_block)
                                        img_url_kroki = UIFactory.get_latex_url(compiled_latex)
                                        resp = await fetch_kroki_image(client, img_url_kroki, compiled_latex)
                                        if resp and resp.status_code == 200:
                                            media_bytes = resp.content
                                    
                                    if media_bytes:
                                        media = InputMediaPhoto(media=media_bytes, caption=convert_to_legacy_html(cap), parse_mode="HTML")
                                        await app.bot.edit_message_media(chat_id=engine.config['channel'], message_id=int(mid), media=media, reply_markup=kb)
                            else:
                                await edit_rich_message_safe(app.bot, chat_id=engine.config['channel'], message_id=int(mid), html_content=cap, reply_markup=kb)
                            engine.db_update_track_status(mid, "active")
                    except Exception as e:
                        traceback.print_exc()
                        print(f"Error processing REF:{ref} | {e}")
                await asyncio.sleep(0.5)
