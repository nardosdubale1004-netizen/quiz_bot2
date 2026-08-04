# src/callbacks.py
import asyncio
import traceback
import httpx
import io
from src.config import CONFIG, Style, LOCKOUT_MESSAGES, USER_STATES, USER_PAYLOADS, ADMIN_IDS, FEEDBACK_CATEGORIES, FEEDBACK_STATUS_LABELS, FSM_INPUT_HINT
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
    db_save_cached_file_id,
    db_get_user_profile,
    db_get_user_subject_marks,
    db_update_user_consent_state,
    db_leave_organization,
    db_join_organization,
    db_join_organization_by_id,
    db_create_organization,
    db_get_organization_roster,
    db_get_user_organizations,
    db_dissolve_organization,
    db_set_user_nickname,
    db_get_feedback_by_id,
    db_update_feedback_status,
    db_get_feedback_list,
    db_get_admin_dashboard_stats,
    db_get_recent_users,
    db_call_guarded,
    db_get_user_feedback_list,
    db_count_user_feedback,
    db_add_feedback_message,
    db_get_feedback_thread,
    db_get_question_by_id,
    db_get_latest_track_for_question,
    db_get_alliance_leaderboard,
    db_get_city_leaderboard,
    db_get_country_leaderboard,
    db_approve_member_request,
    db_get_org_membership_log,
    db_get_weekly_leaderboard,
)
from src.rendering.html_views import (
    build_profile_card_text,
    build_alliance_info_text,
    build_organization_card_text,
    build_help_menu_text,
    build_feedback_menu_text,
    build_help_menu_keyboard,
    build_help_topic_text,
    build_help_topic_keyboard,
    build_feedback_item_text,
    build_admin_dashboard_text,
    build_user_directory_text,
    build_user_feedback_list_text,
    build_feedback_thread_text,
    build_profile_main_keyboard,
    build_profile_settings_keyboard,
    build_leaderboard_text,
    build_leaderboard_keyboard,
)
from src.rendering.html_views import build_profile_card_text, build_alliance_info_text
from telegram import Update, InputMediaPhoto, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

async def _delayed_delete(bot, chat_id, message_id, delay_seconds: int = 10800):
    """Deletes a status-update DM after a delay — the student still sees it
    when it arrives, but it clears out of the chat afterward. The record
    itself is untouched and always stays viewable in /myfeedback."""
    try:
        await asyncio.sleep(delay_seconds)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


def check_message_has_lockout(user_id, message) -> bool:
    if not message:
        return False
    if (user_id, message.message_id) in LOCKOUT_MESSAGES:
        return True
    current_text = message.caption or message.text or ""
    current_text_lower = current_text.lower()
    return any(kw in current_text_lower for kw in ["lockout active", "already answered", "securely locked"])

def _build_feedback_detail_keyboard(fb_id, return_state: str = None) -> InlineKeyboardMarkup:
    rs = return_state or "all:all:0"
    rows = [
        [InlineKeyboardButton("🔧 In Progress", callback_data=f"fb_status|{fb_id}|in_progress|{rs}"),
         InlineKeyboardButton("🗓️ Planned", callback_data=f"fb_status|{fb_id}|planned|{rs}")],
        [InlineKeyboardButton("✅ Resolved", callback_data=f"fb_status|{fb_id}|resolved|{rs}"),
         InlineKeyboardButton("🚫 Not Planned", callback_data=f"fb_status|{fb_id}|wontfix|{rs}")],
        [InlineKeyboardButton("💬 Reply to User", callback_data=f"fb_reply|{fb_id}")],
    ]
    parts = rs.split(":")
    if len(parts) == 3:
        cat, stat, off = parts
        rows.append([InlineKeyboardButton("🔙 BACK TO QUEUE", callback_data=f"fb_browse|{cat}|{stat}:{off}")])
    return InlineKeyboardMarkup(rows)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, engine):
    """Thin safety wrapper — any exception in any action branch below used to make a
    tap look like it silently did nothing (this was the exact cause behind REF 549
    not responding). Now it always answers the user instead of crashing invisibly."""
    try:
        await _handle_callback_inner(update, context, engine)
    except Exception as e:
        traceback.print_exc()
        try:
            await update.callback_query.answer("⚠️ Something went wrong. Please try again.", show_alert=True)
        except Exception:
            pass


