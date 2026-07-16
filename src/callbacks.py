import httpx
from src.config import CONFIG, Style
from src.rendering import UIFactory, fetch_kroki_image
from src.database import process_user_score, db_set_user_grade
from telegram import Update, InputMediaPhoto, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, engine):
    query = update.callback_query
    data = query.data.split("|")
    action, d_id = data[0], data[1]

    print(f"\n{Style.CYAN}[CALLBACK DEBUG]{Style.RESET} Action: {action} | Ref ID: {d_id} | User ID: {query.from_user.id}")

    # 1. Handle student grade-level selection onboarding clicks (PM only)
    if action == "set_grade":
        grade = int(d_id)
        db_set_user_grade(query.from_user.id, grade)
        await query.answer(f"Grade {grade} registered!")
        await query.edit_message_text(
            f"✅ <b>Success!</b> Your profile is registered under <b>Grade {grade}</b>.\n\n"
            f"Use the /leaderboard command inside our private chat to check rankings, "
            f"and check the main channel for active quizzes!",
            parse_mode="HTML"
        )
        return

    # Check if the click occurred on a public channel post
    is_channel_post = (query.message and query.message.chat.type in ["channel", "supergroup", "group"])

    # Retrieve active question metadata from database
    tracks = engine.db_get_all_tracks()
    mid_key = next((k for k, v in tracks.items() if k.isdigit() and str(v.get('display_id')) == d_id), None)

    if not mid_key:
        print(f" {Style.RED}└─ [ERROR] No message ID tracked for Ref ID: {d_id}{Style.RESET}")
        await query.answer("This quiz session has ended.", show_alert=True)
        return

    track_status = tracks[mid_key].get('status')
    if track_status != "active":
        print(f" {Style.YELLOW}└─ [WARNING] Blocked click: Quiz status is '{track_status}' (not active).{Style.RESET}")
        await query.answer("This quiz session has ended.", show_alert=True)
        return

    engine.refresh_database()
    all_qs = {q['id']: q for subject_list in engine.db.values() for q in subject_list}
    question_data = all_qs.get(tracks[mid_key]['q_id'])

    if not question_data:
        print(f" {Style.RED}└─ [ERROR] Question ID '{tracks[mid_key]['q_id']}' not found in active database.{Style.RESET}")
        await query.answer("Error: Question data not found.")
        return

    try:
        if action == "ans":
            user_selection = int(data[2])
            user_id = query.from_user.id
            is_correct = (user_selection == question_data['correct_option'])
            
            # Record student metrics in Neon cloud database
            perf_card = process_user_score(user_id, mid_key, question_data['id'], is_correct)
            
            correct_letter = chr(65 + question_data['correct_option'])
            user_letter = chr(65 + user_selection)
            user_status = "🟩 CORRECT!" if is_correct else "🟥 INCORRECT"
            
            # Generate a condensed, highly polished mathematical Unicode summary
            poll_hint = UIFactory.generate_poll_hint(question_data)
            
            score_line = f"Total Score: {perf_card['total_marks']} Marks" if perf_card else ""
            accuracy_line = f"Accuracy: {perf_card['accuracy']}%" if perf_card else ""
            
            # Compile the private pop-up modal alert message
            alert_text = (
                f"🎯 Your Selection: {user_letter} ({user_status})\n"
                f"⭐ Correct Option: [{correct_letter}]\n\n"
                f"📝 {poll_hint}\n\n"
                f"📊 {score_line} | {accuracy_line}"
            )

            # 2. If clicked inside the channel, deliver a PRIVATE popup modal alert
            # This completely preserves the unanswered state of the main question card for everyone else!
            if is_channel_post:
                await query.answer(text=alert_text, show_alert=True)
                return
            else:
                # 3. If clicked inside PM study sessions, we can safely edit the message
                await query.answer("Revealing Solution...")
                explanation_html = UIFactory.build_answered_view(question_data, d_id, user_selection, compact=False, perf_card=perf_card)
                retry_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 TRY AGAIN", callback_data=f"reset|{d_id}")]])
                await query.edit_message_text(text=explanation_html, reply_markup=retry_kb, parse_mode="HTML", disable_web_page_preview=True)

        elif action == "reset":
            await query.answer("Resetting view...")
            if mid_key and tracks[mid_key].get("followup_mid"):
                try: 
                    await context.bot.delete_message(chat_id=query.message.chat_id, message_id=tracks[mid_key]["followup_mid"])
                except Exception: 
                    pass
                engine.db_save_track(mid_key, tracks[mid_key]["q_id"], "active", d_id, tracks[mid_key]["type"], tracks[mid_key]["msg_type"], followup_mid=None)

            img_url, caption, _ = UIFactory.create_question_assets(question_data, d_id)
            orig_kb = UIFactory.build_keyboard(question_data, d_id)

            if img_url:
                async with httpx.AsyncClient() as client:
                    resp = await fetch_kroki_image(client, img_url)
                    if resp and resp.status_code == 200:
                        media = InputMediaPhoto(media=resp.content, caption=caption, parse_mode="HTML")
                        await query.edit_message_media(media=media, reply_markup=orig_kb)
                    else:
                        await query.answer("Renderer Error: Reset failed.", show_alert=True)
            else:
                await query.edit_message_text(text=caption, reply_markup=orig_kb, parse_mode="HTML")
            print(f" {Style.GREEN}└─ [SUCCESS] Active question state restored.{Style.RESET}")

    except Exception as e:
        print(f" {Style.RED}└─ [EXCEPTION] Fatal error in callback thread: {e}{Style.RESET}")
        await query.answer("System Error: Could not render response.", show_alert=True)