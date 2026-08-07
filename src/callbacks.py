# src/callbacks.py
import asyncio
import traceback
import httpx
import io
import html
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
    db_hide_question_for_user,
    db_get_alliance_leaderboard,
    db_get_city_leaderboard,
    db_get_country_leaderboard,
    db_approve_member_request,
    db_get_org_membership_log,
    db_get_weekly_leaderboard,
    db_update_user_location,
    db_get_user_timezone,
    db_search_schools,
    db_get_cities_for_country,
    db_get_user_favorites,
    db_add_favorite,
    db_remove_favorite,
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
    build_entity_picker_text,
    build_entity_picker_keyboard,
    build_location_status_text,
    format_public_name,
)




from src.rendering.html_views import build_profile_card_text, build_alliance_info_text
from telegram import Update, InputMediaPhoto, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

_WR_FAV_TYPE_FOR_PURPOSE = {"nav_country": "country", "nav_city": "city", "nav_school": "school"}

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


def _build_school_branch_leaderboard_text(schools: list) -> str:
    if not schools:
        return "<h2>🏢 SCHOOL & BRANCH RANKINGS</h2>\n<i>No data yet.</i>"
    lines = ["<h2>🏢 SCHOOL &amp; BRANCH RANKINGS</h2>", "<hr/>"]
    medals = ["🥇", "🥈", "🥉"]
    for i, s in enumerate(schools):
        rank = medals[i] if i < 3 else str(i+1)
        lines.append(f"{rank} <b>{html.escape(s['org_name'])}</b> — {s['total_score']} marks ({s['member_count']} members)")
        for b in s.get('branches', []):
            lines.append(f"   🏢 {html.escape(b['branch_name'])} ({html.escape(b.get('city') or '—')}) — {b.get('branch_score', 0)} marks")
    return "\n".join(lines)


def _build_feedback_detail_keyboard(fb_id, return_state: str = None) -> InlineKeyboardMarkup:
    rs = return_state or "all:all:0"
    rows = [
        [InlineKeyboardButton("🔧 ACTIVE", callback_data=f"fb_status|{fb_id}|in_progress|{rs}"),
         InlineKeyboardButton("🗓️ PLANNED", callback_data=f"fb_status|{fb_id}|planned|{rs}")],
        [InlineKeyboardButton("✅ RESOLVED", callback_data=f"fb_status|{fb_id}|resolved|{rs}"),
         InlineKeyboardButton("🚫 WON'T FIX", callback_data=f"fb_status|{fb_id}|wontfix|{rs}")],
        [InlineKeyboardButton("💬 REPLY", callback_data=f"fb_reply|{fb_id}")],
    ]
    parts = rs.split(":")
    if len(parts) == 3:
        cat, stat, off = parts
        rows.append([InlineKeyboardButton("🔙 QUEUE", callback_data=f"fb_browse|{cat}|{stat}:{off}")])
    return InlineKeyboardMarkup(rows)

def _build_country_index_kb() -> InlineKeyboardMarkup:
    from src.geo import COUNTRY_NAMES
    letters = sorted(set(c[0] for c in COUNTRY_NAMES))
    rows, row = [], []
    for l in letters:
        row.append(InlineKeyboardButton(l, callback_data=f"regloc_az|{l}"))
        if len(row) == 7:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🔙 BACK", callback_data="settings_menu|0")])
    return InlineKeyboardMarkup(rows)

_WR_FAV_TYPE_FOR_PURPOSE = {"nav_country": "country", "nav_city": "city", "nav_school": "school"}


def _build_wrsel_country_index_kb(purpose: str, favorites: list = None) -> InlineKeyboardMarkup:
    from src.geo import COUNTRY_NAMES
    rows = []
    if favorites:
        for f in favorites[:6]:
            rows.append([InlineKeyboardButton(f"⭐ {f['fav_label']}", callback_data=f"wrsel_fav_go|{purpose}|{f['fav_value']}")])
    letters = sorted(set(c[0] for c in COUNTRY_NAMES))
    row = []
    for l in letters:
        row.append(InlineKeyboardButton(l, callback_data=f"wrsel_letter|{purpose}|{l}"))
        if len(row) == 7:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⭐ MANAGE FAVORITES", callback_data="wr_fav_menu|0")])
    rows.append([InlineKeyboardButton("🔙 BACK TO LEADERBOARD", callback_data="wr|world|_|all|all|all|total|none|0")])
    return InlineKeyboardMarkup(rows)


def _build_wrsel_country_letter_kb(purpose: str, letter: str) -> InlineKeyboardMarkup:
    from src.geo import COUNTRY_NAMES
    matches = sorted(c for c in COUNTRY_NAMES if c.startswith(letter))
    rows = [[InlineKeyboardButton(c, callback_data=f"wrsel_ctry_go|{purpose}|{c}")] for c in matches]
    rows.append([InlineKeyboardButton("🔤 A-Z", callback_data=f"wrsel_ctry|{purpose}|0")])
    return InlineKeyboardMarkup(rows)

def _build_country_letter_kb(letter: str) -> InlineKeyboardMarkup:
    from src.geo import COUNTRY_NAMES
    matches = sorted(c for c in COUNTRY_NAMES if c.startswith(letter))
    rows = [[InlineKeyboardButton(c, callback_data=f"regloc_country|{c}")] for c in matches]
    rows.append([InlineKeyboardButton("🔤 A-Z", callback_data="regloc_start|0")])
    return InlineKeyboardMarkup(rows)


def _build_city_kb(cities: list) -> InlineKeyboardMarkup:
    rows, row = [], []
    for c in cities[:20]:
        row.append(InlineKeyboardButton(c, callback_data=f"regloc_city|{c}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✍️ REGISTER CITY", callback_data="regloc_city_type|0")])
    rows.append([InlineKeyboardButton("🔙 BACK", callback_data="regloc_start|0")])
    return InlineKeyboardMarkup(rows)


def _build_school_kb(schools: list, offset: int = 0, total: int = None, country: str = None) -> InlineKeyboardMarkup:
    total = total if total is not None else len(schools)
    rows = [[InlineKeyboardButton(f"🏫 {s['org_name']}", callback_data=f"regloc_school|{s['org_id']}")] for s in schools]
    nav_row = []
    if offset > 0:
        nav_row.append(InlineKeyboardButton("⬅️ PREV", callback_data=f"regloc_school_page|{max(0, offset-10)}"))
    if offset + 10 < total:
        nav_row.append(InlineKeyboardButton("NEXT ➡️", callback_data=f"regloc_school_page|{offset+10}"))
    if nav_row:
        rows.append(nav_row)
    rows.append([InlineKeyboardButton("✨ REGISTER SCHOOL", callback_data="regloc_school_create|0")])
    rows.append([InlineKeyboardButton("🚫 NOT A STUDENT", callback_data="regloc_school_skip|0")])
    back_cb = f"regloc_country|{country}" if country else "regloc_start|0"
    rows.append([InlineKeyboardButton("🔙 BACK", callback_data=back_cb)])
    return InlineKeyboardMarkup(rows)


def _school_kb_back_country(session: dict) -> str:
    return session.get("reg_country") or ""


async def _regloc_show_school_step(context, chat_id, message_id, user_id, offset: int = 0):
    from src.database import db_search_schools
    session = USER_PAYLOADS.get(user_id, {})
    city, country = session.get("reg_city"), session.get("reg_country")
    all_schools = await asyncio.to_thread(db_search_schools, None, city, country, 200)
    page = all_schools[offset:offset + 10]

    if all_schools:
        html_content = f"<h2>🏫 Schools in {html.escape(city or '')}</h2>\n\nPick your school from the list:"
    else:
        html_content = f"<h2>🏫 Schools in {html.escape(city or '')}</h2>\n\nNo schools on file yet — register yours below:"

    kb = _build_school_kb(page, offset, len(all_schools), country)
    await edit_rich_message_safe(context.bot, chat_id=chat_id, message_id=message_id, html_content=html_content, reply_markup=kb)

async def _regloc_show_review(context, chat_id, message_id, user_id):
    """Final confirm screen — shows exactly what's about to be saved, WITH a clear before → after
    for anything that's actually changing, flags anything that will land as pending, warns about
    what happens to old scores, and requires an explicit CONFIRM before anything is written."""
    from src.database import db_get_user_profile
    session = USER_PAYLOADS.get(user_id, {})
    city = session.get("reg_city") or "Not set"
    country = session.get("reg_country") or "Not set"
    city_is_new = session.get("reg_city_is_new", False)
    school_name = session.get("reg_school_name")
    school_is_new = session.get("reg_school_is_new", False)
    school_org_id = session.get("reg_school_org_id")
    leaving_school = session.get("reg_leave_school", False)

    profile = await asyncio.to_thread(db_get_user_profile, user_id)
    old_country = profile.get("personal_country") if profile else None
    old_city = profile.get("personal_city") if profile else None
    old_org_name = profile.get("org_name") if profile else None

    lines = ["📋 <b>REVIEW YOUR PROFILE SETUP</b>\n"]

    if old_country and old_country != country:
        lines.append(f"🌍 <b>Country:</b> {html.escape(old_country)} → <b>{html.escape(country)}</b>")
    else:
        lines.append(f"🌍 <b>Country:</b> {html.escape(country)}")

    if old_city and old_city != city:
        pending_tag = " — ⏳ <i>pending admin approval</i>" if city_is_new else " — ✅ recognized"
        lines.append(f"🏙️ <b>City:</b> {html.escape(old_city)} → <b>{html.escape(city)}</b>{pending_tag}")
    elif city_is_new:
        lines.append(f"🏙️ <b>City:</b> {html.escape(city)} — ⏳ <i>pending admin approval</i>")
    else:
        lines.append(f"🏙️ <b>City:</b> {html.escape(city)} — ✅ recognized")

    if leaving_school and old_org_name:
        lines.append(f"🏫 <b>School:</b> {html.escape(old_org_name)} → <b>Removed</b>")
    elif school_name and old_org_name and school_name != old_org_name:
        pending_tag = " — ⏳ <i>pending admin approval</i>" if school_is_new else " — ✅ recognized"
        lines.append(f"🏫 <b>School:</b> {html.escape(old_org_name)} → <b>{html.escape(school_name)}</b>{pending_tag}")
    elif school_name:
        pending_tag = " — ⏳ <i>pending admin approval</i>" if school_is_new else " — ✅ recognized"
        lines.append(f"🏫 <b>School:</b> {html.escape(school_name)}{pending_tag}")
    else:
        lines.append("🏫 <b>School:</b> <i>Not a student — you can add this anytime from 📍 LOCATIONS & SCHOOL</i>")

    changing_something = (old_country and old_country != country) or (old_city and old_city != city) or leaving_school or (school_name and old_org_name and school_name != old_org_name)
    if changing_something:
        lines.append(
            "\n<blockquote>📌 <b>Your marks always stay where you earned them.</b> Whatever you scored "
            "under your old city/country/school stays credited there exactly as it is — it never moves. "
            "From the moment you confirm, only your NEW correct answers count toward whatever's shown "
            "above. Nothing about your personal total changes either way.</blockquote>"
        )

    if city_is_new or school_is_new:
        lines.append(
            "\n<blockquote>⚠️ Anything marked <b>pending</b> gets saved to your profile AND sent to admins "
            "for review only when you tap CONFIRM &amp; SAVE below — nothing was sent while you were "
            "filling this out. Your score stays <b>personal only</b> on anything pending until an admin "
            "approves it; we'll message you the moment that happens.</blockquote>"
        )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ CONFIRM & SAVE", callback_data="regloc_confirm|0")],
        [InlineKeyboardButton("🔙 START OVER", callback_data="regloc_start|0")],
        [InlineKeyboardButton("❌ CANCEL", callback_data="fsm_cancel|privacy_menu")]
    ])
    await edit_rich_message_safe(context.bot, chat_id=chat_id, message_id=message_id, html_content="\n".join(lines), reply_markup=kb)


