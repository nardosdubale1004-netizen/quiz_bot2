import traceback
import httpx
from src.config import CONFIG, Style
from src.rendering import UIFactory, fetch_kroki_image
from src.database import process_user_score, db_set_user_grade, db_update_private_message_id
from telegram import Update, InputMediaPhoto, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, engine):
    query = update.callback_query
    data = query.data.split("|")
    action, d_id = data[0], data[1]

    print(f"\n{Style.CYAN}[CALLBACK DEBUG]{Style.RESET} Action: {action} | Ref ID: {d_id} | User ID: {query.from_user.id}")

    # Handle grade onboarding registration
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

    # Check database for active sent questions mapping
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
            print(f" {Style.CYAN}├─ [DEBUG] Generating Answer Summary Sheet for REF: {d_id}{Style.RESET}")
            await query.answer("Generating Answer Sheet...")

            # Process score metrics dynamically inside Neon database
            user_id = query.from_user.id
            is_correct = (user_selection == question_data['correct_option'])
            perf_card = process_user_score(user_id, query.message.message_id, question_data['id'], is_correct, user_selection)

            active_is_photo = (tracks[mid_key].get('msg_type') == "photo")
            explanation_html = UIFactory.build_answered_view(question_data, d_id, user_selection, compact=active_is_photo, perf_card=perf_card)
            retry_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 TRY AGAIN", callback_data=f"reset|{d_id}")]])

            if active_is_photo:
                print(f" {Style.CYAN}├─ [DEBUG] Question has diagram. Compiling widescreen Solution Sheet graphic...{Style.RESET}")
                latex_code, _ = UIFactory.create_explanation_assets(question_data, user_selection, d_id)
                if latex_code:
                    img_url = UIFactory.get_latex_url(latex_code)
                    async with httpx.AsyncClient() as client:
                        resp = await fetch_kroki_image(client, img_url, latex_code)
                        if resp and resp.status_code == 200:
                            print(f" {Style.GREEN}├─ [SUCCESS] Solution Sheet compiled successfully. Swapping active image...{Style.RESET}")
                            media = InputMediaPhoto(media=resp.content, caption=explanation_html, parse_mode="HTML")
                            await query.edit_message_media(media=media, reply_markup=retry_kb)
                            
                            # Passes the continuation parameter to generate a completely distinct, un-duplicated derivation sheet
                            full_explanation_text = UIFactory.build_answered_view(
                                question_data, d_id, user_selection, compact=False, perf_card=perf_card, continuation=True
                            )
                            if len(full_explanation_text) > len(explanation_html):
                                if "followup_mid" in tracks[mid_key]:
                                    try: await context.bot.delete_message(chat_id=query.message.chat_id, message_id=tracks[mid_key]["followup_mid"])
                                    except Exception: pass
                                
                                follow_up = await context.bot.send_message(
                                    chat_id=query.message.chat_id,
                                    text=full_explanation_text,
                                    parse_mode="HTML",
                                    disable_web_page_preview=True,
                                    reply_to_message_id=query.message.message_id
                                )
                                # Update database tracking
                                engine.db_save_track(mid_key, tracks[mid_key]["q_id"], "active", d_id, tracks[mid_key]["type"], tracks[mid_key]["msg_type"], followup_mid=follow_up.message_id)
                        else:
                            await query.edit_message_caption(caption=explanation_html, reply_markup=retry_kb, parse_mode="HTML")
                else:
                    await query.edit_message_caption(caption=explanation_html, reply_markup=retry_kb, parse_mode="HTML")
            else:
                await query.edit_message_text(text=explanation_html, reply_markup=retry_kb, parse_mode="HTML", disable_web_page_preview=True)

        elif action == "reset":
            await query.answer("Resetting view...")
            if mid_key and tracks[mid_key].get("followup_mid"):
                try: 
                    await context.bot.delete_message(chat_id=query.message.chat_id, message_id=tracks[mid_key]["followup_mid"])
                except Exception: 
                    pass
                # Update database tracking
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
        traceback.print_exc()
        print(f" {Style.RED}└─ [EXCEPTION] Fatal error in callback thread: {e}{Style.RESET}")
        await query.answer("System Error: Could not render response.", show_alert=True)