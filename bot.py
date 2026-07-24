# bot.py
import os
import sys
import json
import asyncio
import threading
import traceback
import io
from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from src.config import CONFIG, Style
from src.database import (
    QuizEngine,
    db_get_user_profile,
    db_get_user_response,
    db_update_private_message_id,
    db_get_weekly_leaderboard,
    db_get_pending_scheduled_question,
    db_mark_question_as_sent,
    process_user_score,
    db_update_response_view_state
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
    q = await asyncio.to_thread(db_get_pending_scheduled_question)
    if not q:
        return

    print(f"{Style.YELLOW}[SCHEDULER] Found pending scheduled question REF: {q['id']}. Publishing...{Style.RESET}", flush=True)
    channel = CONFIG.get("channel")

    tracks = await asyncio.to_thread(engine.db_get_all_tracks)
    last_seq = max((v.get('display_id', 100) for v in tracks.values()), default=100) + 1

    try:
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

            media_bytes = None
            if img_url:
                async with httpx.AsyncClient() as client:
                    resp = await fetch_kroki_image(client, img_url)
                    if resp and resp.status_code == 200:
                        media_bytes = resp.content
                    else:
                        raise Exception("Kroki failed to compile scheduled asset.")

            m = await send_rich_message_safe(app.bot, chat_id=channel, html_content=caption, reply_markup=kb, media_bytes=media_bytes)
            msg_type = "photo" if img_url else "text"
            type_str = "premium"

        await asyncio.to_thread(engine.db_save_track, m.message_id, q['id'], "active", last_seq, type_str, msg_type)
        await asyncio.to_thread(db_mark_question_as_sent, q['id'])
        print(f"{Style.GREEN}[SCHEDULER] Successfully posted scheduled quiz REF: {last_seq} to channel.{Style.RESET}", flush=True)
    except Exception as e:
        traceback.print_exc()
        print(f"{Style.RED}[SCHEDULER ERROR] Failed to post scheduled question {q['id']}: {e}{Style.RESET}", flush=True)

async def start_command(update: Update, context):
    user_id = update.effective_user.id
    args = context.args

    channel_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📣 RETURN TO CHANNEL", url="https://t.me/grade12EntranceExam")
    ]])

    # --- DEEP-LINKED QUIZ ANSWER PROCESSING ---
    if args and args[0].startswith("ans_"):
        payload = args[0]
        try:
            _, ref_id, choice_idx_str = payload.split("_")
            display_id = int(ref_id)
            user_selection = int(choice_idx_str)

            tracks = await asyncio.to_thread(engine.db_get_all_tracks)
            mid_key = next((k for k, v in tracks.items() if k.isdigit() and int(v.get('display_id')) == display_id), None)

            if not mid_key:
                await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content="⚠️ This quiz session has ended or the reference was not found.", reply_markup=channel_kb)
                return

            await asyncio.to_thread(engine.refresh_database)
            all_qs = {q['id']: q for subject_list in engine.db.values() for q in subject_list}
            question_data = all_qs.get(tracks[mid_key]['q_id'])

            if not question_data:
                await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content="Error: Question data not found.", reply_markup=channel_kb)
                return

            existing_response = await asyncio.to_thread(db_get_user_response, user_id, mid_key)

            if existing_response:
                original_selection = existing_response['selected_option']
                old_private_mid = existing_response.get('private_message_id')

                # Load expansion states directly from the database row dynamically
                show_derivation = existing_response.get('show_derivation', False)
                show_perf = existing_response.get('show_perf', False)

                try:
                    await context.bot.delete_message(chat_id=update.message.chat_id, message_id=update.message.message_id)
                except Exception:
                    pass

                if old_private_mid:
                    try:
                        await context.bot.delete_message(chat_id=update.message.chat_id, message_id=old_private_mid)
                        await context.bot.delete_message(chat_id=update.message.chat_id, message_id=old_private_mid + 1)
                    except Exception:
                        pass

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
                    latex_code, _ = UIFactory.create_explanation_assets(question_data, original_selection, display_id)
                    if latex_code:
                        img_url = UIFactory.get_latex_url(latex_code)
                        async with httpx.AsyncClient() as client:
                            resp = await fetch_kroki_image(client, img_url, latex_code)
                            if resp and resp.status_code == 200:
                                legacy_caption = convert_to_legacy_html(explanation_html_compact)
                                photo_kb = UIFactory.build_answered_keyboard(display_id, original_selection, show_derivation=show_derivation, show_perf=show_perf, is_photo=True)
                                m = await context.bot.send_photo(chat_id=update.message.chat_id, photo=io.BytesIO(resp.content), caption=legacy_caption, parse_mode="HTML", reply_markup=photo_kb)
                                await asyncio.to_thread(db_update_private_message_id, user_id, mid_key, m.message_id)

                                # Restore expanded followups immediately on reload
                                if show_derivation or show_perf:
                                    full_text = UIFactory.build_answered_view(
                                        question_data, str(display_id), original_selection, show_derivation=show_derivation, show_perf=show_perf, perf_card=perf_card, continuation=True
                                    )
                                    follow_up = await send_rich_message_safe(
                                        context.bot,
                                        chat_id=update.message.chat_id,
                                        html_content=full_text,
                                        reply_to_message_id=m.message_id
                                    )
                                    await asyncio.to_thread(engine.db_save_track, mid_key, tracks[mid_key]["q_id"], "active", display_id, tracks[mid_key]["type"], tracks[mid_key]["msg_type"], followup_mid=follow_up.message_id)
                                return

                reveal_kb = UIFactory.build_answered_keyboard(display_id, original_selection, show_derivation=show_derivation, show_perf=show_perf, is_photo=False)
                f_m = await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content=explanation_html, reply_markup=reveal_kb)
                await asyncio.to_thread(db_update_private_message_id, user_id, mid_key, f_m.message_id)
                return

            is_correct = (user_selection == question_data['correct_option'])
            # Initial answer from channel starts in compact mode (False, False)
            perf_card = await asyncio.to_thread(process_user_score, user_id, mid_key, question_data['id'], is_correct, user_selection, None, False, False)

            explanation_html = UIFactory.build_answered_view(
                question_data, str(display_id), user_selection, show_derivation=False, show_perf=False, perf_card=perf_card
            )

            has_ex_diag = UIFactory.has_explanation_diagram(question_data)
            if has_ex_diag:
                explanation_html_compact = UIFactory.build_answered_view(
                    question_data, str(display_id), user_selection, show_derivation=False, show_perf=False, perf_card=perf_card
                )
                latex_code, _ = UIFactory.create_explanation_assets(question_data, user_selection, display_id)
                if latex_code:
                    img_url = UIFactory.get_latex_url(latex_code)
                    async with httpx.AsyncClient() as client:
                        resp = await fetch_kroki_image(client, img_url, latex_code)
                        if resp and resp.status_code == 200:
                            legacy_caption = convert_to_legacy_html(explanation_html_compact)
                            photo_kb = UIFactory.build_answered_keyboard(display_id, user_selection, show_derivation=False, show_perf=False, is_photo=True)
                            m = await context.bot.send_photo(chat_id=update.message.chat_id, photo=io.BytesIO(resp.content), caption=legacy_caption, parse_mode="HTML", reply_markup=photo_kb)
                            await asyncio.to_thread(db_update_private_message_id, user_id, mid_key, m.message_id)
                            return

            reveal_kb = UIFactory.build_answered_keyboard(display_id, user_selection, show_derivation=False, show_perf=False, is_photo=False)
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

        await send_rich_message_safe(
            context.bot,
            chat_id=update.message.chat_id,
            html_content=(
                f"👋 <b>Welcome Back, Scholar!</b>\n\n"
                f"Your academic profile is active and fully synchronized.\n\n"
                f"📊 <b>YOUR STUDY METRICS:</b>\n"
                f"├─ Registered Level: <b>Grade {grade}</b>\n"
                f"├─ Practice Score:  <b>{user_marks} Marks</b>\n"
                f"├─ Mastery Level:   <b>{mastery}</b>\n"
                f"└─ Accuracy:        <b>{accuracy}%</b> ({profile['correct']} of {profile['total']} questions solved correctly)\n\n"
                f"💬 <b>STUDY CHANNELS:</b>\n"
                f"• Check the main channel for active scheduled questions!\n"
                f"• Use the /leaderboard command here to view your rank standings!"
            ),
            reply_markup=channel_kb
        )
        return

    keyboard = [
        [InlineKeyboardButton("🎒 Grade 6", callback_data="set_grade|6"),
         InlineKeyboardButton("🎒 Grade 8", callback_data="set_grade|8")],
        [InlineKeyboardButton("🎒 Grade 10", callback_data="set_grade|10"),
         InlineKeyboardButton("🎒 Grade 12", callback_data="set_grade|12")],
        [InlineKeyboardButton("📢 VISIT CHANNEL", url="https://t.me/grade12EntranceExam")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await send_rich_message_safe(
        context.bot,
        chat_id=update.message.chat_id,
        html_content=(
            "👋 <b>Welcome to Quiz Master Pro!</b>\n\n"
            "To customize your study experience, unlock early bird rewards, and compare "
            "scores inside fair rank tables, select your academic grade level below:"
        ),
        reply_markup=reply_markup
    )

async def leaderboard_command(update: Update, context):
    user_id = update.effective_user.id
    profile = await asyncio.to_thread(db_get_user_profile, user_id)

    if not profile:
        await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content="⚠️ Please register your grade first by typing /start.")
        return

    grade = profile['grade']
    user_marks = profile['total_marks']
    mastery = get_grade_mastery_title(user_marks)

    weekly_top = await asyncio.to_thread(db_get_weekly_leaderboard, grade)

    channel_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📣 RETURN TO CHANNEL", url="https://t.me/grade12EntranceExam")
    ]])

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

        # Clean up any active webhook conflict from cloud deployments before polling
        print("Clearing active webhook to prevent polling conflict...", flush=True)
        loop.run_until_complete(app.bot.delete_webhook(drop_pending_updates=True))

        # Start background polling updater so students can receive explanation cards
        loop.run_until_complete(app.updater.start_polling())

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
            import time
            while True:
                time.sleep(3600)

if __name__ == "__main__":
    main()