async def _handle_callback_inner(update: Update, context: ContextTypes.DEFAULT_TYPE, engine):
    query = update.callback_query
    data = query.data.split("|")
    action, d_id = data[0], data[1]
    user_id = query.from_user.id

    print(f"\n{Style.CYAN}[CALLBACK DEBUG]{Style.RESET} Action: {action} | Ref ID: {d_id} | User ID: {user_id}")

    # Standard circular home button for intermediate flows
    return_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("👤 OPEN MY DASHBOARD", callback_data="privacy_menu|0")
    ]])

    if action == "set_grade":
        grade = int(d_id)
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        previous_grade = profile.get("grade") if profile else None

        if previous_grade and grade < previous_grade:
            await query.answer()
            warn_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"⚠️ YES, SWITCH TO GRADE {grade}", callback_data=f"confirm_grade|{grade}")],
                [InlineKeyboardButton("❌ CANCEL", callback_data="reselect_grade_panel|0")]
            ])
            await edit_rich_message_safe(
                context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
                html_content=(
                    f"⚠️ <b>Heads up before you switch</b>\n\n"
                    f"You're moving from Grade <b>{previous_grade}</b> down to Grade <b>{grade}</b>.\n\n"
                    f"Questions above your grade earn a ×1.5 challenge bonus, and below earn only ×0.3. "
                    f"Dropping your grade means questions that used to be \"above your level\" (worth more) "
                    f"now count as \"below your level\" (worth less) — your point-earning potential per question "
                    f"can drop. Only switch if this reflects your actual academic level."
                ),
                reply_markup=warn_kb
            )
            return

        await asyncio.to_thread(db_set_user_grade, query.from_user.id, grade)
        await query.answer(f"Grade {grade} registered!")
        msg = f"✅ <b>Academic Level Registered: Grade {grade}</b>\n\nYour profile is complete." if not previous_grade else \
              f"✅ <b>Grade Updated:</b> {previous_grade} → <b>{grade}</b>\n\nYour challenge-bonus multiplier now compares against Grade {grade}."
        confirm_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 CHANGE GRADE AGAIN", callback_data="reselect_grade_panel|0")],
            [InlineKeyboardButton("👤 OPEN MY DASHBOARD", callback_data="privacy_menu|0")]
        ])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=msg, reply_markup=confirm_kb)
        return

    elif action == "confirm_grade":
        grade = int(d_id)
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        previous_grade = profile.get("grade") if profile else None
        await asyncio.to_thread(db_set_user_grade, query.from_user.id, grade)
        await query.answer(f"Grade {grade} registered!")
        confirm_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 CHANGE GRADE AGAIN", callback_data="reselect_grade_panel|0")],
            [InlineKeyboardButton("👤 OPEN MY DASHBOARD", callback_data="privacy_menu|0")]
        ])
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=f"✅ <b>Grade Updated:</b> {previous_grade} → <b>{grade}</b>",
            reply_markup=confirm_kb
        )
        return

    elif action == "privacy_menu":
        await query.answer()
        USER_STATES[user_id] = "IDLE"
        USER_PAYLOADS.pop(user_id, None)
        from src.config import LAST_UTILITY_MID
        LAST_UTILITY_MID[user_id] = query.message.message_id
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        subject_marks = await asyncio.to_thread(db_get_user_subject_marks, user_id)
        text = build_profile_card_text(profile, None, subject_marks)
        kb = build_profile_main_keyboard(has_team=bool(profile.get("org_id")))
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        return

    elif action == "profile_popup":
        await query.answer()
        from src.config import LAST_UTILITY_MID
        prev_mid = LAST_UTILITY_MID.get(user_id)
        if prev_mid and prev_mid != query.message.message_id:
            try:
                await context.bot.delete_message(chat_id=query.message.chat_id, message_id=prev_mid)
            except Exception:
                pass
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        subject_marks = await asyncio.to_thread(db_get_user_subject_marks, user_id)
        text = build_profile_card_text(profile, None, subject_marks)
        kb = build_profile_main_keyboard(has_team=bool(profile.get("org_id")))
        m = await send_rich_message_safe(context.bot, chat_id=query.message.chat_id, html_content=text, reply_markup=kb)
        if m:
            LAST_UTILITY_MID[user_id] = m.message_id
        return

    elif action == "settings_menu":
        await query.answer()
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        kb = build_profile_settings_keyboard(profile.get("public_consent_granted", False))
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content="🎛️ <b>SETTINGS</b>\n<hr/>\nVisibility, nickname, grade, or location.",
            reply_markup=kb
        )
        return

    elif action == "toggle_consent":
        consent_state = (d_id == "1")
        await asyncio.to_thread(db_update_user_consent_state, user_id, consent_state)
        await query.answer("Visibility updated!")
        kb = build_profile_settings_keyboard(consent_state)
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content="🎛️ <b>SETTINGS</b>\n<hr/>\nVisibility, nickname, grade, or location.",
            reply_markup=kb
        )
        return

    elif action == "reselect_grade_panel":
        await query.answer()
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        current_grade = profile.get("grade") if profile else None

        def _lbl(g):
            return f"✅ Grade {g} (current)" if current_grade == g else f"🎒 Grade {g}"

        grade_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(_lbl(6), callback_data="set_grade|6"),
             InlineKeyboardButton(_lbl(8), callback_data="set_grade|8")],
            [InlineKeyboardButton(_lbl(10), callback_data="set_grade|10"),
             InlineKeyboardButton(_lbl(12), callback_data="set_grade|12")],
            [InlineKeyboardButton("🔙 BACK TO SETTINGS", callback_data="settings_menu|0")],
            [InlineKeyboardButton("👤 RETURN TO PROFILE", callback_data="privacy_menu|0")]
        ])
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content="🎒 <b>SELECT ACADEMIC GRADE LEVEL</b>\n<hr/>\nChanging this recalculates your challenge-bonus multiplier going forward.",
            reply_markup=grade_keyboard
        )
        return

    elif action == "alliance_portal":
        await query.answer()
        USER_STATES[user_id] = "IDLE"
        USER_PAYLOADS.pop(user_id, None)
        orgs = await asyncio.to_thread(db_get_user_organizations, user_id)

        if orgs:
            text = (
                "🏰 <b>YOUR REGISTERED TEAMS</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Select a team below to view details, rosters, or manage settings:\n"
            )
            buttons = []
            for org in orgs:
                buttons.append([InlineKeyboardButton(f"🏫 {org['org_name']} (#{org['org_tag']})", callback_data=f"view_org|{org['org_id']}")])
            buttons.append([
                InlineKeyboardButton("✨ ESTABLISH TEAM", callback_data="fsm_create_org|0"),
                InlineKeyboardButton("🔑 JOIN TEAM", callback_data="fsm_join_org|0")
            ])
        else:
            text = (
                "🏰 <b>ALLIANCE CLAN PORTAL</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "You are not registered in any Study Alliance yet.\n\n"
                "Choose an action below:"
            )
            buttons = [
                [InlineKeyboardButton("✨ ESTABLISH NEW ALLIANCE", callback_data="fsm_create_org|0")],
                [InlineKeyboardButton("🔑 INTEGRATE USING GROUP TAG", callback_data="fsm_join_org|0")]
            ]

        buttons.append([InlineKeyboardButton("❓ HOW IT WORKS", callback_data="help_menu|0")])
        buttons.append([InlineKeyboardButton("🔙 BACK TO PROFILE", callback_data="privacy_menu|0")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
        return
    elif action == "alliance_info":
        await query.answer()
        text = build_alliance_info_text()
        back_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 BACK TO ALLIANCE PORTAL", callback_data="alliance_portal|0")
        ]])
        await query.edit_message_text(text, reply_markup=back_kb, parse_mode="HTML")
        return

    elif action == "set_location_fsm":
        await query.answer()
        USER_STATES[user_id] = "AWAITING_LOCATION_CITY"
        USER_PAYLOADS[user_id] = {"edit_mid": query.message.message_id}

        fsm_cancel_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 BACK TO SETTINGS", callback_data="settings_menu|0")],
            [InlineKeyboardButton("❌ CANCEL & RETURN", callback_data="fsm_cancel|privacy_menu")]
        ])
        await query.edit_message_text(
            (
                "📍 <b>PROMPT: YOUR CITY</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Only matters if you're not on a school team — it powers the 🌆 City leaderboard for solo scholars.\n\n"
                "Please type the city you're studying in:\n"
                "<i>(Example: Addis Ababa)</i>"
            ) + FSM_INPUT_HINT,
            reply_markup=fsm_cancel_kb,
            parse_mode="HTML"
        )
        return

    elif action == "view_org":
        await query.answer()
        parts = d_id.split(":")
        org_id = int(parts[0])
        sort_field = parts[1] if len(parts) > 1 else "score"
        sort_dir = parts[2] if len(parts) > 2 else "desc"

        conn = engine.get_db_connection()
        org_details = None
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM organizations WHERE org_id = %s;", (org_id,))
                org_details = cur.fetchone()
        finally:
            engine.release_connection(conn)

        if not org_details:
            await query.edit_message_text("⚠️ Organization not found.", reply_markup=return_kb)
            return

        roster = await asyncio.to_thread(db_get_organization_roster, org_id)
        text = build_organization_card_text(org_details, roster, sort_field, sort_dir)

        user_membership = next((m for m in roster if int(m['user_id']) == int(user_id)), None)
        user_role = user_membership.get("org_role") if user_membership else "member"
        is_admin_here = user_role in ("creator", "admin")

        def _sort_btn(field, label):
            nxt = "asc" if (sort_field == field and sort_dir == "desc") else "desc"
            arrow = ("↑" if nxt == "asc" else "↓") if sort_field == field else ""
            return InlineKeyboardButton(f"{label} {arrow}".strip(), callback_data=f"view_org|{org_id}:{field}:{nxt}")

        buttons = [[_sort_btn("score", "🏆"), _sort_btn("name", "🔤"), _sort_btn("date", "📅")]]
        if is_admin_here:
            buttons.append([InlineKeyboardButton("📋 MEMBERS & REQUESTS", callback_data=f"org_history|{org_id}")])
        buttons.append([InlineKeyboardButton("🚪 LEAVE TEAM", callback_data=f"leave_org_warn|{org_id}")])
        if user_role == "creator":
            buttons.append([InlineKeyboardButton("💥 DISSOLVE TEAM", callback_data=f"dissolve_org_warn|{org_id}")])
        buttons.append([InlineKeyboardButton("🔗 INVITE LINK", callback_data=f"team_invite|{org_id}")])
        buttons.append([
            InlineKeyboardButton("🔙 TEAMS", callback_data="alliance_portal|0"),
            InlineKeyboardButton("👤 PROFILE", callback_data="privacy_menu|0")
        ])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=InlineKeyboardMarkup(buttons))
        return

    elif action == "leave_org_warn":
        await query.answer()
        org_id = int(d_id)
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        if profile.get("org_role") == "creator":
            warn_text = (
                "👑 <b>You're the Creator — Leaving Needs a Handoff</b>\n\n"
                "A team can't be left without an owner. If you leave, control will "
                "automatically pass to your longest-standing admin (or member, if there's "
                "no admin yet) so the team keeps running. This can't be undone."
            )
        else:
            warn_text = "⚠️ <b>Leave this Study Alliance?</b>\n\nYour personal score is never affected, but you'll stop counting toward this team's total."
        warn_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚪 LEAVE TEAM", callback_data=f"leave_org_confirm|{org_id}")],
            [InlineKeyboardButton("❌ CANCEL", callback_data=f"view_org|{org_id}")]
        ])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=warn_text, reply_markup=warn_kb)
        return

    elif action == "leave_org_confirm":
        await query.answer()
        org_id = int(d_id)
        result = await asyncio.to_thread(db_leave_organization, user_id, org_id)
        if result and result.get("was_creator") and result.get("promoted_id"):
            try:
                await context.bot.send_message(
                    chat_id=int(result["promoted_id"]),
                    text="👑 <b>You're now the Creator of your Study Alliance!</b>\n\nThe previous creator left, and you were promoted automatically. Manage the roster from /profile → 🏫 MY TEAM.",
                    parse_mode="HTML"
                )
            except Exception:
                pass
            msg = "🚪 You left the team. Since you were the creator, leadership was automatically handed to your longest-standing admin/member."
        else:
            msg = "🚪 You successfully exited the school team. Alliance points reset."
        await query.edit_message_text(msg, reply_markup=return_kb, parse_mode="HTML")
        return

    elif action == "dissolve_org_warn":
        await query.answer()
        org_id = int(d_id)
        warn_text = "💥 <b>Dissolve this Study Alliance?</b>\n\nThis permanently deletes the team and unlinks every member. This cannot be undone."
        warn_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💥 CONFIRM DISSOLUTION", callback_data=f"dissolve_org_confirm|{org_id}")],
            [InlineKeyboardButton("❌ CANCEL", callback_data=f"view_org|{org_id}")]
        ])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=warn_text, reply_markup=warn_kb)
        return

    elif action == "set_nick_fsm":
        await query.answer()
        USER_STATES[user_id] = "AWAITING_NICKNAME"
        USER_PAYLOADS[user_id] = {"edit_mid": query.message.message_id}

        fsm_cancel_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 BACK TO SETTINGS", callback_data="settings_menu|0")],
            [InlineKeyboardButton("❌ CANCEL & RETURN", callback_data="fsm_cancel|privacy_menu")]
        ])
        await query.edit_message_text(
            (
                "✍️ <b>PROMPT: SCOREBOARD PSEUDONYM</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Please type your preferred display name for leaderboards directly into this chat.\n\n"
                "⚠️ <b>Simple Rules:</b>\n"
                "├─ Max 20 characters\n"
                "└─ Spaces and underscores allowed"
            ) + FSM_INPUT_HINT,
            reply_markup=fsm_cancel_kb,
            parse_mode="HTML"
        )
        return

    elif action == "fsm_create_org":
        await query.answer()
        USER_STATES[user_id] = "AWAITING_ORG_NAME"
        USER_PAYLOADS[user_id] = {"edit_mid": query.message.message_id}

        fsm_cancel_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ CANCEL & RETURN", callback_data="fsm_cancel|alliance_portal")
        ]])
        await query.edit_message_text(
            (
                "✍️ <b>PROMPT: CREATE SCHOOL TEAM</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Please type the full formal name of your school or study academy team:\n"
                "<i>(Example: Abyssinia Academy)</i>"
            ) + FSM_INPUT_HINT,
            reply_markup=fsm_cancel_kb,
            parse_mode="HTML"
        )
        return

    elif action == "fsm_join_org":
        await query.answer()
        USER_STATES[user_id] = "AWAITING_ORG_JOIN"
        USER_PAYLOADS[user_id] = {"edit_mid": query.message.message_id}

        fsm_cancel_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ CANCEL & RETURN", callback_data="fsm_cancel|alliance_portal")
        ]])
        await query.edit_message_text(
            (
                "✍️ <b>PROMPT: JOIN SCHOOL TEAM</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Please enter the short, uppercase Code Tag of the school team you want to join:\n"
                "<i>(Example: ABYSSINIA)</i>"
            ) + FSM_INPUT_HINT,
            reply_markup=fsm_cancel_kb,
            parse_mode="HTML"
        )
        return

    

    elif action == "dissolve_org_confirm":
        await query.answer()
        org_id = int(d_id)
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        await asyncio.to_thread(db_dissolve_organization, org_id)
        await query.edit_message_text("💥 School team dissolved. All student mappings have been cleared.", reply_markup=return_kb)
        return

    elif action == "close_portal":
        await query.answer("Dashboard closed.")
        await query.delete_message()
        return
    
    elif action == "menu_leaderboard":
        await query.answer()
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        if not profile or not profile.get("grade"):
            await query.answer("Please set your grade first via /start.", show_alert=True)
            return
        rows = await asyncio.to_thread(db_get_weekly_leaderboard, profile['grade'])
        text = build_leaderboard_text("grade", rows, profile)
        kb = build_leaderboard_keyboard("grade")
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        return

    elif action == "lb_filter":
        await query.answer()
        scope = d_id
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        if scope == "grade":
            rows = await asyncio.to_thread(db_get_weekly_leaderboard, profile.get('grade')) if profile and profile.get('grade') else []
        elif scope == "school":
            rows = await asyncio.to_thread(db_get_alliance_leaderboard)
        elif scope == "city":
            rows = await asyncio.to_thread(db_get_city_leaderboard)
        else:
            rows = await asyncio.to_thread(db_get_country_leaderboard)
        text = build_leaderboard_text(scope, rows, profile)
        kb = build_leaderboard_keyboard(scope)
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        return

    elif action == "menu_invite":
        await query.answer()
        from src.database import db_get_or_create_referral_token
        referral_token = await asyncio.to_thread(db_get_or_create_referral_token, user_id)
        bot_username = CONFIG.get("bot_username") or (await context.bot.get_me()).username
        invite_link = f"https://t.me/{bot_username}?start=ref_{referral_token}"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("👤 MY PROFILE", callback_data="privacy_menu|0"),
            InlineKeyboardButton("🔙 CLOSE", callback_data="close_portal|0")
        ]])
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=(
                "🤝 <b>INVITE FRIENDS, EARN BONUS MARKS!</b>\n\n"
                "Share your link. When a friend joins and answers correctly, you get "
                "<b>+1 Mark</b> per correct answer — and a smaller share two levels deep too.\n\n"
                f"🔗 <b>Your link:</b>\n<code>{invite_link}</code>\n\n"
                f"👆 <i>Tap the code above to copy it, then paste it anywhere to share.</i>"
            ),
            reply_markup=kb
        )
        return
  
    elif action == "full_docs":
        await query.answer()
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=build_help_menu_text(), reply_markup=build_help_menu_keyboard()
        )
        return

    elif action == "help_menu":
        await query.answer()
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=build_help_menu_text(), reply_markup=build_help_menu_keyboard()
        )
        return

    elif action == "help_topic":
        await query.answer()
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=build_help_topic_text(d_id), reply_markup=build_help_topic_keyboard()
        )
        return

    elif action == "quickjoin_org":
        await query.answer()
        org_id = int(d_id)
        join_data = await asyncio.to_thread(db_join_organization_by_id, user_id, org_id)
        USER_PAYLOADS.pop(user_id, None)
        USER_STATES[user_id] = "IDLE"

        if not join_data:
            await query.edit_message_text("⚠️ That team no longer exists.", reply_markup=return_kb)
            return

        if join_data["role_assigned"] == "pending":
            text = f"📥 <b>Request sent!</b> <b>{join_data['org_name']}</b> requires admin approval — you'll be added once confirmed."
        else:
            text = f"✅ <b>You're in!</b> You're now registered under <b>{join_data['org_name']}</b>."
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        return

    elif action == "team_invite":
        await query.answer()
        org_id = int(d_id)
        conn = engine.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT join_token, org_name FROM organizations WHERE org_id = %s;", (org_id,))
                row = cur.fetchone()
        finally:
            engine.release_connection(conn)
        if not row:
            await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content="⚠️ Team not found.", reply_markup=return_kb)
            return
        bot_username = CONFIG.get("bot_username") or (await context.bot.get_me()).username
        invite_link = f"https://t.me/{bot_username}?start=join_{row['join_token']}"
        invite_text = f"🔗 <b>INVITE LINK FOR {row['org_name']}</b>\n\nShare this link — anyone who opens it joins (or requests to join) your team automatically:\n\n<code>{invite_link}</code>"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK TO TEAM", callback_data=f"view_org|{org_id}")]])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=invite_text, reply_markup=kb)
        return

    elif action == "force_create_org":
        await query.answer()
        session = USER_PAYLOADS.get(user_id, {})
        org_name = session.get("org_name")
        if not org_name:
            await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content="⚠️ Session expired. Please start again from 🏰 STUDY ALLIANCE TEAMS.", reply_markup=return_kb)
            return
        USER_STATES[user_id] = "AWAITING_ORG_TAG"
        USER_PAYLOADS[user_id] = {"org_name": org_name, "edit_mid": query.message.message_id}
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ CANCEL & RETURN", callback_data="fsm_cancel|alliance_portal")]])
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=f"🏫 Name Accepted: <b>{org_name}</b>\n\n✍ Enter a short, uppercase Code Tag identifier (2-15 characters, no spaces):\n<i>(Example: ABYSSINIA)</i>",
            reply_markup=cancel_kb
        )
        return

    elif action == "fb_cat":
        await query.answer()
        category = d_id
        USER_STATES[user_id] = "AWAITING_FEEDBACK_TEXT"
        USER_PAYLOADS[user_id] = {"category": category, "edit_mid": query.message.message_id}
        label = FEEDBACK_CATEGORIES.get(category, category)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 BACK TO FEEDBACK MENU", callback_data="fb_menu|0")],
            [InlineKeyboardButton("👤 MY PROFILE", callback_data="privacy_menu|0")]
        ])
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=(
                f"{label}\n\n"
                f"✍️ Describe it in your own words — as much detail helps.\n\n"
                f"📝 <i>Type your message in the box below, then tap send.</i>"
            ),
            reply_markup=kb
        )
        return
    elif action == "fb_cancel":
        USER_STATES[user_id] = "IDLE"
        USER_PAYLOADS.pop(user_id, None)
        buttons = [[InlineKeyboardButton(label, callback_data=f"fb_cat|{key}")] for key, label in FEEDBACK_CATEGORIES.items()]
        buttons.append([InlineKeyboardButton("📋 MY FEEDBACK & REQUESTS", callback_data="my_feedback|0")])
        buttons.append([
            InlineKeyboardButton("👤 GO TO PROFILE", callback_data="privacy_menu|0"),
            InlineKeyboardButton("❌ CANCEL", callback_data="close_portal|0")
        ])
        await query.answer("Cancelled.")
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=build_feedback_menu_text(), reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    elif action == "fb_status":
        from src.database import db_is_admin
        if not await asyncio.to_thread(db_is_admin, user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        fb_id, new_status = d_id, data[2]
        return_state = data[3] if len(data) > 3 else None
        await asyncio.to_thread(db_update_feedback_status, int(fb_id), new_status)
        await query.answer(f"Marked {FEEDBACK_STATUS_LABELS.get(new_status, new_status)}")

        fb = await asyncio.to_thread(db_get_feedback_by_id, int(fb_id))
        if fb:
            thread = await asyncio.to_thread(db_get_feedback_thread, int(fb_id))
            kb = _build_feedback_detail_keyboard(fb_id, return_state)
            await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=build_feedback_thread_text(fb, thread), reply_markup=kb)

            if new_status in ("planned", "resolved"):
                try:
                    notice_text = (
                        f"🗓️ Your feedback #{fb_id} has been <b>added to our plans for a future update</b>!"
                        if new_status == "planned" else
                        f"✅ Your feedback #{fb_id} has been <b>resolved</b>! Thanks for helping improve the bot."
                    )
                    notice_text += "\n\n<i>The full history always stays in /myfeedback.</i>"
                    notice_kb = InlineKeyboardMarkup([
                        [InlineKeyboardButton("💬 VIEW & REPLY", callback_data=f"fb_view|{fb_id}|0")],
                        [InlineKeyboardButton("👤 MY PROFILE", callback_data="privacy_menu|0")]
                    ])
                    from src.config import FEEDBACK_NOTICE_MIDS
                    existing = FEEDBACK_NOTICE_MIDS.get(int(fb_id))
                    if existing and str(existing[0]) == str(fb['user_id']):
                        try:
                            await edit_rich_message_safe(context.bot, chat_id=int(fb['user_id']), message_id=existing[1], html_content=notice_text, reply_markup=notice_kb)
                        except Exception:
                            notice_msg = await send_rich_message_safe(context.bot, chat_id=int(fb['user_id']), html_content=notice_text, reply_markup=notice_kb)
                            FEEDBACK_NOTICE_MIDS[int(fb_id)] = (fb['user_id'], notice_msg.message_id)
                    else:
                        notice_msg = await send_rich_message_safe(context.bot, chat_id=int(fb['user_id']), html_content=notice_text, reply_markup=notice_kb)
                        FEEDBACK_NOTICE_MIDS[int(fb_id)] = (fb['user_id'], notice_msg.message_id)
                except Exception:
                    pass
        return

    elif action == "fb_reply":
        from src.database import db_is_admin
        if not await asyncio.to_thread(db_is_admin, user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        fb_id = int(d_id)
        fb = await asyncio.to_thread(db_get_feedback_by_id, fb_id)
        if not fb:
            await query.answer("Not found.", show_alert=True)
            return
        await query.answer()
        return_state = data[2] if len(data) > 2 else None
        USER_STATES[user_id] = "AWAITING_ADMIN_REPLY"
        USER_PAYLOADS[user_id] = {"fb_id": fb_id, "target_user_id": fb['user_id'], "return_state": return_state, "edit_mid": query.message.message_id}
        thread = await asyncio.to_thread(db_get_feedback_thread, fb_id)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ CANCEL", callback_data=f"fb_item|{fb_id}|{return_state or 'all:all:0'}")]])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=f"💬 <b>Type your reply:</b>\n\n{build_feedback_thread_text(fb, thread)}",
         reply_markup=kb)
        return

    elif action == "fb_browse":
        from src.database import db_is_admin, db_count_feedback
        if not await asyncio.to_thread(db_is_admin, user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        await query.answer()

        category = d_id
        rest = data[2] if len(data) > 2 else "open:0"
        status, offset_str = rest.split(":", 1) if ":" in rest else (rest, "0")
        offset = int(offset_str) if offset_str.isdigit() else 0

        cat_filter = None if category == "all" else category
        status_filter = None if status == "all" else status

        items = await asyncio.to_thread(db_get_feedback_list, status_filter, cat_filter, 6, offset)
        total = await asyncio.to_thread(db_count_feedback, status_filter, cat_filter)

        from src.rendering.html_views import build_feedback_browse_list_text
        text = build_feedback_browse_list_text(items, category, status, offset, total)

        if not items:
            buttons = [[InlineKeyboardButton("🔙 DASHBOARD", callback_data="admin_dashboard|0")]]
            await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=InlineKeyboardMarkup(buttons))
            return

        return_state = f"{category}:{status}:{offset}"
        item_rows = [
            [InlineKeyboardButton(f"#{fb['id']} · {fb['message'][:28]}", callback_data=f"fb_item|{fb['id']}|{return_state}")]
            for fb in items
        ]

        nav_row = []
        if offset > 0:
            nav_row.append(InlineKeyboardButton("⬅️ PREV", callback_data=f"fb_browse|{category}|{status}:{max(0, offset-6)}"))
        if offset + 6 < total:
            nav_row.append(InlineKeyboardButton("NEXT ➡️", callback_data=f"fb_browse|{category}|{status}:{offset+6}"))
        if nav_row:
            item_rows.append(nav_row)

        item_rows.append([
            InlineKeyboardButton("🆕 Open", callback_data=f"fb_browse|{category}|open:0"),
            InlineKeyboardButton("🔧 Active", callback_data=f"fb_browse|{category}|in_progress:0"),
            InlineKeyboardButton("📋 All", callback_data=f"fb_browse|{category}|all:0"),
        ])
        item_rows.append([InlineKeyboardButton("🔙 DASHBOARD", callback_data="admin_dashboard|0")])

        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=InlineKeyboardMarkup(item_rows))
        return

    elif action == "admin_dashboard":
        from src.database import db_is_admin
        if not await asyncio.to_thread(db_is_admin, user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        await query.answer()
        stats = await asyncio.to_thread(db_get_admin_dashboard_stats)
        text = build_admin_dashboard_text(stats)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 VIEW USER DIRECTORY", callback_data="admin_users|0"),
             InlineKeyboardButton("💬 VIEW FEEDBACK", callback_data="fb_browse|all|open:0")],
            [InlineKeyboardButton("📚 ALL QUESTIONS", callback_data="admin_questions|all:all:0")]
        ])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        return

    elif action == "admin_users":
        from src.database import db_is_admin
        if not await asyncio.to_thread(db_is_admin, user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        await query.answer()
        offset = int(d_id)
        users = await asyncio.to_thread(db_get_recent_users, 15, offset)
        text = build_user_directory_text(users)
        nav_row = []
        if offset > 0:
            nav_row.append(InlineKeyboardButton("⬅️ PREV", callback_data=f"admin_users|{max(0, offset-15)}"))
        if len(users) == 15:
            nav_row.append(InlineKeyboardButton("NEXT ➡️", callback_data=f"admin_users|{offset+15}"))
        buttons = [nav_row] if nav_row else []
        buttons.append([InlineKeyboardButton("🔙 BACK TO DASHBOARD", callback_data="admin_dashboard|0")])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=InlineKeyboardMarkup(buttons))
        return

    elif action == "my_feedback":
        await query.answer()
        offset = int(d_id)
        items = await asyncio.to_thread(db_get_user_feedback_list, user_id, 5, offset)
        total = await asyncio.to_thread(db_count_user_feedback, user_id)
        text = build_user_feedback_list_text(items, total)

        item_rows = [
            [InlineKeyboardButton(f"#{fb['id']} · {fb['message'][:24]}", callback_data=f"fb_view|{fb['id']}|{offset}")]
            for fb in items
        ]
        nav_row = []
        if offset > 0:
            nav_row.append(InlineKeyboardButton("⬅️ PREV", callback_data=f"my_feedback|{max(0, offset-5)}"))
        if offset + 5 < total:
            nav_row.append(InlineKeyboardButton("NEXT ➡️", callback_data=f"my_feedback|{offset+5}"))
        if nav_row:
            item_rows.append(nav_row)
        item_rows.append([InlineKeyboardButton("🔙 BACK TO FEEDBACK MENU", callback_data="fb_menu|0")])
        item_rows.append([InlineKeyboardButton("👤 BACK TO PROFILE", callback_data="privacy_menu|0")])

        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=InlineKeyboardMarkup(item_rows))
        return

    elif action == "fb_menu":
        await query.answer()
        USER_STATES[user_id] = "IDLE"
        USER_PAYLOADS.pop(user_id, None)
        from src.rendering.html_views import build_feedback_menu_text
        buttons = [[InlineKeyboardButton(label, callback_data=f"fb_cat|{key}")] for key, label in FEEDBACK_CATEGORIES.items()]
        buttons.append([InlineKeyboardButton("📋 MY FEEDBACK & REQUESTS", callback_data="my_feedback|0")])
        buttons.append([
            InlineKeyboardButton("👤 GO TO PROFILE", callback_data="privacy_menu|0"),
            InlineKeyboardButton("❌ CANCEL", callback_data="close_portal|0")
        ])
        text = build_feedback_menu_text()
        kb = InlineKeyboardMarkup(buttons)
        # The target message may have been deleted by an unrelated concurrent DM
        # (e.g. an answer explanation card arriving mid-tap) — if editing it fails,
        # send a fresh feedback menu instead of silently doing nothing, and re-track
        # it as the utility message so future taps stay in sync.
        try:
            await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
            from src.config import LAST_UTILITY_MID
            LAST_UTILITY_MID[user_id] = query.message.message_id
        except Exception as edit_err:
            print(f"[FB-MENU-FALLBACK] Edit failed ({edit_err}), sending fresh feedback menu.", flush=True)
            m = await send_rich_message_safe(context.bot, chat_id=query.message.chat_id, html_content=text, reply_markup=kb)
            if m:
                from src.config import LAST_UTILITY_MID
                LAST_UTILITY_MID[user_id] = m.message_id
        return

    elif action == "fb_item":
        from src.database import db_is_admin
        if not await asyncio.to_thread(db_is_admin, user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        await query.answer()
        fb_id = int(d_id)
        return_state = data[2] if len(data) > 2 else None
        fb = await asyncio.to_thread(db_get_feedback_by_id, fb_id)
        if not fb:
            await query.answer("Not found.", show_alert=True)
            return
        thread = await asyncio.to_thread(db_get_feedback_thread, fb_id)
        kb = _build_feedback_detail_keyboard(fb_id, return_state)
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=build_feedback_thread_text(fb, thread), reply_markup=kb)
        return

    elif action == "my_feedback":
        await query.answer()
        offset = int(d_id)
        items = await asyncio.to_thread(db_get_user_feedback_list, user_id, 5, offset)
        total = await asyncio.to_thread(db_count_user_feedback, user_id)
        text = build_user_feedback_list_text(items, total)

        nav_row = []
        if offset > 0:
            nav_row.append(InlineKeyboardButton("⬅️ PREV", callback_data=f"my_feedback|{max(0, offset - 5)}"))
        if offset + 5 < total:
            nav_row.append(InlineKeyboardButton("NEXT ➡️", callback_data=f"my_feedback|{offset + 5}"))
        buttons = [nav_row] if nav_row else []
        buttons.append([InlineKeyboardButton("✍️ SUBMIT NEW FEEDBACK", callback_data="fb_menu|0")])
        buttons.append([InlineKeyboardButton("🔙 BACK", callback_data="fb_menu|0")])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=InlineKeyboardMarkup(buttons))
        return

    elif action == "fb_view":
        await query.answer()
        fb_id = int(d_id)
        return_offset = data[2] if len(data) > 2 else "0"
        fb = await asyncio.to_thread(db_get_feedback_by_id, fb_id)
        if not fb or str(fb.get("user_id")) != str(user_id):
            await query.answer("Not found.", show_alert=True)
            return
        thread = await asyncio.to_thread(db_get_feedback_thread, fb_id)
        text = build_feedback_thread_text(fb, thread)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 REPLY", callback_data=f"fb_user_reply|{fb_id}|{return_offset}")],
            [InlineKeyboardButton("🔙 BACK TO LIST", callback_data=f"my_feedback|{return_offset}")]
        ])
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=text, reply_markup=kb
        )
        return

    elif action == "fb_user_reply":
        await query.answer()
        fb_id = int(d_id)
        return_offset = data[2] if len(data) > 2 else "0"
        fb = await asyncio.to_thread(db_get_feedback_by_id, fb_id)
        if not fb or str(fb.get("user_id")) != str(user_id):
            await query.answer("Not found.", show_alert=True)
            return
        USER_STATES[user_id] = "AWAITING_USER_FEEDBACK_REPLY"
        USER_PAYLOADS[user_id] = {"fb_id": fb_id, "return_offset": return_offset, "edit_mid": query.message.message_id}
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ CANCEL", callback_data=f"fb_view|{fb_id}|{return_offset}")]])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content="✍️ <b>Type your reply below:</b>", reply_markup=kb)
        return

    elif action == "fsm_cancel":
        await query.answer("Cancelled.")
        USER_STATES[user_id] = "IDLE"
        USER_PAYLOADS.pop(user_id, None)
        from src.config import LAST_UTILITY_MID
        LAST_UTILITY_MID.pop(user_id, None)

        destination = d_id or "privacy_menu"

        # Delete the in-flight FSM card outright — the destination screen is opened
        # as a fresh message instead of morphing this one back into it.
        try:
            await query.delete_message()
        except Exception:
            pass

        if destination == "alliance_portal":
            orgs = await asyncio.to_thread(db_get_user_organizations, user_id)
            if orgs:
                text = "🏰 <b>YOUR REGISTERED TEAMS</b>\n<hr/>\nSelect a team to view details:\n"
                buttons = [[InlineKeyboardButton(f"🏫 {org['org_name']} (#{org['org_tag']})", callback_data=f"view_org|{org['org_id']}")] for org in orgs]
                buttons.append([InlineKeyboardButton("✨ ESTABLISH TEAM", callback_data="fsm_create_org|0"),
                                 InlineKeyboardButton("🔑 JOIN TEAM", callback_data="fsm_join_org|0")])
            else:
                text = "🏰 <b>ALLIANCE CLAN PORTAL</b>\n<hr/>\nYou're not on a team yet."
                buttons = [[InlineKeyboardButton("✨ ESTABLISH NEW ALLIANCE", callback_data="fsm_create_org|0")],
                           [InlineKeyboardButton("🔑 INTEGRATE USING GROUP TAG", callback_data="fsm_join_org|0")]]
            buttons.append([InlineKeyboardButton("❓ HOW IT WORKS", callback_data="help_menu|0")])
            buttons.append([InlineKeyboardButton("🔙 BACK TO PROFILE", callback_data="privacy_menu|0")])
            m = await send_rich_message_safe(context.bot, chat_id=query.message.chat_id, html_content=text, reply_markup=InlineKeyboardMarkup(buttons))
            if m:
                LAST_UTILITY_MID[user_id] = m.message_id
            return

        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        subject_marks = await asyncio.to_thread(db_get_user_subject_marks, user_id)
        text = build_profile_card_text(profile, None, subject_marks)
        kb = build_profile_main_keyboard(has_team=bool(profile.get("org_id")))
        m = await send_rich_message_safe(context.bot, chat_id=query.message.chat_id, html_content=text, reply_markup=kb)
        if m:
            LAST_UTILITY_MID[user_id] = m.message_id
        return


    elif action == "org_history":
        org_id = int(d_id)
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        if profile.get("org_role") not in ("creator", "admin") or int(profile.get("org_id") or 0) != org_id:
            await query.answer("Only team admins can view this.", show_alert=True)
            return
        await query.answer()

        conn = engine.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM organizations WHERE org_id = %s;", (org_id,))
                org_details = cur.fetchone()
        finally:
            engine.release_connection(conn)
        if not org_details:
            await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content="⚠️ Team not found.", reply_markup=return_kb)
            return

        from src.database import db_get_org_membership_log
        from src.rendering.html_views import build_org_history_text
        log_rows = await asyncio.to_thread(db_get_org_membership_log, org_id)
        text = build_org_history_text(org_details, log_rows)

        pending_rows = [r for r in log_rows if r['org_role'] == 'pending']
        kb_rows = []
        for r in pending_rows[:8]:
            nm_short = format_public_name(r)[:16]
            kb_rows.append([
                InlineKeyboardButton(f"🟢 {nm_short}", callback_data=f"process_req|{org_id}|{r['user_id']}|1"),
                InlineKeyboardButton("🔴 Reject", callback_data=f"process_req|{org_id}|{r['user_id']}|0")
            ])
        kb_rows.append([InlineKeyboardButton("🔙 BACK TO TEAM", callback_data=f"view_org|{org_id}")])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=InlineKeyboardMarkup(kb_rows))
        return

    elif action == "process_req":
        org_id, target_user_id, decision = int(d_id), data[2], data[3]
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        if profile.get("org_role") not in ("creator", "admin") or int(profile.get("org_id") or 0) != org_id:
            await query.answer("Only team admins can process requests.", show_alert=True)
            return

        approve = (decision == "1")
        ok = await asyncio.to_thread(db_approve_member_request, target_user_id, org_id, approve)
        await query.answer("Approved!" if approve else "Rejected.")

        conn = engine.get_db_connection()
        org_name = "your team"
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT org_name FROM organizations WHERE org_id = %s;", (org_id,))
                row = cur.fetchone()
                if row:
                    org_name = row['org_name']
        finally:
            engine.release_connection(conn)

        try:
            status_line = f"\n\n{'✅ Approved' if approve else '🚫 Rejected'} — this request is now closed."
            old_text = (query.message.text or query.message.caption or "") + status_line
            await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=old_text, reply_markup=None)
        except Exception:
            pass

        if ok and approve:
            join_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🏫 GO TO TEAM", callback_data=f"view_org|{org_id}")],
                [InlineKeyboardButton("👤 MY PROFILE", callback_data="privacy_menu|0")]
            ])
            try:
                await context.bot.send_message(
                    chat_id=int(target_user_id),
                    text=f"✅ <b>You're in!</b>\n\nYour request to join <b>{html.escape(org_name)}</b> was approved. Every correct answer now also scores for your team!",
                    parse_mode="HTML", reply_markup=join_kb
                )
            except Exception:
                pass
        elif ok and not approve:
            try:
                await context.bot.send_message(
                    chat_id=int(target_user_id),
                    text=f"Your request to join <b>{html.escape(org_name)}</b> wasn't accepted this time. You're welcome to try another team.",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        return

    elif action == "my_answers_menu":
        await query.answer()
        from src.database import db_get_user_subjects_summary
        from src.rendering.html_views import build_my_answers_subject_menu_text, build_my_answers_subject_keyboard
        summary = await asyncio.to_thread(db_get_user_subjects_summary, user_id)
        text = build_my_answers_subject_menu_text(summary)
        kb = build_my_answers_subject_keyboard(summary)
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        return

    elif action == "my_ans_subj":
        await query.answer()
        subject, filter_mode, offset_str = data[1], data[2], data[3]
        offset = int(offset_str)
        sort_code = data[4] if len(data) > 4 else "t"
        dir_code = data[5] if len(data) > 5 else "a"
        sort_field = {"t": "topic", "d": "date", "g": "tags", "l": "difficulty"}.get(sort_code, "topic")
        sort_dir = {"a": "asc", "d": "desc"}.get(dir_code, "asc")

        from src.database import db_get_user_question_matrix, db_count_user_question_matrix
        from src.rendering.html_views import build_my_answers_list_text, build_my_answers_keyboard
        rows = await asyncio.to_thread(db_get_user_question_matrix, user_id, subject, filter_mode, 8, offset, sort_field, sort_dir)
        total = await asyncio.to_thread(db_count_user_question_matrix, user_id, subject, filter_mode)
        text = build_my_answers_list_text(rows, subject, filter_mode, offset, total, sort_field, sort_dir)
        kb = build_my_answers_keyboard(rows, subject, filter_mode, offset, total, sort_field, sort_dir)
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        return

    elif action == "my_ans_open":
        await query.answer()
        q_id, subject, filter_mode, offset = data[1], data[2], data[3], data[4]
        sort_code = data[5] if len(data) > 5 else "t"
        dir_code = data[6] if len(data) > 6 else "a"
        back_cb = f"my_ans_subj|{subject}|{filter_mode}|{offset}|{sort_code}|{dir_code}"

        q = await asyncio.to_thread(db_get_question_by_id, q_id)
        if not q:
            await query.answer("Question not found.", show_alert=True)
            return
        track = await asyncio.to_thread(db_get_latest_track_for_question, q_id)
        back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK TO LIST", callback_data=back_cb)]])

        if not track:
            await edit_rich_message_safe(
                context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
                html_content=f"📅 <b>{q['topic']}</b>\n\nThis question hasn't been published yet.",
                reply_markup=back_kb
            )
            return

        existing = await asyncio.to_thread(db_get_user_response, user_id, track['message_id'])
        if existing:
            # Trust the stored track status — reconcile_deleted_track_messages() already
            # keeps this current every ~30s. A live forward+delete probe on every tap was
            # what caused the multi-second "freeze" opening an answered question.
            removed_note = ""
            if track.get('status') == 'deleted':
                removed_note = (
                    "\n\n<i>⚫ Note: this question was later removed from the channel — "
                    "your saved answer above is unaffected.</i>"
                )

            perf_card = await asyncio.to_thread(process_user_score, user_id, track['message_id'], q_id, existing['is_correct'], existing['selected_option'])
            explanation_html = UIFactory.build_answered_view(q, str(track['display_id']), existing['selected_option'], show_derivation=True, show_perf=False, perf_card=perf_card) + removed_note
            await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=explanation_html, reply_markup=back_kb)
            return

        if track['status'] == 'deleted':
            await edit_rich_message_safe(
                context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
                html_content=f"⚫ <b>{q['topic']}</b>\n\nThis question was removed from the channel and is no longer available.",
                reply_markup=back_kb
            )
            return

        channel_username = CONFIG.get("channel", "QuizOva").lstrip('@')
        open_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📣 OPEN IN CHANNEL", url=f"https://t.me/{channel_username}/{track['message_id']}")],
            [InlineKeyboardButton("🔙 BACK TO LIST", callback_data=back_cb)]
        ])
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=f"⬜ <b>{q['topic']}</b>\n\nYou haven't answered this one yet — tap below to open it in the channel.",
            reply_markup=open_kb
        )
        return

    elif action == "admin_questions":
        from src.database import db_is_admin, db_get_admin_question_overview, db_count_admin_questions
        if not await asyncio.to_thread(db_is_admin, user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        await query.answer()
        subj_raw, status_filter, offset_str = d_id.split(":")
        subject = None if subj_raw == "all" else subj_raw
        offset = int(offset_str)
        rows = await asyncio.to_thread(db_get_admin_question_overview, subject, status_filter, 10, offset)
        total = await asyncio.to_thread(db_count_admin_questions, subject, status_filter)
        from src.rendering.html_views import build_admin_questions_text, build_admin_questions_keyboard
        channel_username = CONFIG.get("channel", "QuizOva").lstrip('@')
        text = build_admin_questions_text(rows, subj_raw, status_filter, offset, total, channel_username)
        kb = build_admin_questions_keyboard(subject, status_filter, offset, total)
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        return






    if action not in ("ans", "toggle", "toggle_photo"):
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        subject_marks = await asyncio.to_thread(db_get_user_subject_marks, user_id)
        text = build_profile_card_text(profile, None, subject_marks)
        kb = build_profile_main_keyboard(has_team=bool(profile.get("org_id")))
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        return

    # --- ORIGINAL CORE ENGINE FLOWS ---

    track, question_data = await asyncio.to_thread(db_get_track_and_question, int(d_id))

    if not track or not question_data:
        print(f" {Style.RED}└─ [ERROR] No track record located for Ref ID: {d_id}{Style.RESET}")
        await query.answer("This quiz session has ended.", show_alert=True)
        return

    track_status = track.get('status')
    mid_key = track['message_id']
    warning_notice = "ℹ️ <b>Already Answered</b>\n" \
                    "<i>You've already submitted your answer for this one — here's your saved result.</i>\n\n"

    try:
        if action == "ans":
            if track_status == "tournament_closed":
                await query.answer("This round is closed. Submissions are no longer accepted!", show_alert=True)
                return

            if track_status == "tournament_active":
                existing_response = await asyncio.to_thread(db_get_user_response, user_id, mid_key)
                if existing_response:
                    await query.answer("✅ Already submitted — sit tight, results are on the way!", show_alert=False)
                    return

                user_selection = int(data[2])
                is_correct = (user_selection == question_data['correct_option'])
                
                print(f" [CALLBACK-TOURNAMENT-TRACE] Submitting User: {user_id} | Option: {user_selection} | Is Correct: {is_correct}", flush=True)
                
                try:
                    await asyncio.to_thread(process_user_score, user_id, mid_key, question_data['id'], is_correct, user_selection, None, True, False)
                except Exception as db_err:
                    try:
                        from src.debug_log import dlog_exception
                        dlog_exception(f"callbacks.py -> handle_callback (process_user_score failed for User: {user_id}, Message ID: {mid_key})", db_err)
                    except Exception:
                        pass
                    raise db_err

                await query.answer("Response recorded!")
                
                # Dynamic response receipt with show_derivation=True activated by default
                explanation_html = UIFactory.build_answered_view(question_data, d_id, user_selection, show_derivation=True, show_perf=False, perf_card=None)
                await query.edit_message_text(explanation_html, reply_markup=return_kb, parse_mode="HTML")
                await asyncio.to_thread(db_update_private_message_id, user_id, mid_key, query.message.message_id)
                return

            if track_status != "active" and track_status != "closed":
                print(f" {Style.YELLOW}└─ [WARNING] Blocked submission: Quiz status is '{track_status}'.{Style.RESET}")
                await query.answer("This quiz session has ended. Submissions are closed!", show_alert=True)
                return

            user_selection = int(data[2])
            print(f" {Style.CYAN}├─ [DEBUG] Generating Answer Summary Sheet for REF: {d_id}{Style.RESET}")
            await query.answer("Generating Answer Sheet...")

            is_correct = (user_selection == question_data['correct_option'])
            
            try:
                perf_card = await db_call_guarded(process_user_score, user_id, mid_key, question_data['id'], is_correct, user_selection, None, False, False)
            except TimeoutError:
                await send_rich_message_safe(context.bot, chat_id=update.message.chat_id, html_content="⚠️ We're experiencing very high traffic right now. Please tap your answer again in a few seconds.", reply_markup=channel_kb)
                return

            # Direct to-Telegram inline derivation rendering
            explanation_html = UIFactory.build_answered_view(question_data, d_id, user_selection, show_derivation=True, show_perf=False, perf_card=perf_card)

            has_lockout = check_message_has_lockout(user_id, query.message)
            if has_lockout:
                explanation_html = warning_notice + explanation_html

            has_tikz = UIFactory.has_real_diagram(question_data)
            media_bytes = None
            cached_file_id = None

            kb = UIFactory.build_answered_keyboard(d_id, user_selection, True, False, is_photo=False, message_id=track['message_id'])

            if has_tikz:
                cache_key = f"q:{question_data['id']}:exp:{user_selection}"
                cached_file_id = await asyncio.to_thread(db_get_cached_file_id, cache_key)

                if not cached_file_id:
                    print(f" {Style.YELLOW}├─ [CACHE MISS] Solution sheet not cached. Compiling via Kroki...{Style.RESET}")
                    latex_code, _ = UIFactory.create_explanation_assets(question_data, user_selection, d_id)
                    if latex_code:
                        img_url = UIFactory.get_latex_url(latex_code)
                        async with httpx.AsyncClient() as client:
                            resp = await fetch_kroki_image(client, img_url, latex_code)
                            if resp and resp.status_code == 200:
                                media_bytes = resp.content

            m = await edit_rich_message_safe(
                context.bot,
                chat_id=query.message.chat_id,
                message_id=query.message.message_id,
                html_content=explanation_html,
                reply_markup=kb,
                media_bytes=media_bytes,
                file_id=cached_file_id
            )

            if media_bytes and m and m.photo and not cached_file_id:
                            await asyncio.to_thread(db_save_cached_file_id, cache_key, m.photo[-1].file_id)

            if m:
                await asyncio.to_thread(db_update_private_message_id, user_id, mid_key, m.message_id)
            return

        elif action in ["toggle", "toggle_photo"]:
            user_selection = int(data[2])
            show_derivation = (int(data[3]) == 1)
            show_perf = (int(data[4]) == 1)
            await query.answer("Updating View...")

            is_correct_ans = (user_selection == question_data['correct_option'])

            state_task = asyncio.to_thread(db_update_response_view_state, user_id, mid_key, show_derivation, show_perf)
            score_task = asyncio.to_thread(process_user_score, user_id, mid_key, question_data['id'], is_correct_ans, user_selection)

            try:
                _, perf_card = await asyncio.gather(state_task, score_task)
            except Exception as db_err:
                try:
                    from src.debug_log import dlog_exception
                    dlog_exception(f"callbacks.py -> handle_callback toggle tasks failed for User: {user_id}, Message ID: {mid_key}", db_err)
                except Exception:
                    pass
                raise db_err

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

            has_tikz = UIFactory.has_real_diagram(question_data)
            media_bytes = None
            cached_file_id = None

            kb = UIFactory.build_answered_keyboard(d_id, user_selection, show_derivation, show_perf, is_photo=False, message_id=track['message_id'])

            if has_tikz:
                cache_key = f"q:{question_data['id']}:exp:{user_selection}"
                cached_file_id = await asyncio.to_thread(db_get_cached_file_id, cache_key)

                if not cached_file_id:
                    print(f" {Style.YELLOW}├─ [CACHE MISS] Solution sheet not cached. Compiling via Kroki...{Style.RESET}")
                    latex_code, _ = UIFactory.create_explanation_assets(question_data, user_selection, d_id)
                    if latex_code:
                        img_url = UIFactory.get_latex_url(latex_code)
                        async with httpx.AsyncClient() as client:
                            resp = await fetch_kroki_image(client, img_url, latex_code)
                            if resp and resp.status_code == 200:
                                media_bytes = resp.content

            m = await edit_rich_message_safe(
                context.bot,
                chat_id=query.message.chat_id,
                message_id=query.message.message_id,
                html_content=explanation_html,
                reply_markup=kb,
                media_bytes=media_bytes,
                file_id=cached_file_id
            )

            if media_bytes and m and m.photo and not cached_file_id:
                await asyncio.to_thread(db_save_cached_file_id, cache_key, m.photo[-1].file_id)
            return

    except Exception as e:
        traceback.print_exc()
        print(f" {Style.RED}└─ [EXCEPTION] Fatal error in callback thread: {e}{Style.RESET}")
        
        try:
            from src.debug_log import dlog_exception
            dlog_exception(f"callbacks.py -> handle_callback global catch (Action: {action} | Ref ID: {d_id})", e)
        except Exception:
            pass

        await query.answer("System Error: Could not render response.", show_alert=True)