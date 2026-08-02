# src/callbacks.py
import asyncio
import traceback
import httpx
import io
from src.config import CONFIG, Style, LOCKOUT_MESSAGES, USER_STATES, USER_PAYLOADS, ADMIN_IDS, FEEDBACK_CATEGORIES, FEEDBACK_STATUS_LABELS
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
    db_call_guarded
)
from src.rendering.html_views import (
    build_profile_card_text,
    build_alliance_info_text,
    build_organization_card_text,
    build_full_documentation_text,
    build_bot_roadmap_text,
    build_feedback_item_text,
    build_admin_dashboard_text, 
    build_user_directory_text
)
from src.rendering.html_views import build_profile_card_text, build_alliance_info_text
from telegram import Update, InputMediaPhoto, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

def check_message_has_lockout(user_id, message) -> bool:
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
    user_id = query.from_user.id

    print(f"\n{Style.CYAN}[CALLBACK DEBUG]{Style.RESET} Action: {action} | Ref ID: {d_id} | User ID: {user_id}")

    # Standard circular home button for intermediate flows
    return_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("👤 OPEN MY DASHBOARD", callback_data="privacy_menu|0")
    ]])

    if action == "set_grade":
        grade = int(d_id)
        await asyncio.to_thread(db_set_user_grade, query.from_user.id, grade)
        await query.answer(f"Grade {grade} registered!")
        await query.edit_message_text(
            f"✅ <b>Academic Level Registered: Grade {grade}</b>\n\n"
            "Your profile details are complete. Explore leaderboards or update details below:",
            reply_markup=return_kb,
            parse_mode="HTML"
        )
        return

    # --- SIMPLIFIED UNIFIED PROFILE PORTAL CALLBACK FLOWS ---

    elif action == "privacy_menu":
        await query.answer()
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        org_id = profile.get("org_id")
        roster = await asyncio.to_thread(db_get_organization_roster, org_id) if org_id else []
        
        text = build_profile_card_text(profile, roster)
        
        consent_btn_text = "🔴 OPT-OUT PUBLIC LEADERBOARDS" if profile.get("public_consent_granted") else "🟢 OPT-IN PUBLIC LEADERBOARDS"
        consent_target = "0" if profile.get("public_consent_granted") else "1"
        
        buttons = [
            [InlineKeyboardButton(consent_btn_text, callback_data=f"toggle_consent|{consent_target}")],
            [InlineKeyboardButton("📝 UPDATE PUBLIC NICKNAME", callback_data="set_nick_fsm|0")],
            [InlineKeyboardButton("🎒 CHANGE ACADEMIC LEVEL", callback_data="reselect_grade_panel|0")],
            [InlineKeyboardButton("📍 UPDATE MY LOCATION", callback_data="set_location_fsm|0")],
            [InlineKeyboardButton("🏰 STUDY ALLIANCE TEAMS", callback_data="alliance_portal|0")],
            [InlineKeyboardButton("📖 HOW IT WORKS", callback_data="full_docs|0"),
             InlineKeyboardButton("🗺️ ROADMAP", callback_data="roadmap|0")]
        ]
        buttons.append([InlineKeyboardButton("🔙 CLOSE PANEL", callback_data="close_portal|0")])
        
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
        return

    elif action == "toggle_consent":
        consent_state = (d_id == "1")
        await asyncio.to_thread(db_update_user_consent_state, user_id, consent_state)
        await query.answer("Scoreboard visibility status updated!", show_alert=True)
        # Re-render the menu instantly
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        org_id = profile.get("org_id")
        roster = await asyncio.to_thread(db_get_organization_roster, org_id) if org_id else []
        text = build_profile_card_text(profile, roster)
        
        consent_btn_text = "🔴 OPT-OUT PUBLIC LEADERBOARDS" if profile.get("public_consent_granted") else "🟢 OPT-IN PUBLIC LEADERBOARDS"
        consent_target = "0" if profile.get("public_consent_granted") else "1"
        
        buttons = [
            [InlineKeyboardButton(consent_btn_text, callback_data=f"toggle_consent|{consent_target}")],
            [InlineKeyboardButton("📝 UPDATE PUBLIC NICKNAME", callback_data="set_nick_fsm|0")],
            [InlineKeyboardButton("🎒 CHANGE ACADEMIC LEVEL", callback_data="reselect_grade_panel|0")],
            [InlineKeyboardButton("📍 UPDATE MY LOCATION", callback_data="set_location_fsm|0")],
            [InlineKeyboardButton("🏰 STUDY ALLIANCE TEAMS", callback_data="alliance_portal|0")],
            [InlineKeyboardButton("📖 HOW IT WORKS", callback_data="full_docs|0"),
             InlineKeyboardButton("🗺️ ROADMAP", callback_data="roadmap|0")]
        ]
        buttons.append([InlineKeyboardButton("🔙 CLOSE PANEL", callback_data="close_portal|0")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
        return

    elif action == "reselect_grade_panel":
        await query.answer()
        grade_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎒 Grade 6", callback_data="set_grade|6"),
             InlineKeyboardButton("🎒 Grade 8", callback_data="set_grade|8")],
            [InlineKeyboardButton("🎒 Grade 10", callback_data="set_grade|10"),
             InlineKeyboardButton("🎒 Grade 12", callback_data="set_grade|12")],
            [InlineKeyboardButton("🔙 RETURN TO PROFILE", callback_data="privacy_menu|0")]
        ])
        await query.edit_message_text(
            "🎒 <b>SELECT ACADEMIC GRADE LEVEL</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Choose your active academic level using the options below:",
            reply_markup=grade_keyboard,
            parse_mode="HTML"
        )
        return

    elif action == "alliance_portal":
        await query.answer()
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

        buttons.append([InlineKeyboardButton("❓ HOW IT WORKS", callback_data="alliance_info|0")])
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

        fsm_cancel_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ CANCEL & RETURN", callback_data="privacy_menu|0")
        ]])
        await query.edit_message_text(
            "📍 <b>PROMPT: YOUR CITY</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Only matters if you're not on a school team — it powers the 🌆 City leaderboard for solo scholars.\n\n"
            "Please type the city you're studying in:\n"
            "<i>(Example: Addis Ababa)</i>",
            reply_markup=fsm_cancel_kb,
            parse_mode="HTML"
        )
        return

    elif action == "view_org":
        await query.answer()
        org_id = int(d_id)
        
        # Pull team details from DB
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
        text = build_organization_card_text(org_details, roster)
        
        # Find user's role in this specific organization
        user_membership = next((m for m in roster if int(m['user_id']) == int(user_id)), None)
        user_role = user_membership.get("org_role") if user_membership else "member"
        
        buttons = [
            [InlineKeyboardButton("🚪 LEAVE School TEAM", callback_data=f"leave_org_warn|{org_id}")]
        ]
        if user_role == "creator":
            buttons.append([InlineKeyboardButton("💥 DISSOLVE School TEAM", callback_data=f"dissolve_org_warn|{org_id}")])
        buttons.append([InlineKeyboardButton("🔗 GET TEAM INVITE LINK", callback_data=f"team_invite|{org_id}")])
        buttons.append([InlineKeyboardButton("🔙 BACK TO TEAM LIST", callback_data="alliance_portal|0")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
        return

    elif action == "leave_org_warn":
        await query.answer()
        org_id = int(d_id)
        
        # Warn before committing the leave action
        warn_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚪 LEAVE TEAM", callback_data=f"leave_org_confirm|{org_id}")],
            [InlineKeyboardButton("❌ CANCEL", callback_data=f"view_org|{org_id}")]
        ])
        await query.edit_message_text(
            "⚠️ <b>WARNING: EXIT ALLIANCE ROSTER</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Are you sure you want to leave this study team?\n"
            "<i>Your scored marks will no longer contribute to their global collective scoreboard metrics.</i>",
            reply_markup=warn_kb,
            parse_mode="HTML"
        )
        return

    elif action == "dissolve_org_warn":
        await query.answer()
        org_id = int(d_id)
        
        # Warn with high impact before dissolving
        warn_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💥 CONFIRM DISSOLUTION", callback_data=f"dissolve_org_confirm|{org_id}")],
            [InlineKeyboardButton("❌ CANCEL", callback_data=f"view_org|{org_id}")]
        ])
        await query.edit_message_text(
            "⚠️ <b>CRITICAL WARNING: DISSOLVE TEAM ALLIANCE</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "You are about to completely dissolve and delete this school team. "
            "<b>This operation is permanent and cannot be undone!</b> All mapped students will be removed from this roster.",
            reply_markup=warn_kb,
            parse_mode="HTML"
        )
        return

    elif action == "set_nick_fsm":
        await query.answer()
        USER_STATES[user_id] = "AWAITING_NICKNAME"
        USER_PAYLOADS[user_id] = {"edit_mid": query.message.message_id}
        
        fsm_cancel_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ CANCEL & RETURN", callback_data="privacy_menu|0")
        ]])
        await query.edit_message_text(
            "✍️ <b>PROMPT: SCOREBOARD PSEUDONYM</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Please type your preferred display name for leaderboards directly into this chat.\n\n"
            "⚠️ <b>Simple Rules:</b>\n"
            "├─ Max 20 characters\n"
            "└─ Spaces and underscores allowed",
            reply_markup=fsm_cancel_kb,
            parse_mode="HTML"
        )
        return

    elif action == "fsm_create_org":
        await query.answer()
        USER_STATES[user_id] = "AWAITING_ORG_NAME"
        USER_PAYLOADS[user_id] = {"edit_mid": query.message.message_id}
        
        fsm_cancel_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ CANCEL & RETURN", callback_data="alliance_portal|0")
        ]])
        await query.edit_message_text(
            "✍️ <b>PROMPT: CREATE SCHOOL TEAM</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Please type the full formal name of your school or study academy team:\n"
            "<i>(Example: Abyssinia Academy)</i>",
            reply_markup=fsm_cancel_kb,
            parse_mode="HTML"
        )
        return

    elif action == "fsm_join_org":
        await query.answer()
        USER_STATES[user_id] = "AWAITING_ORG_JOIN"
        USER_PAYLOADS[user_id] = {"edit_mid": query.message.message_id}
        
        fsm_cancel_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ CANCEL & RETURN", callback_data="alliance_portal|0")
        ]])
        await query.edit_message_text(
            "✍️ <b>PROMPT: JOIN SCHOOL TEAM</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Please enter the short, uppercase Code Tag of the school team you want to join:\n"
            "<i>(Example: ABYSSINIA)</i>",
            reply_markup=fsm_cancel_kb,
            parse_mode="HTML"
        )
        return

    elif action == "leave_org_confirm":
        await query.answer()
        org_id = int(d_id)
        await asyncio.to_thread(db_leave_organization, user_id, org_id)
        await query.edit_message_text("🚪 You successfully exited the school team. Alliance points reset.", reply_markup=return_kb)
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
    elif action == "full_docs":
        await query.answer()
        text = build_full_documentation_text()
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🗺️ VIEW ROADMAP", callback_data="roadmap|0"),
            InlineKeyboardButton("🔙 BACK", callback_data="privacy_menu|0")
        ]])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        return

    elif action == "roadmap":
        await query.answer()
        text = build_bot_roadmap_text()
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📖 FULL DOCS", callback_data="full_docs|0"),
            InlineKeyboardButton("🔙 BACK", callback_data="privacy_menu|0")
        ]])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
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
        await query.edit_message_text(text, reply_markup=return_kb, parse_mode="HTML")
        return

    elif action == "force_create_org":
        await query.answer()
        session = USER_PAYLOADS.get(user_id, {})
        org_name = session.get("org_name")
        if not org_name:
            await query.edit_message_text("⚠️ Session expired. Please start again from 🏰 STUDY ALLIANCE TEAMS.", reply_markup=return_kb)
            return

        USER_STATES[user_id] = "AWAITING_ORG_TAG"
        USER_PAYLOADS[user_id] = {"org_name": org_name, "edit_mid": query.message.message_id}
        await query.edit_message_text(
            f"🏫 Name Accepted: <b>{org_name}</b>\n\n"
            "✍ Enter a short, uppercase Code Tag identifier for your group (2-15 characters, no spaces):\n"
            "<i>(Example: ABYSSINIA)</i>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ CANCEL & RETURN", callback_data="privacy_menu|0")]]),
            parse_mode="HTML"
        )
        return

    elif action == "team_invite":
        await query.answer()
        org_id = int(d_id)
        conn = engine.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT join_token FROM organizations WHERE org_id = %s;", (org_id,))
                row = cur.fetchone()
        finally:
            engine.release_connection(conn)
        join_token = row["join_token"] if row else None
        bot_username = CONFIG.get("bot_username") or (await context.bot.get_me()).username
        invite_link = f"https://t.me/{bot_username}?start=join_{join_token}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK TO TEAM", callback_data=f"view_org|{org_id}")]])
        await query.edit_message_text(
            f"🔗 <b>TEAM INVITE LINK</b>\n\n"
            f"Share this — anyone who opens it and taps Start joins your team directly:\n\n"
            f"<code>{invite_link}</code>",
            reply_markup=kb, parse_mode="HTML"
        )
        return

    elif action == "fb_cat":
        await query.answer()
        category = d_id
        USER_STATES[user_id] = "AWAITING_FEEDBACK_TEXT"
        USER_PAYLOADS[user_id] = {"category": category, "edit_mid": query.message.message_id}
        label = FEEDBACK_CATEGORIES.get(category, category)
        await query.edit_message_text(
            f"{label}\n\n✍ Describe it in your own words — as much detail helps:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ CANCEL", callback_data="fb_cancel|0")]]),
            parse_mode="HTML"
        )
        return

    elif action == "fb_cancel":
        await query.answer("Cancelled.")
        USER_STATES.pop(user_id, None)
        USER_PAYLOADS.pop(user_id, None)
        await query.delete_message()
        return

    elif action == "fb_status":
        from src.database import db_is_admin
        if not await asyncio.to_thread(db_is_admin, user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        fb_id, new_status = d_id, data[2]
        await asyncio.to_thread(db_update_feedback_status, int(fb_id), new_status)
        await query.answer(f"Marked {FEEDBACK_STATUS_LABELS.get(new_status, new_status)}")

        fb = await asyncio.to_thread(db_get_feedback_by_id, int(fb_id))
        if fb:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔧 In Progress", callback_data=f"fb_status|{fb_id}|in_progress"),
                 InlineKeyboardButton("🗓️ Planned", callback_data=f"fb_status|{fb_id}|planned")],
                [InlineKeyboardButton("✅ Resolved", callback_data=f"fb_status|{fb_id}|resolved"),
                 InlineKeyboardButton("🚫 Not Planned", callback_data=f"fb_status|{fb_id}|wontfix")],
                [InlineKeyboardButton("💬 Reply to User", callback_data=f"fb_reply|{fb_id}")]
            ])
            await query.edit_message_text(build_feedback_item_text(fb), reply_markup=kb, parse_mode="HTML")

            # Only tell the user for meaningful outcomes — not every intermediate nudge.
            if new_status in ("planned", "resolved"):
                try:
                    nice_msg = (
                        f"🗓️ Your feedback #{fb_id} has been <b>added to our plans for a future update</b>!"
                        if new_status == "planned" else
                        f"✅ Your feedback #{fb_id} has been <b>resolved</b>! Thanks for helping improve the bot."
                    )
                    await context.bot.send_message(chat_id=int(fb['user_id']), text=nice_msg, parse_mode="HTML")
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
        USER_STATES[user_id] = "AWAITING_ADMIN_REPLY"
        USER_PAYLOADS[user_id] = {"fb_id": fb_id, "target_user_id": fb['user_id'], "edit_mid": query.message.message_id}
        await query.edit_message_text(
            f"💬 <b>Type your reply to this user for feedback #{fb_id}:</b>\n\n{build_feedback_item_text(fb)}",
            parse_mode="HTML"
        )
        return

    elif action == "fb_browse":
        from src.database import db_is_admin
        if not await asyncio.to_thread(db_is_admin, user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        await query.answer()
        category, status = d_id, data[2]
        cat_filter = None if category == "all" else category
        items = await asyncio.to_thread(db_get_feedback_list, status, cat_filter, 5, 0)

        if not items:
            await query.edit_message_text("📭 No items match this filter.", reply_markup=return_kb)
            return

        # Send each as its own actionable card so status buttons work per item.
        await query.delete_message()
        for fb in items:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔧 In Progress", callback_data=f"fb_status|{fb['id']}|in_progress"),
                 InlineKeyboardButton("🗓️ Planned", callback_data=f"fb_status|{fb['id']}|planned")],
                [InlineKeyboardButton("✅ Resolved", callback_data=f"fb_status|{fb['id']}|resolved"),
                 InlineKeyboardButton("🚫 Not Planned", callback_data=f"fb_status|{fb['id']}|wontfix")],
                [InlineKeyboardButton("💬 Reply to User", callback_data=f"fb_reply|{fb['id']}")]
            ])
            await send_rich_message_safe(context.bot, chat_id=query.message.chat_id, html_content=build_feedback_item_text(fb), reply_markup=kb)
        return

    elif action == "admin_dashboard":
        from src.database import db_is_admin
        if not await asyncio.to_thread(db_is_admin, user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        await query.answer()
        stats = await asyncio.to_thread(db_get_admin_dashboard_stats)
        text = build_admin_dashboard_text(stats)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("👥 VIEW USER DIRECTORY", callback_data="admin_users|0"),
            InlineKeyboardButton("💬 VIEW FEEDBACK", callback_data="fb_browse|all|open")
        ]])
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
    # --- ORIGINAL CORE ENGINE FLOWS ---

    track, question_data = await asyncio.to_thread(db_get_track_and_question, int(d_id))

    if not track or not question_data:
        print(f" {Style.RED}└─ [ERROR] No track record located for Ref ID: {d_id}{Style.RESET}")
        await query.answer("This quiz session has ended.", show_alert=True)
        return

    track_status = track.get('status')
    mid_key = track['message_id']
    warning_notice = "⚠️ <b>Lockout active: You have already answered this question!</b>\n" \
                     "<i>Your original selection and score have been securely locked.</i>\n\n"

    try:
        if action == "ans":
            if track_status == "tournament_closed":
                await query.answer("This round is closed. Submissions are no longer accepted!", show_alert=True)
                return

            if track_status == "tournament_active":
                existing_response = await asyncio.to_thread(db_get_user_response, user_id, mid_key)
                if existing_response:
                    await query.answer("Lockout active! Your response has already been submitted.", show_alert=True)
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