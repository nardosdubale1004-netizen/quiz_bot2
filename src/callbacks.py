# src/callbacks.py
import asyncio
import traceback
import httpx
import io
from src.config import CONFIG, Style, LOCKOUT_MESSAGES
from src.rendering import UIFactory, fetch_kroki_image
from src.rendering.rich_helpers import send_rich_message_safe, edit_rich_message_safe, convert_to_legacy_html
from src.database import (
    process_user_score,
    db_set_user_grade,
    db_update_private_message_id,
    db_update_response_view_state,
    db_get_user_response,
    db_get_track_and_question,
    db_get_cached_file_id,
    db_save_cached_file_id
)
from telegram import Update, InputMediaPhoto, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

def check_message_has_lockout(user_id, message) -> bool:
    """
    Safely determines if the message currently contains the lockout warning notice.
    Checks both the in-memory shared set and the case-insensitive backup string.
    """
    if not message:
        return False
    if (user_id, message.message_id) in LOCKOUT_MESSAGES:
        return True
    current_text = message.caption or message.text or ""
    current_text_lower = current_text.lower()
    return any(kw in current_text_lower for kw in ["lockout active", "already answered", "securely locked"])

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, engine):
    query = update.callback_query
    data = query.data.split("|")
    action, d_id = data[0], data[1]

    print(f"\n{Style.CYAN}[CALLBACK DEBUG]{Style.RESET} Action: {action} | Ref ID: {d_id} | User ID: {query.from_user.id}")

    if action == "set_grade":
        grade = int(d_id)
        await asyncio.to_thread(db_set_user_grade, query.from_user.id, grade)
        await query.answer(f"Grade {grade} registered!")
        await query.edit_message_text(
            f"✅ <b>Success!</b> Your profile is registered under <b>Grade {grade}</b>.\n\n"
            f"Use the /leaderboard command inside our private chat to check rankings, "
            f"and check the main channel for active quizzes!",
            parse_mode="HTML"
        )
        return

    track, question_data = await asyncio.to_thread(db_get_track_and_question, int(d_id))

    if not track or not question_data:
        print(f" {Style.RED}└─ [ERROR] No track record located for Ref ID: {d_id}{Style.RESET}")
        await query.answer("This quiz session has ended.", show_alert=True)
        return

    track_status = track.get('status')
    if track_status != "active":
        print(f" {Style.YELLOW}└─ [WARNING] Blocked click: Quiz status is '{track_status}' (not active).{Style.RESET}")
        await query.answer("This quiz session has ended.", show_alert=True)
        return

    mid_key = track['message_id']
    warning_notice = "⚠️ <b>Lockout active: You have already answered this question!</b>\n" \
                     "<i>Your original selection and score have been securely locked.</i>\n\n"

    try:
        if action == "ans":
            user_selection = int(data[2])
            print(f" {Style.CYAN}├─ [DEBUG] Generating Answer Summary Sheet for REF: {d_id}{Style.RESET}")
            await query.answer("Generating Answer Sheet...")

            user_id = query.from_user.id
            is_correct = (user_selection == question_data['correct_option'])
            perf_card = await asyncio.to_thread(process_user_score, user_id, mid_key, question_data['id'], is_correct, user_selection, None, True, False)

            active_is_photo = (track.get('msg_type') == "photo")
            explanation_html = UIFactory.build_answered_view(question_data, d_id, user_selection, show_derivation=True, show_perf=False, perf_card=perf_card)
            retry_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 TRY AGAIN", callback_data=f"reset|{d_id}")]])

            has_lockout = check_message_has_lockout(user_id, query.message)

            if has_lockout:
                explanation_html = warning_notice + explanation_html

            if active_is_photo:
                cache_key = f"q:{question_data['id']}:exp:{user_selection}"
                cached_file_id = await asyncio.to_thread(db_get_cached_file_id, cache_key)

                photo_kb = UIFactory.build_answered_keyboard(d_id, user_selection, True, False, is_photo=True, message_id=track['message_id'])
                legacy_caption = convert_to_legacy_html(explanation_html)

                if cached_file_id:
                    print(f" {Style.GREEN}├─ [CACHE HIT] Reusing cached file ID for solution image. Bypassing Kroki...{Style.RESET}")
                    media = InputMediaPhoto(media=cached_file_id, caption=legacy_caption, parse_mode="HTML")
                    await query.edit_message_media(media=media, reply_markup=photo_kb)
                else:
                    print(f" {Style.YELLOW}├─ [CACHE MISS] Solution sheet not cached. Compiling via Kroki...{Style.RESET}")
                    latex_code, _ = UIFactory.create_explanation_assets(question_data, user_selection, d_id)
                    if latex_code:
                        img_url = UIFactory.get_latex_url(latex_code)
                        async with httpx.AsyncClient() as client:
                            resp = await fetch_kroki_image(client, img_url, latex_code)
                            if resp and resp.status_code == 200:
                                print(f" {Style.GREEN}├─ [SUCCESS] Solution Sheet compiled. Swapping active image and storing in cache...{Style.RESET}")
                                media = InputMediaPhoto(media=io.BytesIO(resp.content), caption=legacy_caption, parse_mode="HTML")
                                m = await query.edit_message_media(media=media, reply_markup=photo_kb)
                                if m and m.photo:
                                    await asyncio.to_thread(db_save_cached_file_id, cache_key, m.photo[-1].file_id)
                            else:
                                await query.edit_message_caption(caption=legacy_caption, reply_markup=retry_kb, parse_mode="HTML")
                    else:
                        await query.edit_message_caption(caption=legacy_caption, reply_markup=retry_kb, parse_mode="HTML")

                full_text = UIFactory.build_answered_view(question_data, d_id, user_selection, show_derivation=True, show_perf=False, perf_card=perf_card, continuation=True)
                if has_lockout:
                    full_text = warning_notice + full_text

                follow_up = await send_rich_message_safe(
                    context.bot,
                    chat_id=query.message.chat_id,
                    html_content=full_text,
                    reply_to_message_id=query.message.message_id
                )
                await asyncio.to_thread(engine.db_save_track, mid_key, track["q_id"], "active", d_id, track["type"], track["msg_type"], followup_mid=follow_up.message_id)
                return
            else:
                reveal_kb = UIFactory.build_answered_keyboard(d_id, user_selection, True, False, is_photo=False, message_id=track['message_id'])
                await edit_rich_message_safe(
                    context.bot,
                    chat_id=query.message.chat_id,
                    message_id=query.message.message_id,
                    html_content=explanation_html,
                    reply_markup=reveal_kb
                )
            return

        elif action == "toggle":
            user_selection = int(data[2])
            show_derivation = (int(data[3]) == 1)
            show_perf = (int(data[4]) == 1)
            user_id = query.from_user.id
            await query.answer("Updating View...")

            is_correct_ans = (user_selection == question_data['correct_option'])

            state_task = asyncio.to_thread(db_update_response_view_state, user_id, mid_key, show_derivation, show_perf)
            score_task = asyncio.to_thread(process_user_score, user_id, mid_key, question_data['id'], is_correct_ans, user_selection)

            _, perf_card = await asyncio.gather(state_task, score_task)

            explanation_html = UIFactory.build_answered_view(
                question_data,
                d_id,
                user_selection,
                show_derivation=show_derivation,
                show_perf=show_perf,
                perf_card=perf_card
            )

            has_lockout = check_message_has_lockout(user_id, query.message)

            if has_lockout:
                explanation_html = warning_notice + explanation_html

            kb = UIFactory.build_answered_keyboard(d_id, user_selection, show_derivation, show_perf, is_photo=False, message_id=track['message_id'])

            await edit_rich_message_safe(
                context.bot,
                chat_id=query.message.chat_id,
                message_id=query.message.message_id,
                html_content=explanation_html,
                reply_markup=kb
            )
            return

        elif action == "toggle_photo":
            user_selection = int(data[2])
            show_derivation = (int(data[3]) == 1)
            show_perf = (int(data[4]) == 1)
            user_id = query.from_user.id
            await query.answer("Updating Solution Card...")

            is_correct_ans = (user_selection == question_data['correct_option'])

            state_task = asyncio.to_thread(db_update_response_view_state, user_id, mid_key, show_derivation, show_perf)
            score_task = asyncio.to_thread(process_user_score, user_id, mid_key, question_data['id'], is_correct_ans, user_selection)

            _, perf_card = await asyncio.gather(state_task, score_task)

            kb = UIFactory.build_answered_keyboard(d_id, user_selection, show_derivation, show_perf, is_photo=True, message_id=track['message_id'])
            await query.message.edit_reply_markup(reply_markup=kb)

            if not show_derivation and not show_perf:
                if mid_key and track.get("followup_mid"):
                    try:
                        await context.bot.delete_message(chat_id=query.message.chat_id, message_id=track["followup_mid"])
                    except Exception:
                        pass
                    await asyncio.to_thread(engine.db_save_track, mid_key, track["q_id"], "active", d_id, track["type"], track["msg_type"], followup_mid=None)
            else:
                full_text = UIFactory.build_answered_view(
                    question_data,
                    d_id,
                    user_selection,
                    show_derivation=show_derivation,
                    show_perf=show_perf,
                    perf_card=perf_card,
                    continuation=True
                )

                has_lockout = check_message_has_lockout(user_id, query.message)

                if has_lockout:
                    full_text = warning_notice + full_text

                if mid_key and track.get("followup_mid"):
                    try:
                        await edit_rich_message_safe(
                            context.bot,
                            chat_id=query.message.chat_id,
                            message_id=track["followup_mid"],
                            html_content=full_text,
                            reply_markup=None
                        )
                    except Exception:
                        follow_up = await send_rich_message_safe(
                            context.bot,
                            chat_id=query.message.chat_id,
                            html_content=full_text,
                            reply_to_message_id=query.message.message_id
                        )
                        await asyncio.to_thread(engine.db_save_track, mid_key, track["q_id"], "active", d_id, track["type"], track["msg_type"], followup_mid=follow_up.message_id)
                else:
                    follow_up = await send_rich_message_safe(
                        context.bot,
                        chat_id=query.message.chat_id,
                        html_content=full_text,
                        reply_to_message_id=query.message.message_id
                    )
                    await asyncio.to_thread(engine.db_save_track, mid_key, track["q_id"], "active", d_id, track["type"], track["msg_type"], followup_mid=follow_up.message_id)
            return

        elif action == "reset":
            await query.answer("Resetting view...")
            if mid_key and track.get("followup_mid"):
                try:
                    await context.bot.delete_message(chat_id=query.message.chat_id, message_id=track["followup_mid"])
                except Exception:
                    pass
                await asyncio.to_thread(engine.db_save_track, mid_key, track["q_id"], "active", d_id, track["type"], track["msg_type"], followup_mid=None)

            img_url, caption = UIFactory.create_question_assets(question_data, d_id)
            orig_kb = UIFactory.build_keyboard(question_data, d_id)

            if img_url:
                cache_key = f"q:{question_data['id']}:diagram"
                cached_file_id = await asyncio.to_thread(db_get_cached_file_id, cache_key)
                legacy_caption = convert_to_legacy_html(caption)

                if cached_file_id:
                    print(f" {Style.GREEN}├─ [CACHE HIT] Reusing cached file ID for resetting diagram view...{Style.RESET}")
                    media = InputMediaPhoto(media=cached_file_id, caption=legacy_caption, parse_mode="HTML")
                    await query.edit_message_media(media=media, reply_markup=orig_kb)
                else:
                    print(f" {Style.YELLOW}├─ [CACHE MISS] Compiling reset diagram view...{Style.RESET}")
                    question_block = UIFactory.build_question_text_block(question_data, d_id)
                    figure_block = UIFactory.build_figure_block(question_data, add_strut=True)
                    options_block = UIFactory.build_options_block(question_data)

                    compiled_latex = UIFactory.assemble_layout(UIFactory.WATERMARK, question_block, figure_block, options_block, display_id=d_id)
                    img_url_kroki = UIFactory.get_latex_url(compiled_latex)
                    async with httpx.AsyncClient() as client:
                        resp = await fetch_kroki_image(client, img_url_kroki, compiled_latex)
                        if resp and resp.status_code == 200:
                            media = InputMediaPhoto(media=io.BytesIO(resp.content), caption=legacy_caption, parse_mode="HTML")
                            m = await query.edit_message_media(media=media, reply_markup=orig_kb)
                            if m and m.photo:
                                await asyncio.to_thread(db_save_cached_file_id, cache_key, m.photo[-1].file_id)
                        else:
                            await query.answer("Renderer Error: Reset failed.", show_alert=True)
            else:
                await edit_rich_message_safe(
                    context.bot,
                    chat_id=query.message.chat_id,
                    message_id=query.message.message_id,
                    html_content=caption,
                    reply_markup=orig_kb
                )
            print(f" {Style.GREEN}└─ [SUCCESS] Active question state restored.{Style.RESET}")

    except Exception as e:
        traceback.print_exc()
        print(f" {Style.RED}└─ [EXCEPTION] Fatal error in callback thread: {e}{Style.RESET}")
        await query.answer("System Error: Could not render response.", show_alert=True)