async def _regloc_finish(context, chat_id, message_id, user_id, school_msg: str = None):
    """Actually commits the reviewed selections. Only ever called from the regloc_confirm branch."""
    from src.database import db_update_user_location, db_create_location_suggestion, db_set_user_pending_city, db_get_all_admin_ids, db_leave_organization
    session = USER_PAYLOADS.pop(user_id, {})
    USER_STATES[user_id] = "IDLE"

    if session.get("reg_leave_school"):
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        old_org_id = profile.get("org_id") if profile else None
        old_org_name = profile.get("org_name") if profile else None
        if old_org_id:
            await asyncio.to_thread(db_leave_organization, user_id, old_org_id)
            school_msg = (
                f"🚪 <b>Removed from {html.escape(old_org_name or 'your previous team')}.</b> "
                f"Everything you earned there stays on that team's board."
            )

    city, country = session.get("reg_city"), session.get("reg_country")
    city_is_new = session.get("reg_city_is_new", False)
    existing_sid = session.get("reg_city_suggestion_id")

    if city or country:
        if city_is_new and not existing_sid:
            # This IS the single point where a new city gets created and admins get notified —
            # only ever reached from the final regloc_confirm tap, after the user has reviewed
            # the whole setup (city + school together) on the review screen.
            sid = await asyncio.to_thread(db_create_location_suggestion, "city", city, country, user_id)
            await asyncio.to_thread(db_set_user_pending_city, user_id, city, country, sid)
            admin_ids = await asyncio.to_thread(db_get_all_admin_ids)
            req_name = html.escape((await context.bot.get_chat(user_id)).first_name or "A student")
            review_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ APPROVE", callback_data=f"loc_review|{sid}|1"),
                InlineKeyboardButton("🚫 REJECT", callback_data=f"loc_review|{sid}|0")
            ], [
                InlineKeyboardButton("💬 ASK USER", callback_data=f"loc_review_msg|{sid}"),
                InlineKeyboardButton("⏳ PENDING QUEUE", callback_data=f"loc_review|{sid}|-1")
            ]])
            for admin_id in admin_ids:
                try:
                    await context.bot.send_message(chat_id=int(admin_id),
                        text=f"📍 <b>NEW CITY SUGGESTION</b>\n\n<b>{req_name}</b> set their city to <b>{html.escape(city)}, {html.escape(country)}</b>.",
                        reply_markup=review_kb, parse_mode="HTML")
                except Exception:
                    pass
        elif not city_is_new:
            await asyncio.to_thread(db_update_user_location, user_id, city or "Not set", country or "Not set")
        # else: city_is_new and existing_sid — already created & admins already notified at accept-pending. No-op.

    profile_nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("👤 PROFILE", callback_data="privacy_menu|0")]])
    status_line = "⏳ pending admin review" if city_is_new else "✅ saved"
    await edit_rich_message_safe(
        context.bot, chat_id=chat_id, message_id=message_id,
        html_content=(
            f"✅ <b>Setup complete!</b>\n\n📍 {city or '—'}, {country or '—'} ({status_line})\n{school_msg or ''}\n\n"
            f"📌 <i>Your marks always stay with the city/country/school you earned them in — changing any of "
            f"these later never moves old marks, it just starts a fresh total on the new one.</i>"
        ),
        reply_markup=profile_nav_kb
    )


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

async def _notify_org_admins_pending_request(context, org_id, org_name, requester):
    from src.database import db_get_org_admin_ids, db_get_pending_org_requests
    admin_ids = await asyncio.to_thread(db_get_org_admin_ids, org_id)
    if not admin_ids:
        return

    conn = engine_ref = None
    request_count, last_requested_at = 1, None
    from src.database import GLOBAL_ENGINE
    conn = GLOBAL_ENGINE.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT request_count, last_requested_at FROM org_memberships WHERE user_id = %s AND org_id = %s;", (str(requester.id), int(org_id)))
            row = cur.fetchone()
            if row:
                request_count = row['request_count'] or 1
                last_requested_at = row['last_requested_at']
    finally:
        GLOBAL_ENGINE.release_connection(conn)

    from src.geo import format_local_time
    from src.database import db_get_user_snapshot
    last_req_str = format_local_time(last_requested_at) if last_requested_at else "just now"
    req_name = html.escape(requester.first_name or requester.username or "A student")

    snap = await asyncio.to_thread(db_get_user_snapshot, requester.id)
    acc = int((snap.get('correct', 0) / snap['total']) * 100) if snap.get('total') else 0
    snapshot_line = (
        f"\n📊 Grade {snap.get('grade') or '—'} · {snap.get('total_marks', 0)} marks · "
        f"🎯 {acc}% ({snap.get('correct', 0)}/{snap.get('total', 0)}) · 🔥 {snap.get('current_streak', 0)}d streak"
        f"\n📍 {snap.get('personal_city') or '—'}, {snap.get('personal_country') or '—'}"
    ) if snap else ""

    approve_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🟢 APPROVE", callback_data=f"process_req|{org_id}|{requester.id}|1"),
        InlineKeyboardButton("🔴 REJECT", callback_data=f"process_req|{org_id}|{requester.id}|0")
    ], [
        InlineKeyboardButton("⏳ KEEP PENDING", callback_data=f"process_req|{org_id}|{requester.id}|-1")
    ]])

    repeat_note = f"\n📈 Requested <b>{request_count}×</b>, last on {last_req_str}." if request_count > 1 else ""

    for admin_id in admin_ids:
        try:
            await context.bot.send_message(
                chat_id=int(admin_id),
                text=f"📥 <b>NEW JOIN REQUEST</b>\n\n<b>{req_name}</b> wants to join <b>{html.escape(org_name)}</b>.{repeat_note}{snapshot_line}",
                reply_markup=approve_kb, parse_mode="HTML"
            )
        except Exception:
            pass

# Local lock registry for this module — mirrors bot.py's _UTILITY_LOCKS.
# Needed because callbacks.py is a separate module; it cannot reach bot.py's
# private _open_utility_view/_UTILITY_LOCKS directly.
_UTILITY_LOCKS: dict = {}

async def _open_utility_view(context, user_id, chat_id, html_content, reply_markup=None):
    """Ensures only ONE profile-family message (profile/settings/leaderboard/invite/help/
    feedback/team/my-answers/locations wizard/etc) is ever visible in this user's DM at a
    time. Tracked in the DATABASE (db_get_last_utility_mid / db_set_last_utility_mid) so it
    survives restarts and stays correct across both bot.py and callbacks.py call sites.
    NEVER touches answer-explanation cards from the channel — those are tracked separately
    via db_update_private_message_id and are never written to this tracker.

    This was missing from callbacks.py entirely, which caused a silent NameError every time
    the 👤 PROFILE button was tapped from an answer-explanation card in the DM — the tap
    looked like it did nothing."""
    from src.rendering.rich_helpers import open_utility_view
    return await open_utility_view(context.bot, None, _UTILITY_LOCKS, user_id, chat_id, html_content, reply_markup)


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
                [InlineKeyboardButton(f"✅ SWITCH TO {grade}", callback_data=f"confirm_grade|{grade}")],
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
        if not previous_grade:
            msg = f"✅ <b>Grade {grade} Registered!</b>\n\nSet your country, city & school now? Optional, editable later."
            confirm_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🌍 SET LOCATION", callback_data="regloc_start|0")],
                [InlineKeyboardButton("⏭ SKIP", callback_data="privacy_menu|0")]
            ])
        else:
            msg = f"✅ <b>Grade Updated:</b> {previous_grade} → <b>{grade}</b>"
            confirm_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 CHANGE AGAIN", callback_data="reselect_grade_panel|0")],
                [InlineKeyboardButton("👤 PROFILE", callback_data="privacy_menu|0")]
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
        from src.database import db_set_last_utility_mid
        await asyncio.to_thread(db_set_last_utility_mid, user_id, query.message.message_id)
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        subject_marks = await asyncio.to_thread(db_get_user_subject_marks, user_id)
        text = build_profile_card_text(profile, None, subject_marks)
        kb = build_profile_main_keyboard(has_team=bool(profile.get("org_id")))
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        return

    elif action == "profile_popup":
        await query.answer()
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        if not profile or not profile.get("grade"):
            nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🎒 SET UP MY PROFILE", callback_data="regloc_start_new|0")]])
            await _open_utility_view(
                context, user_id, query.message.chat_id,
                "🎒 You haven't finished setup yet. Type /start to register your grade first.",
                nav_kb
            )
            return
        subject_marks = await asyncio.to_thread(db_get_user_subject_marks, user_id)
        text = build_profile_card_text(profile, None, subject_marks)
        kb = build_profile_main_keyboard(has_team=bool(profile.get("org_id")))
        await _open_utility_view(context, user_id, query.message.chat_id, text, kb)
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
        await query.answer()

        if not consent_state:
            explain = (
                "<h3>🔴 Going Private</h3>\n"
                "<blockquote>"
                "Your name disappears from leaderboards and round podiums. Others see a stable "
                "anonymous ID like <code>Scholar ...4821</code> instead.\n\n"
                "Your scores and rank still count — only your identity is hidden."
                "</blockquote>\n"
                "Confirm to go private?"
            )
            warn_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ CONFIRM", callback_data="confirm_consent|0"),
                 InlineKeyboardButton("❌ CANCEL", callback_data="settings_menu|0")]
            ])
            await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=explain, reply_markup=warn_kb)
            return

        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        has_nickname = bool(profile and profile.get("nickname"))

        if not has_nickname:
            explain = (
                "<h3>🟢 Going Public — pick a name first</h3>\n"
                "<blockquote>"
                "You haven't set a nickname yet. We <b>strongly recommend</b> a nickname — it lets you "
                "show up on leaderboards without ever revealing your real Telegram username or name.\n\n"
                "If you'd rather show your real Telegram identity instead, that's a separate, explicit "
                "choice below — nothing is shown by default."
                "</blockquote>"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✍️ SET A NICKNAME (RECOMMENDED)", callback_data="set_nick_fsm|0")],
                [InlineKeyboardButton("🆔 Use my real Telegram identity instead", callback_data="reveal_identity_warn|0")],
                [InlineKeyboardButton("❌ CANCEL", callback_data="settings_menu|0")]
            ])
            await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=explain, reply_markup=kb)
            return

        explain = (
            "<h3>🟢 Going Public</h3>\n"
            "<blockquote>"
            f"You already have a nickname set (<b>{html.escape(profile.get('nickname'))}</b>) — that's "
            "what shows on leaderboards and podiums. Your real Telegram username/name stays hidden. "
            "No need to reveal it unless you specifically choose to below."
            "</blockquote>\n"
            "Confirm to go public with your nickname?"
        )
        warn_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ CONFIRM (use nickname)", callback_data="confirm_consent|1")],
            [InlineKeyboardButton("🆔 Use real identity instead", callback_data="reveal_identity_warn|0")],
            [InlineKeyboardButton("❌ CANCEL", callback_data="settings_menu|0")]
        ])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=explain, reply_markup=warn_kb)
        return

    elif action == "reveal_identity_warn":
        await query.answer()
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        handle = f"@{profile.get('username')}" if profile.get("username") else (profile.get("first_name") or "your name")
        explain = (
            "<h3>⚠️ Reveal your real Telegram identity?</h3>\n"
            "<blockquote>"
            f"This will show <b>{html.escape(handle)}</b> — your actual Telegram handle — to every "
            "student on leaderboards and round podiums, instead of a nickname or anonymous ID.\n\n"
            "You can turn this off again anytime from Settings."
            "</blockquote>\n"
            "Are you sure?"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚠️ YES, SHOW MY REAL IDENTITY", callback_data="confirm_reveal_identity|1")],
            [InlineKeyboardButton("❌ NO, GO BACK", callback_data="settings_menu|0")]
        ])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=explain, reply_markup=kb)
        return

    elif action == "confirm_reveal_identity":
        from src.database import db_set_show_real_identity
        await asyncio.to_thread(db_set_show_real_identity, user_id, True)
        await asyncio.to_thread(db_update_user_consent_state, user_id, True)
        await query.answer("Real identity enabled.")
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        kb = build_profile_settings_keyboard(True)
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content="🎛️ <b>SETTINGS</b>\n<hr/>\nVisibility, nickname, grade, or location.", reply_markup=kb)
        return

    elif action == "confirm_consent":
        from src.database import db_set_show_real_identity
        consent_state = (d_id == "1")
        await asyncio.to_thread(db_update_user_consent_state, user_id, consent_state)
        if not consent_state:
            await asyncio.to_thread(db_set_show_real_identity, user_id, False)
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

    elif action == "confirm_change":
        display_id, new_opt = int(d_id), int(data[2])
        from src.database import db_is_tournament_round_still_open, db_edit_tournament_answer
        track, question_data = await asyncio.to_thread(db_get_track_and_question, display_id)
        if not track or not question_data:
            await query.answer("This round is no longer available.", show_alert=True)
            return

        still_open = await asyncio.to_thread(db_is_tournament_round_still_open, track['message_id'])
        if not still_open:
            await query.answer("⏳ Time's up! Your original answer has been locked in.", show_alert=True)
            letters = ["A", "B", "C", "D", "E"]
            existing_response = await asyncio.to_thread(db_get_user_response, user_id, track['message_id'])
            old_opt = existing_response['selected_option'] if existing_response else new_opt
            nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("👤 MY PROFILE", callback_data="privacy_menu|0")]])
            try:
                await context.bot.delete_message(chat_id=query.message.chat_id, message_id=query.message.message_id)
            except Exception:
                pass
            await send_rich_message_safe(
                context.bot, chat_id=query.message.chat_id,
                html_content=(
                    f"✅ <b>Answer updated to {letters[new_opt]}</b> for REF <code>{display_id}</code>.\n\n"
                    f"{flip_note}\n\n"
                    f"The full explanation lands here automatically once the round wraps up."
                ),
                reply_markup=nav_kb
            )
            return

        is_correct = (new_opt == question_data['correct_option'])
        result = await asyncio.to_thread(db_edit_tournament_answer, user_id, track['message_id'], new_opt, is_correct)
        await query.answer("✅ Answer changed!")

        letters = ["A", "B", "C", "D", "E"]
        flip_note = {
            "helped": "🎉 Good call — that flipped you from wrong to right!",
            "hurt": "😬 Ouch — that flipped you from right to wrong.",
            "neutral": "Noted — your correctness didn't change."
        }.get(result.get("o_result_flip") if result else "neutral", "")
        nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("👤 MY PROFILE", callback_data="privacy_menu|0")]])
        try:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=query.message.message_id)
        except Exception:
            pass
        await send_rich_message_safe(
            context.bot, chat_id=query.message.chat_id,
            html_content=(
                f"✅ <b>Answer updated to {letters[new_opt]}</b> for REF <code>{display_id}</code>.\n\n"
                f"{flip_note}\n\n"
                f"The full explanation lands here automatically once the round wraps up."
            ),
            reply_markup=nav_kb
        )
        return

    elif action == "cancel_change":
        await query.answer("Kept your original answer.")
        nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("👤 MY PROFILE", callback_data="privacy_menu|0")]])
        try:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=query.message.message_id)
        except Exception:
            pass
        await send_rich_message_safe(
            context.bot, chat_id=query.message.chat_id,
            html_content="✅ No change made — your original answer stays as submitted.",
            reply_markup=nav_kb
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

    elif action == "loc_status_menu":
        await query.answer()
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 SET / CHANGE LOCATION", callback_data="regloc_start|0")],
            [InlineKeyboardButton("🔙 BACK TO SETTINGS", callback_data="settings_menu|0")]
        ])
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=build_location_status_text(profile), reply_markup=kb
        )
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

    elif action == "regloc_start":
        await query.answer()
        USER_STATES[user_id] = "IDLE"
        USER_PAYLOADS[user_id] = {"edit_mid": query.message.message_id}
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content="🌍 <b>YOUR COUNTRY</b>\n\nTap the first letter:",
            reply_markup=_build_country_index_kb()
        )
        return

    elif action == "regloc_az":
        await query.answer()
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=f"🌍 <b>COUNTRIES: {d_id}</b>",
            reply_markup=_build_country_letter_kb(d_id)
        )
        return

    elif action == "regloc_country":
        await query.answer()
        from src.geo import normalize_country_input
        country, _ = normalize_country_input(d_id)
        USER_PAYLOADS.setdefault(user_id, {})["reg_country"] = country
        from src.database import db_get_cities_for_country
        cities = await asyncio.to_thread(db_get_cities_for_country, country)
        if cities:
            await edit_rich_message_safe(
                context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
                html_content=f"<h2>🏙️ Cities in {html.escape(country)}</h2>\n\nPick your city from the list, or tap Register City to add yours:",
                reply_markup=_build_city_kb(cities)
            )
        else:
            USER_STATES[user_id] = "AWAITING_REGLOC_CITY_TEXT"
            back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data="regloc_start|0")]])
            await edit_rich_message_safe(
                context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
                html_content=f"<h2>🏙️ Cities in {html.escape(country)}</h2>\n\nNo cities on file yet — type yours below:" + FSM_INPUT_HINT,
                reply_markup=back_kb
            )
        return

    elif action == "regloc_city_type":
        await query.answer()
        USER_STATES[user_id] = "AWAITING_REGLOC_CITY_TEXT"
        session = USER_PAYLOADS.get(user_id, {})
        country = session.get("reg_country", "")
        back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data=f"regloc_country|{country}")]])
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=f"✍️ <b>Register your city in {html.escape(country)}</b>\n\nType your city name:" + FSM_INPUT_HINT,
            reply_markup=back_kb
        )
        return

    elif action == "regloc_skip_city":
        await query.answer()
        await _regloc_show_school_step(context, query.message.chat_id, query.message.message_id, user_id)
        return

    elif action == "regloc_city":
        await query.answer()
        USER_PAYLOADS.setdefault(user_id, {})["reg_city"] = d_id
        await _regloc_show_school_step(context, query.message.chat_id, query.message.message_id, user_id)
        return

    elif action == "regloc_school_search":
        await query.answer()
        USER_STATES[user_id] = "AWAITING_REGLOC_SCHOOL_SEARCH"
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content="🔍 Type school name:" + FSM_INPUT_HINT,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏭ SKIP", callback_data="regloc_school_skip|0")]])
        )
        return

    elif action == "regloc_school":
        await query.answer()
        USER_PAYLOADS.setdefault(user_id, {})
        USER_PAYLOADS[user_id]["reg_school_org_id"] = int(d_id)
        USER_PAYLOADS[user_id]["reg_school_is_new"] = False
        from src.database import GLOBAL_ENGINE
        conn = GLOBAL_ENGINE.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT org_name FROM organizations WHERE org_id = %s;", (int(d_id),))
                row = cur.fetchone()
                USER_PAYLOADS[user_id]["reg_school_name"] = row["org_name"] if row else None
        finally:
            GLOBAL_ENGINE.release_connection(conn)
        await _regloc_show_review(context, query.message.chat_id, query.message.message_id, user_id)
        return

    elif action == "regloc_school_skip":
        await query.answer()
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        USER_PAYLOADS.setdefault(user_id, {})
        USER_PAYLOADS[user_id]["reg_school_name"] = None
        USER_PAYLOADS[user_id]["reg_school_org_id"] = None
        USER_PAYLOADS[user_id]["reg_school_is_new"] = False
        # If they already have a school and just picked "Not a student", that's an
        # explicit removal — flagged here so the review screen shows it clearly and
        # _regloc_finish actually acts on it (leaving the old team).
        USER_PAYLOADS[user_id]["reg_leave_school"] = bool(profile and profile.get("org_id"))
        await _regloc_show_review(context, query.message.chat_id, query.message.message_id, user_id)
        return

    elif action == "regloc_school_create":
        await query.answer()
        session = USER_PAYLOADS.get(user_id, {})
        USER_STATES[user_id] = "AWAITING_ORG_NAME"
        USER_PAYLOADS[user_id] = {
            "edit_mid": query.message.message_id,
            "org_city": session.get("reg_city"),
            "org_country": session.get("reg_country"),
            "reg_city": session.get("reg_city"),
            "reg_country": session.get("reg_country"),
            "reg_city_is_new": session.get("reg_city_is_new", False),
            "reg_city_suggestion_id": session.get("reg_city_suggestion_id"),
        }
        back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data="regloc_school_page|0")]])
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content="✍️ <b>NEW SCHOOL NAME</b>" + FSM_INPUT_HINT,
            reply_markup=back_kb
        )
        return
    
    elif action == "regloc_school_page":
        await query.answer()
        offset = int(d_id)
        await _regloc_show_school_step(context, query.message.chat_id, query.message.message_id, user_id, offset)
        return
    
    elif action == "regloc_city_pending_ack":
        # No DB writes here on purpose — the city stays STAGED in USER_PAYLOADS (already
        # set by AWAITING_REGLOC_CITY_TEXT) and is only ever actually created/sent to
        # admins from _regloc_finish, once the user reviews and confirms the WHOLE setup
        # (city + school together) at the final step. Tapping "accept & continue" here is
        # purely navigation to the school step.
        await query.answer()
        await _regloc_show_school_step(context, query.message.chat_id, query.message.message_id, user_id)
        return

    elif action == "regloc_skip_all":
        await query.answer()
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        subject_marks = await asyncio.to_thread(db_get_user_subject_marks, user_id)
        text = build_profile_card_text(profile, None, subject_marks)
        kb = build_profile_main_keyboard(has_team=bool(profile.get("org_id")))
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        return

    elif action == "regloc_confirm":
        await query.answer()
        session = USER_PAYLOADS.get(user_id, {})
        school_org_id = session.get("reg_school_org_id")
        school_name = session.get("reg_school_name")
        school_msg = ""

        if school_org_id:
            join_data = await asyncio.to_thread(db_join_organization_by_id, user_id, school_org_id)
            school_msg = f"✅ Joined <b>{join_data['org_name']}</b>!" if join_data else "⚠️ Could not join school."
        elif school_name and session.get("reg_school_is_new"):
            from src.database import db_create_location_suggestion, db_get_all_admin_ids
            org_tag = session.get("reg_new_org_tag")
            reg_city = session.get("reg_city")
            reg_country = session.get("reg_country")
            try:
                new_org_id = await asyncio.to_thread(
                    db_create_organization, school_name, org_tag, user_id,
                    "School", True, reg_city, reg_country, "pending"
                )
                sid = await asyncio.to_thread(db_create_location_suggestion, "school", school_name, reg_country, user_id, new_org_id)
                admin_ids = await asyncio.to_thread(db_get_all_admin_ids)
                req_name = html.escape((await context.bot.get_chat(user_id)).first_name or "A student")
                review_kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ APPROVE", callback_data=f"loc_review|{sid}|1"),
                    InlineKeyboardButton("🚫 REJECT", callback_data=f"loc_review|{sid}|0")
                ], [
                    InlineKeyboardButton("💬 ASK USER", callback_data=f"loc_review_msg|{sid}"),
                    InlineKeyboardButton("⏳ PENDING QUEUE", callback_data=f"loc_review|{sid}|-1")
                ]])
                for admin_id in admin_ids:
                    try:
                        await context.bot.send_message(
                            chat_id=int(admin_id),
                            text=f"🏫 <b>NEW SCHOOL SUGGESTION</b>\n\n<b>{req_name}</b> created <b>{html.escape(school_name)}</b> ({html.escape(reg_city or '')}, {html.escape(reg_country or '')}).",
                            reply_markup=review_kb, parse_mode="HTML"
                        )
                    except Exception:
                        pass
                school_msg = f"🏫 <b>{html.escape(school_name)}</b> submitted for review — you'll be linked once approved."
            except Exception:
                school_msg = "⚠️ Could not create the new school team — please try again from 📍 LOCATIONS &amp; SCHOOL."

        await _regloc_finish(context, query.message.chat_id, query.message.message_id, user_id, school_msg=school_msg)
        return

    elif action == "loc_user_reply":
        sid = int(d_id)
        await query.answer()
        USER_STATES[user_id] = "AWAITING_USER_LOCATION_REPLY"
        USER_PAYLOADS[user_id] = {"suggestion_id": sid, "edit_mid": query.message.message_id}
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ CANCEL", callback_data="fsm_cancel|privacy_menu")]])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content="✍️ <b>Type your reply:</b>", reply_markup=cancel_kb)
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
        from src.database import db_get_org_rank_summary, db_get_org_grade_breakdown
        rank_summary = await asyncio.to_thread(db_get_org_rank_summary, org_id)
        grade_rows = await asyncio.to_thread(db_get_org_grade_breakdown, org_id)
        text = build_organization_card_text(org_details, roster, sort_field, sort_dir)
        from src.rendering.html_views import build_organization_grade_breakdown_text
        grade_text = build_organization_grade_breakdown_text(grade_rows)
        if grade_text:
            text += f"\n{grade_text}"
        rank_bits = []
        if rank_summary.get("city_rank"):
            rank_bits.append(f"🌆 City #{rank_summary['city_rank']}")
        if rank_summary.get("country_rank"):
            rank_bits.append(f"🌍 Country #{rank_summary['country_rank']}")
        if rank_summary.get("world_rank"):
            rank_bits.append(f"🏆 World #{rank_summary['world_rank']}")
        if rank_bits:
            text = text.replace("<hr/>\n<b>", f"{' · '.join(rank_bits)}\n<hr/>\n<b>", 1)

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
                "👑 <b>You're the Creator</b>\n\n"
                "Leaving hands control to your longest-standing admin automatically. Cannot be undone."
            )
        else:
            warn_text = "⚠️ <b>Leave this team?</b>\n\nYour personal score is unaffected."
        warn_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚪 LEAVE", callback_data=f"leave_org_confirm|{org_id}")],
            [InlineKeyboardButton("❌ CANCEL", callback_data=f"view_org|{org_id}")]
        ])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=warn_text, reply_markup=warn_kb)
        return

    elif action == "leave_org_confirm":
        await query.answer()
        org_id = int(d_id)

        # Snapshot who was leaving + who the admins are BEFORE the leave, since
        # db_leave_organization marks their membership row 'left' immediately.
        leaver_profile = await asyncio.to_thread(db_get_user_profile, user_id)
        leaver_name = format_public_name(leaver_profile) if leaver_profile else "A student"
        from src.database import db_get_org_admin_ids
        admin_ids_before = await asyncio.to_thread(db_get_org_admin_ids, org_id)

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
            msg = (
                "🚪 You left the team. Since you were the creator, leadership was automatically handed "
                "to your longest-standing admin/member.\n\n"
                "📌 <i>Your marks stay credited to this team's board exactly as they were. From here, "
                "your correct answers only count toward whichever team(s) you're currently in.</i>"
            )
        else:
            msg = (
                "🚪 You successfully exited the school team.\n\n"
                "📌 <i>Every mark you earned while on this team stays on its board. Going forward, "
                "your correct answers only count toward your current team(s).</i>"
            )
            # Non-creator leave — notify the remaining admins/creator, since they'd
            # otherwise have no way of knowing their roster shrank.
            for admin_id in admin_ids_before:
                if str(admin_id) == str(user_id):
                    continue
                try:
                    await context.bot.send_message(
                        chat_id=int(admin_id),
                        text=f"🚪 <b>{leaver_name}</b> has left your team.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

        await query.edit_message_text(msg, reply_markup=return_kb, parse_mode="HTML")
        return

    elif action == "dissolve_org_warn":
        await query.answer()
        org_id = int(d_id)
        warn_text = (
            "⚠️ <b>What do you want to do with this team?</b>\n\n"
            "<blockquote>"
            "🚪 <b>Leave Team</b> — you step down as creator; leadership passes to your longest-standing "
            "admin or member automatically. The team keeps running.\n\n"
            "💥 <b>Delete Entire Team</b> — the team is closed for everyone. Members lose their team "
            "membership and it disappears from leaderboards. This cannot be undone by you (only an admin "
            "can restore it)."
            "</blockquote>"
        )
        warn_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚪 LEAVE TEAM (HAND OVER)", callback_data=f"leave_org_warn|{org_id}")],
            [InlineKeyboardButton("💥 DELETE ENTIRE TEAM", callback_data=f"dissolve_org_final_warn|{org_id}")],
            [InlineKeyboardButton("❌ CANCEL", callback_data=f"view_org|{org_id}")]
        ])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=warn_text, reply_markup=warn_kb)
        return

    elif action == "dissolve_org_final_warn":
        await query.answer()
        org_id = int(d_id)
        final_text = (
            "💥 <b>Final confirmation — delete this team?</b>\n\n"
            "<blockquote>This closes the team for every member and removes it from all leaderboards. "
            "It is not fully erased from our records, but it will no longer be usable or visible. "
            "This cannot be reversed from your side.</blockquote>"
        )
        final_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💥 YES, DELETE PERMANENTLY", callback_data=f"dissolve_org_confirm|{org_id}")],
            [InlineKeyboardButton("❌ CANCEL", callback_data=f"view_org|{org_id}")]
        ])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=final_text, reply_markup=final_kb)
        return

    elif action == "dissolve_org_confirm":
        await query.answer()
        org_id = int(d_id)
        await asyncio.to_thread(db_dissolve_organization, org_id)
        nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("👤 GO TO PROFILE", callback_data="privacy_menu|0")]])
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content="💥 Team deleted. All student mappings for it are now inactive.",
            reply_markup=nav_kb
        )
        return

    elif action == "set_nick_fsm":
        await query.answer()
        USER_STATES[user_id] = "AWAITING_NICKNAME"
        USER_PAYLOADS[user_id] = {"edit_mid": query.message.message_id}

        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        current_nick = profile.get("nickname") if profile else None
        current_line = f"📛 <b>Current nickname:</b> {current_nick}\n\n" if current_nick else "📛 <i>No custom nickname set yet.</i>\n\n"

        fsm_cancel_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 BACK TO SETTINGS", callback_data="settings_menu|0")],
            [InlineKeyboardButton("❌ CANCEL & RETURN", callback_data="fsm_cancel|privacy_menu")]
        ])
        await query.edit_message_text(
            (
                "✍️ <b>PROMPT: SCOREBOARD PSEUDONYM</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{current_line}"
                "Please type your NEW display name directly into this chat, then tap ➤ send to submit it.\n\n"
                "⚠️ <b>Simple Rules:</b>\n"
                "├─ Max 20 characters\n"
                "└─ Spaces and underscores allowed"
            ) + FSM_INPUT_HINT,
            reply_markup=fsm_cancel_kb,
            parse_mode="HTML"
        )
        return

    elif action == "confirm_nick":
        await query.answer()
        session = USER_PAYLOADS.get(user_id, {})
        pending = session.get("pending_nickname")
        profile_nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("👤 PROFILE", callback_data="privacy_menu|0")]])
        if d_id == "1" and pending:
            await asyncio.to_thread(db_set_user_nickname, user_id, pending)
            USER_PAYLOADS.pop(user_id, None)
            await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
                html_content=f"✅ <b>Nickname updated:</b> {pending}", reply_markup=profile_nav_kb)
        else:
            USER_PAYLOADS.pop(user_id, None)
            await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
                html_content="❌ Cancelled.", reply_markup=profile_nav_kb)
        return

    elif action == "confirm_location":
        await query.answer()
        session = USER_PAYLOADS.get(user_id, {})
        pending_city = session.get("pending_city")
        pending_country = session.get("pending_country")
        profile_nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("👤 OPEN PROFILE DASHBOARD", callback_data="privacy_menu|0")]])
        if d_id == "1" and pending_city and pending_country:
            await asyncio.to_thread(db_update_user_location, user_id, pending_city, pending_country)
            USER_PAYLOADS.pop(user_id, None)
            await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
                html_content=f"✅ <b>Location updated!</b>\n📍 {pending_city}, {pending_country}", reply_markup=profile_nav_kb)
        else:
            USER_PAYLOADS.pop(user_id, None)
            await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
                html_content="❌ Location change cancelled. Your previous location is unchanged.", reply_markup=profile_nav_kb)
        return

    elif action == "confirm_location_pending":
        await query.answer()
        session = USER_PAYLOADS.get(user_id, {})
        pending_city = session.get("pending_city")
        pending_country = session.get("pending_country")
        profile_nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("👤 OPEN PROFILE DASHBOARD", callback_data="privacy_menu|0")]])
        if d_id == "1" and pending_city and pending_country:
            from src.database import db_create_location_suggestion, db_set_user_pending_city, db_get_all_admin_ids
            sid = await asyncio.to_thread(db_create_location_suggestion, "city", pending_city, pending_country, user_id)
            await asyncio.to_thread(db_set_user_pending_city, user_id, pending_city, pending_country, sid)
            USER_PAYLOADS.pop(user_id, None)

            await edit_rich_message_safe(
                context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
                html_content=f"⏳ <b>Submitted!</b>\n📍 {pending_city}, {pending_country} — pending admin review.",
                reply_markup=profile_nav_kb
            )

            admin_ids = await asyncio.to_thread(db_get_all_admin_ids)
            req_name = html.escape(query.from_user.first_name or query.from_user.username or "A student")
            review_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ APPROVE", callback_data=f"loc_review|{sid}|1"),
                InlineKeyboardButton("🚫 REJECT", callback_data=f"loc_review|{sid}|0")
            ], [
                InlineKeyboardButton("💬 ASK USER", callback_data=f"loc_review_msg|{sid}")
            ]])
            for admin_id in admin_ids:
                try:
                    await context.bot.send_message(
                        chat_id=int(admin_id),
                        text=f"📍 <b>NEW CITY SUGGESTION</b>\n\n<b>{req_name}</b> set their city to <b>{html.escape(pending_city)}, {html.escape(pending_country)}</b> — not in our known list.",
                        reply_markup=review_kb, parse_mode="HTML"
                    )
                except Exception:
                    pass
        else:
            USER_PAYLOADS.pop(user_id, None)
            await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
                html_content="❌ Cancelled.", reply_markup=profile_nav_kb)
        return

    elif action == "loc_review":
        from src.database import db_is_admin, db_resolve_location_suggestion, db_get_location_suggestion
        if not await asyncio.to_thread(db_is_admin, user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        sid, decision = int(d_id), data[2]
        if decision == "-1":
            await query.answer("Left pending.")
            from src.database import db_get_location_suggestions_list, db_count_location_suggestions
            from src.rendering.html_views import build_location_suggestions_browse_list_text
            items = await asyncio.to_thread(db_get_location_suggestions_list, "pending", "all", 6, 0)
            total = await asyncio.to_thread(db_count_location_suggestions, "pending", "all")
            text = build_location_suggestions_browse_list_text(items, "all", "pending", 0, total)
            item_rows = [[InlineKeyboardButton(f"#{ls['id']} · {ls['name'][:26]}", callback_data=f"loc_admin_item|{ls['id']}|all:pending:0")] for ls in items]
            nav_row = []
            if total > 6:
                nav_row.append(InlineKeyboardButton("NEXT ➡️", callback_data="loc_admin_browse|all|pending:6"))
            if nav_row:
                item_rows.append(nav_row)
            item_rows.append([InlineKeyboardButton("🔙 DASHBOARD", callback_data="admin_dashboard|0")])
            try:
                await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=InlineKeyboardMarkup(item_rows))
            except Exception:
                await send_rich_message_safe(context.bot, chat_id=query.message.chat_id, html_content=text, reply_markup=InlineKeyboardMarkup(item_rows))
            return
        approve = (decision == "1")
        result = await asyncio.to_thread(db_resolve_location_suggestion, sid, user_id, approve)
        await query.answer("Approved!" if approve else "Rejected.")
        if result:
            sug = result["suggestion"]
            status_line = f"\n\n{'✅ Approved' if approve else '🚫 Rejected'} by admin."
            try:
                old_text = (query.message.text or query.message.caption or "") + status_line
                back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 QUEUE", callback_data=f"loc_admin_browse|{sug['kind']}|pending:0")]])
                await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=old_text, reply_markup=back_kb)
            except Exception:
                pass
            for uid in result.get("affected_users", []):
                try:
                    nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("👤 MY PROFILE", callback_data="privacy_menu|0")]])
                    if approve:
                        msg = f"✅ <b>Your city was approved!</b>\n📍 {html.escape(sug['name'])}, {html.escape(sug.get('country') or '')} now shows on your profile and leaderboards."
                    else:
                        msg = f"🚫 Your suggested city <b>{html.escape(sug['name'])}</b> wasn't approved. Please update your location with a different spelling from /profile → 📍 LOCATION."
                    await context.bot.send_message(chat_id=int(uid), text=msg, parse_mode="HTML", reply_markup=nav_kb)
                except Exception:
                    pass
        return

    elif action == "loc_review_msg":
        from src.database import db_is_admin, db_get_location_suggestion
        if not await asyncio.to_thread(db_is_admin, user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        sid = int(d_id)
        sug = await asyncio.to_thread(db_get_location_suggestion, sid)
        if not sug:
            await query.answer("Not found.", show_alert=True)
            return
        await query.answer()
        USER_STATES[user_id] = "AWAITING_ADMIN_LOCATION_REPLY"
        USER_PAYLOADS[user_id] = {"suggestion_id": sid, "target_user_id": sug["submitted_by"], "edit_mid": query.message.message_id}
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ CANCEL", callback_data="fsm_cancel|privacy_menu")]])
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=f"💬 <b>Ask the student about:</b> {html.escape(sug['name'])}, {html.escape(sug.get('country') or '')}\n\nType your question below:",
            reply_markup=cancel_kb
        )
        return


    elif action == "loc_admin_browse":
        from src.database import db_is_admin, db_get_location_suggestions_list, db_count_location_suggestions
        if not await asyncio.to_thread(db_is_admin, user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        await query.answer()

        kind = d_id
        rest = data[2] if len(data) > 2 else "pending:0"
        status, offset_str = rest.split(":", 1) if ":" in rest else (rest, "0")
        offset = int(offset_str) if offset_str.isdigit() else 0

        items = await asyncio.to_thread(db_get_location_suggestions_list, status, kind, 6, offset)
        total = await asyncio.to_thread(db_count_location_suggestions, status, kind)

        from src.rendering.html_views import build_location_suggestions_browse_list_text
        text = build_location_suggestions_browse_list_text(items, kind, status, offset, total)

        return_state = f"{kind}:{status}:{offset}"
        item_rows = [
            [InlineKeyboardButton(f"#{ls['id']} · {ls['name'][:26]}", callback_data=f"loc_admin_item|{ls['id']}|{return_state}")]
            for ls in items
        ]
        nav_row = []
        if offset > 0:
            nav_row.append(InlineKeyboardButton("⬅️ PREV", callback_data=f"loc_admin_browse|{kind}|{status}:{max(0, offset-6)}"))
        if offset + 6 < total:
            nav_row.append(InlineKeyboardButton("NEXT ➡️", callback_data=f"loc_admin_browse|{kind}|{status}:{offset+6}"))
        if nav_row:
            item_rows.append(nav_row)
        item_rows.append([
            InlineKeyboardButton("🏙 Cities", callback_data=f"loc_admin_browse|city|{status}:0"),
            InlineKeyboardButton("🏫 Schools", callback_data=f"loc_admin_browse|school|{status}:0"),
            InlineKeyboardButton("📋 All", callback_data=f"loc_admin_browse|all|{status}:0"),
        ])
        item_rows.append([
            InlineKeyboardButton("📥 Pending", callback_data=f"loc_admin_browse|{kind}|pending:0"),
            InlineKeyboardButton("✅ Approved", callback_data=f"loc_admin_browse|{kind}|approved:0"),
            InlineKeyboardButton("🚫 Rejected", callback_data=f"loc_admin_browse|{kind}|rejected:0"),
        ])
        item_rows.append([InlineKeyboardButton("🔙 DASHBOARD", callback_data="admin_dashboard|0")])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=InlineKeyboardMarkup(item_rows))
        return

    elif action == "loc_admin_item":
        from src.database import db_is_admin, db_get_location_suggestion, db_get_location_suggestion_thread
        if not await asyncio.to_thread(db_is_admin, user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        ls_id = int(d_id)
        return_state = data[2] if len(data) > 2 else "all:pending:0"
        ls = await asyncio.to_thread(db_get_location_suggestion, ls_id)
        if not ls:
            await query.answer("Not found.", show_alert=True)
            return
        await query.answer()
        thread = await asyncio.to_thread(db_get_location_suggestion_thread, ls_id)

        from src.rendering.html_views import build_location_suggestion_item_text
        text = build_location_suggestion_item_text(ls, thread)

        rows = []
        if ls['status'] == 'pending':
            rows.append([
                InlineKeyboardButton("✅ APPROVE", callback_data=f"loc_review|{ls_id}|1"),
                InlineKeyboardButton("🚫 REJECT", callback_data=f"loc_review|{ls_id}|0")
            ])
            rows.append([InlineKeyboardButton("💬 MESSAGE STUDENT", callback_data=f"loc_review_msg|{ls_id}")])
            rows.append([InlineKeyboardButton("⏳ PENDING QUEUE", callback_data=f"loc_review|{ls_id}|-1")])
        else:
            rows.append([InlineKeyboardButton("💬 MESSAGE STUDENT", callback_data=f"loc_review_msg|{ls_id}")])
        rows.append([InlineKeyboardButton("🔙 QUEUE", callback_data=f"loc_admin_browse|{return_state.split(':')[0]}|{':'.join(return_state.split(':')[1:])}")])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=InlineKeyboardMarkup(rows))
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

    elif action == "close_portal":
        await query.answer("Dashboard closed.")
        await query.delete_message()
        return

    elif action == "menu_leaderboard":
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        if not profile or not profile.get("grade"):
            await query.answer("Please set your grade first via /start.", show_alert=True)
            return
        await query.answer()

        from src.database import db_get_rank_matrix, db_get_scope_summary, db_get_all_subjects
        matrix = await asyncio.to_thread(db_get_rank_matrix, "world", None, "all", "all", "all", "total", 10)
        summary = await asyncio.to_thread(db_get_scope_summary, "world", None, "all")
        text = build_leaderboard_text("world", None, "all", "all", "all", "total", matrix, summary)
        kb = build_leaderboard_keyboard("world", None, "all", "all", "all", "total", "none", [], 0)
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        return

    elif action == "wr":
        parts = data[1:]
        scope = parts[0] if len(parts) > 0 else "world"
        entity = parts[1] if len(parts) > 1 and parts[1] != "_" else None
        grade = parts[2] if len(parts) > 2 else "all"
        subject = parts[3] if len(parts) > 3 else "all"
        difficulty = parts[4] if len(parts) > 4 else "all"
        mode = parts[5] if len(parts) > 5 else "total"
        edit = parts[6] if len(parts) > 6 else "none"
        soff = int(parts[7]) if len(parts) > 7 and parts[7].isdigit() else 0

        if scope in ("country", "city", "school") and not entity:
            await query.answer()
            favorites = await asyncio.to_thread(db_get_user_favorites, user_id, scope)
            purpose = f"nav_{scope}"
            title = {"country": "🌎 SELECT A COUNTRY", "city": "🏙 SELECT YOUR CITY'S COUNTRY", "school": "🏫 SELECT YOUR SCHOOL'S COUNTRY"}[scope]
            subtitle = "" if scope == "country" else "\n<i>Select the country where your city belongs, then we'll narrow it down.</i>"
            await edit_rich_message_safe(
                context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
                html_content=f"<h2>{title}</h2>{subtitle}",
                reply_markup=_build_wrsel_country_index_kb(purpose, favorites)
            )
            return

        await query.answer()
        from src.database import db_get_rank_matrix, db_get_scope_summary, db_get_all_subjects
        matrix = await asyncio.to_thread(db_get_rank_matrix, scope, entity, grade, subject, difficulty, mode, 10)
        summary = await asyncio.to_thread(db_get_scope_summary, scope, entity, grade)
        subjects_list = await asyncio.to_thread(db_get_all_subjects) if edit == "subject" else []

        text = build_leaderboard_text(scope, entity, grade, subject, difficulty, mode, matrix, summary)
        kb = build_leaderboard_keyboard(scope, entity, grade, subject, difficulty, mode, edit, subjects_list, soff)
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        return

    elif action == "wr_pick":
        await query.answer()
        scope = d_id
        parent = data[2] if len(data) > 2 and data[2] != "_" else None
        offset = int(data[3]) if len(data) > 3 and data[3].isdigit() else 0

        from src.database import db_get_entity_list
        parent_for_query = parent if scope in ("city", "school") else None
        items = await asyncio.to_thread(db_get_entity_list, scope, parent_for_query, 200)

        text = build_entity_picker_text(scope, parent)
        back_scope = {"country": "world", "city": "country", "school": "city"}.get(scope, "world")
        kb = build_entity_picker_keyboard(scope, parent, items, offset, back_scope)
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        return

    elif action == "wr_pick_go":
        await query.answer()
        scope = d_id
        entity = data[2]
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content="Loading…", reply_markup=None
        )
        from src.database import db_get_rank_matrix, db_get_scope_summary
        matrix = await asyncio.to_thread(db_get_rank_matrix, scope, entity, "all", "all", "all", "total", 10)
        summary = await asyncio.to_thread(db_get_scope_summary, scope, entity, "all")
        text = build_leaderboard_text(scope, entity, "all", "all", "all", "total", matrix, summary)
        kb = build_leaderboard_keyboard(scope, entity, "all", "all", "all", "total", "none", [], 0)
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        return

    elif action == "wr_fav_menu":
        await query.answer()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌍 COUNTRIES", callback_data="wr_fav_list|country|0")],
            [InlineKeyboardButton("🏙 CITIES", callback_data="wr_fav_list|city|0")],
            [InlineKeyboardButton("🏫 SCHOOLS", callback_data="wr_fav_list|school|0")],
            [InlineKeyboardButton("🔙 LEADERBOARD", callback_data="wr|world|_|all|all|all|total|none|0")]
        ])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content="<h2>⭐ MY FAVORITES</h2>\nPick a category to view, or add a new favorite from inside each list.",
            reply_markup=kb)
        return

    elif action == "wr_fav_list":
        await query.answer()
        fav_type = d_id
        favs = await asyncio.to_thread(db_get_user_favorites, user_id, fav_type)
        icon = {"country": "🌍", "city": "🏙", "school": "🏫"}.get(fav_type, "⭐")
        rows = [[InlineKeyboardButton(f"{icon} {f['fav_label']}", callback_data=f"wr_fav_item|{fav_type}|{f['fav_value']}")] for f in favs]
        rows.append([InlineKeyboardButton("➕ ADD FAVORITE", callback_data=f"wrsel_ctry|fav_{fav_type}|0")])
        rows.append([InlineKeyboardButton("🔙 BACK", callback_data="wr_fav_menu|0")])
        body = f"<h2>{icon} MY FAVORITE {fav_type.upper()}S</h2>"
        if not favs:
            body += "\n<i>None yet — tap add below.</i>"
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=body, reply_markup=InlineKeyboardMarkup(rows))
        return

    elif action == "wr_fav_item":
        await query.answer()
        fav_type, value = d_id, data[2]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 VIEW LEADERBOARD", callback_data=f"wr|{fav_type}|{value}|all|all|all|total|none|0")],
            [InlineKeyboardButton("🗑 REMOVE FAVORITE", callback_data=f"wr_fav_remove|{fav_type}|{value}")],
            [InlineKeyboardButton("🔙 BACK", callback_data=f"wr_fav_list|{fav_type}|0")]
        ])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=f"<h2>⭐ {html.escape(value)}</h2>\nWhat would you like to do?", reply_markup=kb)
        return

    elif action == "wr_fav_remove":
        await query.answer("Removed.")
        fav_type, value = d_id, data[2]
        await asyncio.to_thread(db_remove_favorite, user_id, fav_type, value)
        nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK TO LIST", callback_data=f"wr_fav_list|{fav_type}|0")]])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content="🗑 Removed from your favorites.", reply_markup=nav_kb)
        return
    
    # elif action == "menu_leaderboard":
        #     profile = await asyncio.to_thread(db_get_user_profile, user_id)
        #     if not profile or not profile.get("grade"):
        #         await query.answer("Please set your grade first via /start.", show_alert=True)
        #         return
        #     await query.answer()
        #     rows = await asyncio.to_thread(db_get_weekly_leaderboard, profile['grade'])
        #     text = build_leaderboard_text("grade", rows, profile)
        #     kb = build_leaderboard_keyboard("grade")
        #     await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        #     return

    # elif action == "lb_filter":
        #     await query.answer()
        #     scope = d_id
        #     profile = await asyncio.to_thread(db_get_user_profile, user_id)
        #     if scope == "grade":
        #         active_grade = profile.get('grade') if profile else None
        #         rows = await asyncio.to_thread(db_get_weekly_leaderboard, active_grade) if active_grade else []
        #         text = build_leaderboard_text(scope, rows, profile)
        #         kb = build_leaderboard_keyboard(scope, active_grade)
        #     elif scope == "school":
        #         rows = await asyncio.to_thread(db_get_alliance_leaderboard)
        #         text = build_leaderboard_text(scope, rows, profile)
        #         kb = build_leaderboard_keyboard(scope)
        #     elif scope == "city":
        #         from src.database import db_get_active_cities
        #         cities = await asyncio.to_thread(db_get_active_cities, None)
        #         text = "🌆 <b>PICK A CITY</b>"
        #         kb = build_geo_picker_keyboard(cities, "city") if cities else build_leaderboard_keyboard(scope)
        #     elif scope == "country":
        #         from src.database import db_get_active_countries
        #         countries = await asyncio.to_thread(db_get_active_countries)
        #         text = "🌍 <b>PICK A COUNTRY</b>"
        #         kb = build_geo_picker_keyboard(countries, "country") if countries else build_leaderboard_keyboard(scope)

        #     elif scope == "country_overall":
        #         from src.database import db_get_country_leaderboard
        #         rows = await asyncio.to_thread(db_get_country_leaderboard)
        #         text = build_leaderboard_text(scope, rows, profile)
        #         kb = build_leaderboard_keyboard(scope)

        #     elif scope == "school_branch":
        #         from src.database import db_get_school_with_branches_leaderboard
        #         schools_with_branches = await asyncio.to_thread(db_get_school_with_branches_leaderboard, 10)
        #         text = _build_school_branch_leaderboard_text(schools_with_branches)
        #         kb = build_leaderboard_keyboard(scope)
        #         await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        #         return

        #     else:  # city_overall
        #         from src.database import db_get_city_leaderboard
        #         rows = await asyncio.to_thread(db_get_city_leaderboard)
        #         text = build_leaderboard_text(scope, rows, profile)
        #         kb = build_leaderboard_keyboard(scope)
        #     await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        #     return

    # elif action == "lb_grade":
        #     await query.answer()
        #     grade = int(d_id)
        #     profile = await asyncio.to_thread(db_get_user_profile, user_id)
        #     rows = await asyncio.to_thread(db_get_weekly_leaderboard, grade)
        #     text = build_leaderboard_text("grade", rows, profile, label_override=str(grade))
        #     kb = build_leaderboard_keyboard("grade", grade)
        #     await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        #     return

    # elif action == "lb_city_pick":
        #     await query.answer()
        #     from src.database import db_get_top_users_by_city
        #     rows = await asyncio.to_thread(db_get_top_users_by_city, d_id)
        #     text = build_leaderboard_text("city", rows, None, label_override=d_id)
        #     kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 CITIES", callback_data="lb_filter|city")]])
        #     await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        #     return

    # elif action == "lb_country_pick":
        #     await query.answer()
        #     from src.database import db_get_top_users_by_country
        #     rows = await asyncio.to_thread(db_get_top_users_by_country, d_id)
        #     text = build_leaderboard_text("country", rows, None, label_override=d_id)
        #     kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 COUNTRIES", callback_data="lb_filter|country")]])
        #     await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        #     return

    # elif action == "geo_country_list":
        #     await query.answer()
        #     offset = int(d_id)
        #     from src.database import db_get_countries_ranked, db_count_countries_ranked
        #     from src.rendering.html_views import build_geo_country_list_text, build_geo_country_list_keyboard
        #     rows = await asyncio.to_thread(db_get_countries_ranked, 15, offset)
        #     total = await asyncio.to_thread(db_count_countries_ranked)
        #     text = build_geo_country_list_text(rows, offset, total)
        #     kb = build_geo_country_list_keyboard(rows, offset, total)
        #     await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        #     return

    # elif action == "wr":
        #     await query.answer()
        #     scope = d_id
        #     grade = data[2] if len(data) > 2 else "all"

        #     from src.database import db_get_rank_matrix, db_get_world_summary_counts

        #     grade_val = None if grade == "all" else int(grade)
        #     matrix = await asyncio.to_thread(db_get_rank_matrix, scope, grade_val, 10)
        #     summary = await asyncio.to_thread(db_get_world_summary_counts, grade_val)

        #     text = build_world_rank_text(scope, grade, matrix, summary)
        #     kb = build_world_rank_keyboard(scope, grade)
        #     await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        #     return

    # elif action == "geo_grade_list":
        #     await query.answer()
        #     from src.database import db_get_grade_world_ranked
        #     from src.rendering.html_views import build_geo_grade_list_text, build_geo_grade_list_keyboard
        #     rows = await asyncio.to_thread(db_get_grade_world_ranked, 10, 0)
        #     text = build_geo_grade_list_text(rows)
        #     kb = build_geo_grade_list_keyboard(rows)
        #     await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        #     return

    # elif action == "geo_grade_detail":
        #     await query.answer()
        #     grade = int(d_id)
        #     from src.database import db_get_grade_detail
        #     from src.rendering.html_views import build_geo_grade_detail_text, build_geo_grade_detail_keyboard
        #     detail = await asyncio.to_thread(db_get_grade_detail, grade)
        #     text = build_geo_grade_detail_text(grade, detail)
        #     kb = build_geo_grade_detail_keyboard()
        #     await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        #     return

    # elif action == "geo_country_detail":
        #     await query.answer()
        #     country = d_id
        #     from src.database import db_get_country_detail
        #     from src.rendering.html_views import build_geo_country_detail_text, build_geo_country_detail_keyboard
        #     detail = await asyncio.to_thread(db_get_country_detail, country)
        #     text = build_geo_country_detail_text(country, detail)
        #     kb = build_geo_country_detail_keyboard(country, detail)
        #     await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        #     return

    # elif action == "geo_city_detail":
        #     await query.answer()
        #     city = d_id
        #     country = data[2] if len(data) > 2 else None
        #     from src.database import db_get_city_detail
        #     from src.rendering.html_views import build_geo_city_detail_text, build_geo_city_detail_keyboard
        #     detail = await asyncio.to_thread(db_get_city_detail, city, country)
        #     text = build_geo_city_detail_text(city, country, detail)
        #     kb = build_geo_city_detail_keyboard(city, country, detail)
        #     await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        #     return

    # elif action == "geo_school_list":
        #     await query.answer()
        #     city = d_id
        #     country = data[2] if len(data) > 2 else "all"
        #     offset = int(data[3]) if len(data) > 3 else 0
        #     city_arg = None if city == "all" else city
        #     country_arg = None if country == "all" else country
        #     from src.database import db_get_schools_ranked, db_count_schools_ranked
        #     from src.rendering.html_views import build_geo_school_list_text, build_geo_school_list_keyboard
        #     rows = await asyncio.to_thread(db_get_schools_ranked, city_arg, country_arg, 15, offset)
        #     total = await asyncio.to_thread(db_count_schools_ranked, city_arg, country_arg)
        #     text = build_geo_school_list_text(rows, city, country, offset, total)
        #     kb = build_geo_school_list_keyboard(rows, city, country, offset, total)
        #     await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        #     return

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

        if join_data.get("already_member"):
            text = f"ℹ️ You're already on <b>{join_data['org_name']}</b> as <b>{join_data['role_assigned'].title()}</b>."
        elif join_data.get("already_pending"):
            text = f"📥 Your join request for <b>{join_data['org_name']}</b> is still pending admin approval."
        elif join_data["role_assigned"] == "pending":
            text = f"📥 <b>Request sent!</b> <b>{join_data['org_name']}</b> requires admin approval — you'll be added once confirmed."
        else:
            text = f"✅ <b>You're in!</b> You're now registered under <b>{join_data['org_name']}</b>."
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=return_kb)
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
            [InlineKeyboardButton("📍 LOCATION & SCHOOL REQUESTS", callback_data="loc_admin_browse|all|pending:0")],
            [InlineKeyboardButton("🗂️ FEEDBACK KANBAN", callback_data="fb_kanban|0")],
            [InlineKeyboardButton("📚 ALL QUESTIONS", callback_data="admin_questions|all:all:0")],
            [InlineKeyboardButton("👤 MY PROFILE", callback_data="privacy_menu|0")]
        ])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        return

    elif action == "fb_kanban":
        from src.database import db_is_admin, db_get_feedback_recent_by_status
        if not await asyncio.to_thread(db_is_admin, user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        await query.answer()
        stats = await asyncio.to_thread(db_get_feedback_stats)
        recent_by_status = await asyncio.to_thread(db_get_feedback_recent_by_status, 3)
        from src.rendering.html_views import build_feedback_kanban_text, build_feedback_kanban_keyboard
        text = build_feedback_kanban_text(stats, recent_by_status)
        kb = build_feedback_kanban_keyboard()
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
        viewer_tz = await asyncio.to_thread(db_get_user_timezone, user_id)
        kb = _build_feedback_detail_keyboard(fb_id, return_state)
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=build_feedback_thread_text(fb, thread, viewer_tz), reply_markup=kb)
        return

    elif action == "fb_view":
        fb_id = int(d_id)
        return_offset = data[2] if len(data) > 2 else "0"
        fb = await asyncio.to_thread(db_get_feedback_by_id, fb_id)
        if not fb or str(fb.get("user_id")) != str(user_id):
            await query.answer("Not found.", show_alert=True)
            return
        await query.answer()
        thread = await asyncio.to_thread(db_get_feedback_thread, fb_id)
        viewer_tz = await asyncio.to_thread(db_get_user_timezone, user_id)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 REPLY", callback_data=f"fb_user_reply|{fb_id}|{return_offset}")],
            [InlineKeyboardButton("🔙 BACK TO LIST", callback_data=f"my_feedback|{return_offset}")],
            [InlineKeyboardButton("👤 PROFILE", callback_data="privacy_menu|0")]
        ])
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=build_feedback_thread_text(fb, thread, viewer_tz), reply_markup=kb
        )
        return

    elif action == "fb_user_reply":
        fb_id = int(d_id)
        return_offset = data[2] if len(data) > 2 else "0"
        fb = await asyncio.to_thread(db_get_feedback_by_id, fb_id)
        if not fb or str(fb.get("user_id")) != str(user_id):
            await query.answer("Not found.", show_alert=True)
            return
        await query.answer()
        USER_STATES[user_id] = "AWAITING_USER_FEEDBACK_REPLY"
        USER_PAYLOADS[user_id] = {"fb_id": fb_id, "return_offset": return_offset, "edit_mid": query.message.message_id}
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ CANCEL", callback_data=f"fb_view|{fb_id}|{return_offset}")]])
        try:
            await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content="✍️ <b>Type your reply below:</b>", reply_markup=kb)
        except Exception as e:
            print(f"[FB-REPLY-ERROR] Failed to open reply box for feedback #{fb_id}: {e}", flush=True)
        return

    elif action == "fsm_cancel":
        await query.answer("Cancelled.")
        USER_STATES[user_id] = "IDLE"
        USER_PAYLOADS.pop(user_id, None)
        from src.database import db_set_last_utility_mid
        await asyncio.to_thread(db_set_last_utility_mid, user_id, None)

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
                await asyncio.to_thread(db_set_last_utility_mid, user_id, m.message_id)
            return

        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        subject_marks = await asyncio.to_thread(db_get_user_subject_marks, user_id)
        text = build_profile_card_text(profile, None, subject_marks)
        kb = build_profile_main_keyboard(has_team=bool(profile.get("org_id")))
        m = await send_rich_message_safe(context.bot, chat_id=query.message.chat_id, html_content=text, reply_markup=kb)
        if m:
            await asyncio.to_thread(db_set_last_utility_mid, user_id, m.message_id)
        return

    elif action == "org_history":
        org_id = int(d_id)
        from src.database import db_get_user_org_role
        user_role = await asyncio.to_thread(db_get_user_org_role, user_id, org_id)
        if user_role not in ("creator", "admin"):
            await query.answer("Admins only.", show_alert=True)
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

        from src.database import db_get_org_membership_log, db_get_user_timezone
        from src.rendering.html_views import build_org_history_text
        log_rows = await asyncio.to_thread(db_get_org_membership_log, org_id)
        viewer_tz = await asyncio.to_thread(db_get_user_timezone, user_id)
        text = build_org_history_text(org_details, log_rows, viewer_tz)

        pending_rows = [r for r in log_rows if r['org_role'] == 'pending']
        kb_rows = []
        for r in pending_rows[:8]:
            nm_short = format_public_name(r)[:16]
            kb_rows.append([
                InlineKeyboardButton(f"✅ {nm_short}", callback_data=f"process_req|{org_id}|{r['user_id']}|1"),
                InlineKeyboardButton("❌", callback_data=f"process_req|{org_id}|{r['user_id']}|0")
            ])
        kb_rows.append([InlineKeyboardButton("🔙 TEAM", callback_data=f"view_org|{org_id}")])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=InlineKeyboardMarkup(kb_rows))
        return

    elif action == "process_req":
        org_id, target_user_id, decision = int(d_id), data[2], data[3]
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        if profile.get("org_role") not in ("creator", "admin") or int(profile.get("org_id") or 0) != org_id:
            await query.answer("Only team admins can process requests.", show_alert=True)
            return

        if decision == "-1":
            await query.answer("Left pending — no action taken.")
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
            closed_nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("👤 GO TO PROFILE", callback_data="privacy_menu|0")]])
            await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=old_text, reply_markup=closed_nav_kb)
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
            reject_kb = InlineKeyboardMarkup([[InlineKeyboardButton("👤 RETURN TO PROFILE", callback_data="privacy_menu|0")]])
            try:
                await context.bot.send_message(
                    chat_id=int(target_user_id),
                    text=f"Your request to join <b>{html.escape(org_name)}</b> wasn't accepted this time. You're welcome to try another team.",
                    parse_mode="HTML", reply_markup=reject_kb
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
            removed_note = ""
            if track.get('status') == 'deleted':
                removed_note = (
                    "\n\n<i>⚫ Note: this question was later removed from the channel — "
                    "your saved answer above is unaffected.</i>"
                )
            diagram_note = ""
            from src.rendering.latex_templates import has_real_diagram
            if has_real_diagram(q):
                diagram_note = "\n\n<i>🖼️ This question has a diagram — view it via 📣 OPEN IN CHANNEL below for the full visual.</i>"

            perf_card = await asyncio.to_thread(process_user_score, user_id, track['message_id'], q_id, existing['is_correct'], existing['selected_option'])
            explanation_html = UIFactory.build_answered_view(
                q, str(track['display_id']), existing['selected_option'],
                show_derivation=True, show_perf=False, perf_card=perf_card, include_diagram=False
            ) + removed_note + diagram_note

            channel_username = CONFIG.get("channel", "QuizOva").lstrip('@')
            diag_kb_rows = [
                [InlineKeyboardButton("🙈 REMOVE FROM MY LIST", callback_data=f"my_ans_hide|{q_id}|{subject}|{filter_mode}|{offset}|{sort_code}|{dir_code}")],
                [InlineKeyboardButton("🔙 BACK TO LIST", callback_data=back_cb)]
            ]
            if has_real_diagram(q):
                diag_kb_rows.insert(0, [InlineKeyboardButton("📣 OPEN IN CHANNEL", url=f"https://t.me/{channel_username}/{track['message_id']}")])
            try:
                await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=explanation_html, reply_markup=InlineKeyboardMarkup(diag_kb_rows))
            except Exception as e:
                print(f"[MY-ANS-OPEN-ERROR] Failed to render REF {track.get('display_id')}: {e}", flush=True)
                await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content="⚠️ Couldn't load this question's details. Try again.", reply_markup=back_kb)
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
            [InlineKeyboardButton("🙈 REMOVE FROM MY LIST", callback_data=f"my_ans_hide|{q_id}|{subject}|{filter_mode}|{offset}|{sort_code}|{dir_code}")],
            [InlineKeyboardButton("🔙 BACK TO LIST", callback_data=back_cb)]
        ])
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=f"⬜ <b>{q['topic']}</b>\n\nYou haven't answered this one yet — tap below to open it in the channel.",
            reply_markup=open_kb
        )
        return

    elif action == "my_ans_hide":
        await query.answer()
        q_id_hide, subject, filter_mode, offset = data[1], data[2], data[3], data[4]
        sort_code = data[5] if len(data) > 5 else "t"
        dir_code = data[6] if len(data) > 6 else "a"
        back_cb = f"my_ans_subj|{subject}|{filter_mode}|{offset}|{sort_code}|{dir_code}"

        await asyncio.to_thread(db_hide_question_for_user, user_id, q_id_hide)

        nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK TO LIST", callback_data=back_cb)]])
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=(
                "🙈 <b>Removed from your list.</b>\n\n"
                "This only affects your personal view — the question, your saved score, "
                "and the channel post are all untouched."
            ),
            reply_markup=nav_kb
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

    elif action == "resend_join":
        org_id = int(d_id)
        from src.database import db_resend_join_request
        ok = await asyncio.to_thread(db_resend_join_request, user_id, org_id)
        await query.answer("Sent again!" if ok else "Couldn't resend — try again.")
        if ok:
            conn = engine.get_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT org_name FROM organizations WHERE org_id = %s;", (org_id,))
                    row = cur.fetchone()
            finally:
                engine.release_connection(conn)
            await _notify_org_admins_pending_request(context, org_id, row['org_name'] if row else "the team", query.from_user)
        nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("👤 MY PROFILE", callback_data="privacy_menu|0")]])
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content="🔁 <b>Request re-sent!</b> The team's admins have been notified again.",
            reply_markup=nav_kb
        )
        return

    elif action == "wrsel_ctry":
        await query.answer()
        purpose = d_id
        fav_type = _WR_FAV_TYPE_FOR_PURPOSE.get(purpose)
        favorites = await asyncio.to_thread(db_get_user_favorites, user_id, fav_type) if fav_type else []
        title = {"nav_country": "🌎 SELECT A COUNTRY", "nav_city": "🏙 SELECT YOUR CITY'S COUNTRY",
                  "nav_school": "🏫 SELECT YOUR SCHOOL'S COUNTRY", "fav_country": "🌎 PICK A COUNTRY TO FAVORITE",
                  "fav_city": "🏙 PICK YOUR CITY'S COUNTRY", "fav_school": "🏫 PICK YOUR SCHOOL'S COUNTRY"}.get(purpose, "SELECT COUNTRY")
        subtitle = "" if purpose in ("nav_country", "fav_country") else "\n<i>Select the country where your city belongs, then we'll narrow it down.</i>"
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=f"<h2>{title}</h2>{subtitle}",
            reply_markup=_build_wrsel_country_index_kb(purpose, favorites)
        )
        return

    elif action == "wrsel_letter":
        await query.answer()
        purpose, letter = d_id, data[2]
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=f"<h2>🌎 COUNTRIES: {letter}</h2>",
            reply_markup=_build_wrsel_country_letter_kb(purpose, letter)
        )
        return

    elif action == "wrsel_ctry_go":
        await query.answer()
        purpose, country = d_id, data[2]
        from src.database import db_get_rank_matrix, db_get_scope_summary

        if purpose == "nav_country":
            matrix = await asyncio.to_thread(db_get_rank_matrix, "country", country, "all", "all", "all", "total", 10)
            summary = await asyncio.to_thread(db_get_scope_summary, "country", country, "all")
            text = build_leaderboard_text("country", country, "all", "all", "all", "total", matrix, summary)
            kb = build_leaderboard_keyboard("country", country, "all", "all", "all", "total", "none", [], 0)
            await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
            return

        if purpose == "fav_country":
            await asyncio.to_thread(db_add_favorite, user_id, "country", country, country)
            nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⭐ MY COUNTRY FAVORITES", callback_data="wr_fav_list|country|0")],
                                            [InlineKeyboardButton("🔙 LEADERBOARD", callback_data="wr|world|_|all|all|all|total|none|0")]])
            await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
                html_content=f"⭐ <b>{html.escape(country)}</b> added to your favorites!", reply_markup=nav_kb)
            return

        # nav_city / fav_city / nav_school / fav_school -> continue to city selection
        from src.database import db_get_cities_for_country
        cities = await asyncio.to_thread(db_get_cities_for_country, country)
        rows = [[InlineKeyboardButton(c, callback_data=f"wrsel_city_go|{purpose}|{country}|{c}")] for c in cities[:20]]
        rows.append([InlineKeyboardButton("✍️ TYPE CITY", callback_data=f"wrsel_city_type|{purpose}|{country}")])
        rows.append([InlineKeyboardButton("🔙 COUNTRIES", callback_data=f"wrsel_ctry|{purpose}|0")])
        subtitle = "Pick your city, or type it if it's not listed:" if cities else "No cities on file yet for this country — type yours:"
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=f"<h2>🏙 {html.escape(country)}</h2>\n{subtitle}",
            reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    elif action == "wrsel_city_go":
        await query.answer()
        purpose, country, city = d_id, data[2], data[3]
        from src.database import db_get_rank_matrix, db_get_scope_summary

        if purpose == "nav_city":
            matrix = await asyncio.to_thread(db_get_rank_matrix, "city", city, "all", "all", "all", "total", 10)
            summary = await asyncio.to_thread(db_get_scope_summary, "city", city, "all")
            text = build_leaderboard_text("city", city, "all", "all", "all", "total", matrix, summary)
            kb = build_leaderboard_keyboard("city", city, "all", "all", "all", "total", "none", [], 0)
            await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
            return

        if purpose == "fav_city":
            await asyncio.to_thread(db_add_favorite, user_id, "city", city, f"{city}, {country}")
            nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⭐ MY CITY FAVORITES", callback_data="wr_fav_list|city|0")],
                                            [InlineKeyboardButton("🔙 LEADERBOARD", callback_data="wr|world|_|all|all|all|total|none|0")]])
            await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
                html_content=f"⭐ <b>{html.escape(city)}</b> added to your favorites!", reply_markup=nav_kb)
            return

        # nav_school / fav_school -> continue to school selection
        from src.database import db_search_schools
        schools = await asyncio.to_thread(db_search_schools, None, city, country, 20)
        rows = [[InlineKeyboardButton(s["org_name"], callback_data=f"wrsel_school_go|{purpose}|{s['org_id']}")] for s in schools]
        rows.append([InlineKeyboardButton("✍️ TYPE SCHOOL", callback_data=f"wrsel_school_type|{purpose}|{country}|{city}")])
        rows.append([InlineKeyboardButton("🔙 CITIES", callback_data=f"wrsel_ctry_go|{purpose}|{country}")])
        subtitle = "Pick your school, or type its name if it's not listed:" if schools else "No schools on file yet for this city — type yours:"
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=f"<h2>🏫 {html.escape(city)}</h2>\n{subtitle}",
            reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    elif action == "wrsel_school_go":
        await query.answer()
        purpose, org_id = d_id, int(data[2])
        from src.database import GLOBAL_ENGINE
        conn = GLOBAL_ENGINE.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT org_name FROM organizations WHERE org_id = %s;", (org_id,))
                row = cur.fetchone()
                org_name = row["org_name"] if row else "School"
        finally:
            GLOBAL_ENGINE.release_connection(conn)

        if purpose == "fav_school":
            await asyncio.to_thread(db_add_favorite, user_id, "school", str(org_id), org_name)
            nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⭐ MY SCHOOL FAVORITES", callback_data="wr_fav_list|school|0")],
                                            [InlineKeyboardButton("🔙 LEADERBOARD", callback_data="wr|world|_|all|all|all|total|none|0")]])
            await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
                html_content=f"⭐ <b>{html.escape(org_name)}</b> added to your favorites!", reply_markup=nav_kb)
            return

        from src.database import db_get_rank_matrix, db_get_scope_summary
        matrix = await asyncio.to_thread(db_get_rank_matrix, "school", str(org_id), "all", "all", "all", "total", 10)
        summary = await asyncio.to_thread(db_get_scope_summary, "school", str(org_id), "all")
        text = build_leaderboard_text("school", str(org_id), "all", "all", "all", "total", matrix, summary)
        kb = build_leaderboard_keyboard("school", str(org_id), "all", "all", "all", "total", "none", [], 0)
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        return

    elif action == "wrsel_city_type":
        await query.answer()
        purpose, country = d_id, data[2]
        USER_STATES[user_id] = "AWAITING_WR_CITY_TEXT"
        USER_PAYLOADS[user_id] = {"wr_purpose": purpose, "wr_country": country, "edit_mid": query.message.message_id}
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ CANCEL", callback_data=f"wrsel_ctry_go|{purpose}|{country}")]])
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=f"✍️ <b>Type your city in {html.escape(country)}:</b>" + FSM_INPUT_HINT,
            reply_markup=cancel_kb
        )
        return

    elif action == "wrsel_school_type":
        await query.answer()
        purpose, country, city = d_id, data[2], data[3]
        USER_STATES[user_id] = "AWAITING_WR_SCHOOL_TEXT"
        USER_PAYLOADS[user_id] = {"wr_purpose": purpose, "wr_country": country, "wr_city": city, "edit_mid": query.message.message_id}
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ CANCEL", callback_data=f"wrsel_city_go|{purpose}|{country}|{city}")]])
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=f"✍️ <b>Type your school's name in {html.escape(city)}:</b>" + FSM_INPUT_HINT,
            reply_markup=cancel_kb
        )
        return

    elif action == "wrsel_fav_go":
        await query.answer()
        purpose, value = d_id, data[2]
        from src.database import db_get_rank_matrix, db_get_scope_summary
        scope = {"nav_country": "country", "nav_city": "city", "nav_school": "school"}.get(purpose, "world")
        matrix = await asyncio.to_thread(db_get_rank_matrix, scope, value, "all", "all", "all", "total", 10)
        summary = await asyncio.to_thread(db_get_scope_summary, scope, value, "all")
        text = build_leaderboard_text(scope, value, "all", "all", "all", "total", matrix, summary)
        kb = build_leaderboard_keyboard(scope, value, "all", "all", "all", "total", "none", [], 0)
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        return



    elif action == "...":
        await query.answer()
        ...
        if some_condition:
            await query.answer("...", show_alert=True)
            return




    if action not in ("ans", "toggle", "toggle_photo", "confirm_change", "cancel_change", "fb_view", "my_ans_hide"):
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

