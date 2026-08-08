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


def _build_feedback_detail_keyboard(fb_id, return_state: str = None, is_closed: bool = False) -> InlineKeyboardMarkup:
    rs = return_state or "all:all:0"
    rows = [
        [InlineKeyboardButton("🔧 ACTIVE", callback_data=f"fb_status|{fb_id}|in_progress|{rs}"),
         InlineKeyboardButton("🗓️ PLANNED", callback_data=f"fb_status|{fb_id}|planned|{rs}")],
        [InlineKeyboardButton("✅ RESOLVED", callback_data=f"fb_status|{fb_id}|resolved|{rs}"),
         InlineKeyboardButton("🚫 WON'T FIX", callback_data=f"fb_status|{fb_id}|wontfix|{rs}")],
    ]
    if is_closed:
        rows.append([InlineKeyboardButton("🔓 REOPEN CONVERSATION", callback_data=f"fb_toggle_close|{fb_id}|0")])
    else:
        rows.append([InlineKeyboardButton("💬 REPLY", callback_data=f"fb_reply|{fb_id}")])
        rows.append([InlineKeyboardButton("🔒 CLOSE CONVERSATION", callback_data=f"fb_toggle_close|{fb_id}|1")])
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

    from src.database import db_get_teams_affected_by_location_change
    affected_teams = await asyncio.to_thread(
        db_get_teams_affected_by_location_change, user_id,
        new_city=None if city == "Not set" else city,
        new_country=None if country == "Not set" else country,
        new_school_name=None if leaving_school else school_name,
        leaving_school=leaving_school
    )
    if affected_teams:
        team_list = "\n".join(f"  • <b>{html.escape(t['org_name'])}</b> (#{t['org_tag']})" for t in affected_teams)
        lines.append(
            f"\n<blockquote>🔒 <b>You'll be removed from these dedicated teams</b> — they're "
            f"restricted to your OLD city/country/school:\n{team_list}\n\n"
            f"Everything you earned on them stays on their board exactly as it is; you just "
            f"stop contributing going forward. Confirming below removes you automatically."
            f"</blockquote>"
        )

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


async def _regloc_finish(context, chat_id, message_id, user_id, school_msg: str = None, school_sid: int = None):
    """Commits the reviewed selections. school_sid: the location_suggestions id created for a
    BRAND NEW school in this same confirm action, if any — when both a new city AND a new
    school are submitted together, they get linked so a single admin decision resolves both."""
    from src.database import db_update_user_location, db_create_location_suggestion, db_set_user_pending_city, db_get_all_admin_ids, db_leave_organization, db_get_teams_affected_by_location_change, db_clear_user_grade, db_link_location_suggestions
    session = USER_PAYLOADS.pop(user_id, {})
    USER_STATES[user_id] = "IDLE"

    _city = session.get("reg_city")
    _country = session.get("reg_country")
    _school_name = session.get("reg_school_name")
    _leaving_school = session.get("reg_leave_school", False)

    removed_teams_msg = ""
    affected = await asyncio.to_thread(
        db_get_teams_affected_by_location_change, user_id,
        new_city=_city, new_country=_country,
        new_school_name=(None if _leaving_school else _school_name),
        leaving_school=_leaving_school
    )
    for t in affected:
        await asyncio.to_thread(db_leave_organization, user_id, t['org_id'])
    if affected:
        names = ", ".join(f"<b>{html.escape(t['org_name'])}</b>" for t in affected)
        removed_teams_msg = f"\n\n🔒 <i>Removed from {names} — no longer matches your new location/school.</i>"

    genuinely_no_school_selected = not session.get("reg_school_org_id") and not session.get("reg_school_name")
    if session.get("reg_leave_school") and genuinely_no_school_selected:
        await asyncio.to_thread(db_clear_user_grade, user_id)
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

    location_write_ok = True
    city_sid = None
    if city or country:
        if city_is_new and not existing_sid:
            city_sid = await asyncio.to_thread(db_create_location_suggestion, "city", city, country, user_id)
            location_write_ok = await asyncio.to_thread(db_set_user_pending_city, user_id, city, country, city_sid)
            if not location_write_ok:
                print(f"[REGLOC-FINISH-ERROR] db_set_user_pending_city FAILED for user={user_id} city={city!r} country={country!r} sid={city_sid}", flush=True)
            if school_sid:
                await asyncio.to_thread(db_link_location_suggestions, city_sid, school_sid)
        elif not city_is_new:
            location_write_ok = await asyncio.to_thread(db_update_user_location, user_id, city or "Not set", country or "Not set")
            if not location_write_ok:
                print(f"[REGLOC-FINISH-ERROR] db_update_user_location FAILED for user={user_id} city={city!r} country={country!r}", flush=True)

    # THE FIX ("must confirm both at the same time"): ONE combined admin message covering
    # whatever is actually new THIS confirm — city only, school only, or both. Resolving via
    # the first item's buttons cascades to the linked item automatically (db_resolve_location_
    # suggestion already does this), so admin only ever taps once regardless of how many items
    # are bundled. When only one of the two changed, pending_items naturally has just that one —
    # so single-item submissions are still treated completely separately, as required.
    pending_items = []
    if city_sid:
        pending_items.append(("📍", city_sid, f"{city}, {country}", "city"))
    if school_sid:
        pending_items.append(("🏫", school_sid, session.get("reg_school_name"), "school"))

    if pending_items:
        admin_ids = await asyncio.to_thread(db_get_all_admin_ids)
        req_name = html.escape((await context.bot.get_chat(user_id)).first_name or "A student")
        primary_sid = pending_items[0][1]
        combo_note = "\n\n🔗 <i>Approving or rejecting below resolves everything above together.</i>" if len(pending_items) > 1 else ""
        item_lines = "\n".join(f"{icon} <b>{html.escape(str(label))}</b>" for icon, _, label, _ in pending_items)
        review_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ APPROVE", callback_data=f"loc_review|{primary_sid}|1"),
            InlineKeyboardButton("🚫 REJECT", callback_data=f"loc_review|{primary_sid}|0")
        ], [
            InlineKeyboardButton("💬 ASK USER", callback_data=f"loc_review_msg|{primary_sid}"),
            InlineKeyboardButton("⏳ PENDING QUEUE", callback_data=f"loc_review|{primary_sid}|-1")
        ]])
        for admin_id in admin_ids:
            try:
                await context.bot.send_message(
                    chat_id=int(admin_id),
                    text=f"📥 <b>NEW REGISTRATION REQUEST</b>\n\n<b>{req_name}</b> submitted:\n{item_lines}{combo_note}",
                    reply_markup=review_kb, parse_mode="HTML"
                )
            except Exception:
                pass

    profile_nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("👤 PROFILE", callback_data="privacy_menu|0")]])

    if not location_write_ok:
        retry_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 TRY AGAIN", callback_data="regloc_start|0")]])
        await edit_rich_message_safe(
            context.bot, chat_id=chat_id, message_id=message_id,
            html_content=(
                "⚠️ <b>Could not save your location.</b>\n\n"
                "Your school/team changes above (if any) went through, but saving your city/country hit a "
                "database error. Check the bot logs for <code>[REGLOC-FINISH-ERROR]</code> — that line "
                "will show the exact cause. Please tap below to try again."
            ),
            reply_markup=retry_kb
        )
        return

    status_line = "⏳ pending admin review" if city_is_new else "✅ saved"
    await edit_rich_message_safe(
        context.bot, chat_id=chat_id, message_id=message_id,
        html_content=(
            f"✅ <b>Setup complete!</b>\n\n📍 {city or '—'}, {country or '—'} ({status_line})\n{school_msg or ''}"
            f"{removed_teams_msg}\n\n"
            f"📌 <i>Your marks always stay with the city/country/school you earned them in — changing any of "
            f"these later never moves old marks, it just starts a fresh total on the new one.</i>"
        ),
        reply_markup=profile_nav_kb
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, engine):
    """Every action branch answers its callback query EARLY (before doing the real work), so
    by the time an exception fires here, query.answer() has usually already been called once —
    Telegram silently rejects a second answer() call, and the old `except: pass` around that
    swallowed it completely. THIS is the actual cause behind Kanban / feedback details / request
    details all going quiet: the error WAS happening, it just had nowhere left to surface.
    Now falls back to editing the message with the real error instead of failing silently."""
    try:
        await _handle_callback_inner(update, context, engine)
    except Exception as e:
        traceback.print_exc()
        try:
            await update.callback_query.answer("⚠️ Something went wrong. Please try again.", show_alert=True)
        except Exception:
            try:
                err_detail = f"⚠️ <b>Something went wrong.</b>\n\n🛠️ <code>{type(e).__name__}: {html.escape(str(e))[:200]}</code>"
                await update.callback_query.message.edit_text(err_detail, parse_mode="HTML")
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
    from src.geo import format_local_time
    from src.database import db_get_user_snapshot, db_get_user_timezone
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

    for admin_id in admin_ids:
        try:
            # THE FIX: last_req_str used to be computed ONCE, before this loop, using the
            # default UTC timezone — every admin, regardless of their own city, saw "last on
            # ... UTC". Now fetched per-admin inside the loop.
            admin_tz = await asyncio.to_thread(db_get_user_timezone, admin_id)
            last_req_str = format_local_time(last_requested_at, admin_tz) if last_requested_at else "just now"
            repeat_note = f"\n📈 Requested <b>{request_count}×</b>, last on {last_req_str}." if request_count > 1 else ""
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


async def _render_team_details(context, chat_id, message_id, user_id, org_id: int, grade_filter, sort_field: str, sort_dir: str, is_impersonating: bool = False):
    from src.database import (
        db_get_team_membership_homogeneity, db_get_team_scope_ranks, db_get_org_member_matrix,
        db_count_org_members, db_count_org_left_members, db_get_org_admin_ids, db_get_team_average_marks,
    )
    from src.rendering.html_views import build_team_details_text, build_team_rank_table_text, build_team_details_keyboard

    conn = engine_ref = None
    from src.database import GLOBAL_ENGINE
    conn = GLOBAL_ENGINE.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM organizations WHERE org_id = %s;", (org_id,))
            org = cur.fetchone()
            cur.execute("SELECT org_role FROM org_memberships WHERE user_id = %s AND org_id = %s AND state = 'active';", (str(user_id), org_id))
            membership = cur.fetchone()
    finally:
        GLOBAL_ENGINE.release_connection(conn)

    if not org:
        await edit_rich_message_safe(context.bot, chat_id=chat_id, message_id=message_id, html_content="⚠️ Team not found.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👤 OPEN MY DASHBOARD", callback_data="privacy_menu|0")]]))
        return

    user_role = membership.get("org_role") if membership else "member"
    is_admin_here = user_role in ("creator", "admin")
    is_creator = (user_role == "creator")

    geo = await asyncio.to_thread(db_get_team_membership_homogeneity, org_id)
    scope_ranks = await asyncio.to_thread(db_get_team_scope_ranks, org_id)
    member_count = await asyncio.to_thread(db_count_org_members, org_id, None)
    left_count = await asyncio.to_thread(db_count_org_left_members, org_id) if is_admin_here else 0
    grade_count = await asyncio.to_thread(db_count_org_members, org_id, grade_filter)
    matrix = await asyncio.to_thread(db_get_org_member_matrix, org_id, grade_filter, sort_field, sort_dir, 15, 0)
    avg_info = await asyncio.to_thread(db_get_team_average_marks, org_id) if is_admin_here else None

    info_text = build_team_details_text(org, geo, scope_ranks, member_count, left_count, is_admin_here, avg_info)
    table_text = build_team_rank_table_text(matrix, grade_filter, grade_count)
    kb = build_team_details_keyboard(org_id, grade_filter, sort_field, sort_dir, is_admin_here, is_creator)

    full_text = f"{info_text}\n\n{table_text}"
    if is_impersonating:
        full_text = f"🎭 <b>ACTING AS <code>{user_id}</code></b> — tap 🛑 to stop.\n<hr/>\n{full_text}"
        kb_rows = kb.inline_keyboard + [[InlineKeyboardButton("🛑 STOP ACTING AS USER", callback_data="imp_stop|0")]]
        kb = InlineKeyboardMarkup(kb_rows)
    await edit_rich_message_safe(context.bot, chat_id=chat_id, message_id=message_id, html_content=full_text, reply_markup=kb)


async def _handle_callback_inner(update: Update, context: ContextTypes.DEFAULT_TYPE, engine):
    query = update.callback_query
    data = query.data.split("|")
    action, d_id = data[0], data[1]
    user_id = query.from_user.id

    # THE FIX (impersonation): once a session exists in IMPERSONATION_SESSIONS, every action
    # EXCEPT the impersonation/directory controls listed here runs against the TARGET user's
    # identity instead of the admin's — this is the entire "do as if the user does it"
    # mechanism. The exempt actions always keep the real caller so the controls can never be
    # swapped out from under the admin mid-session (and so admin_users/admin_dashboard stay
    # navigable while impersonating, without needing to imp_stop first just to browse).
    _IMPERSONATION_EXEMPT_ACTIONS = (
        "imp_start", "imp_stop", "imp_request", "imp_respond",
        "admin_view_profile", "admin_manage_user", "admin_toggle_perm",
        "admin_user_actions", "admin_users", "admin_dashboard"
    )
    real_caller_id = user_id
    is_impersonating = False
    if action not in _IMPERSONATION_EXEMPT_ACTIONS:
        from src.config import IMPERSONATION_SESSIONS
        impersonated = IMPERSONATION_SESSIONS.get(str(user_id))
        if impersonated:
            user_id = int(impersonated)
            # THE FIX: is_impersonating used to only ever be shown on the imp_start confirmation
            # screen — every screen after that (profile, settings, team, feedback...) looked
            # byte-identical to the admin's own account, with no way to tell the two apart mid-
            # session. This flag now travels with every action so profile-family screens can
            # stamp themselves.
            is_impersonating = True
    print(f"\n{Style.CYAN}[CALLBACK DEBUG]{Style.RESET} Action: {action} | Ref ID: {d_id} | User ID: {user_id}")

    # Standard circular home button for intermediate flows
    return_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("👤 OPEN MY DASHBOARD", callback_data="privacy_menu|0")
    ]])

    if action == "set_grade":
        grade = int(d_id)
        profile = await asyncio.to_thread(db_get_user_profile, user_id)

        if not profile or not profile.get("org_id"):
            # Grade is optional overall — city/country alone unlock answering — but
            # the moment someone WANTS a grade, they're declaring themselves a student,
            # and a student needs a school on file. This is the opposite of the "grade
            # requires nothing" pass from earlier — that one over-corrected; the real
            # rule is: no school → no grade, but no grade → still totally fine.
            await query.answer()
            nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("📍 REGISTER MY SCHOOL", callback_data="regloc_start|0")]])
            await edit_rich_message_safe(
                context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
                html_content=(
                    "🎒 <b>Grade requires a school on file first</b>\n\n"
                    "Grade only matters for registered students — if you're not currently "
                    "a student, you simply don't need one and can skip this entirely.\n\n"
                    "Register your school, then come back here."
                ),
                reply_markup=nav_kb
            )
            return
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
            msg = (
                f"✅ <b>Grade {grade} Registered!</b>\n\n"
                f"One more step: set your country &amp; city. This is <b>required</b> before you can "
                f"answer questions — even a pending submission unlocks it, so it only takes a minute."
            )
            confirm_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🌍 SET LOCATION NOW", callback_data="regloc_start|0")],
                [InlineKeyboardButton("⏭ LATER (needed before answering)", callback_data="privacy_menu|0")]
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
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        if not profile or not profile.get("personal_city") or not profile.get("personal_country"):
            # Grade is optional and must never gate this — only city+country define
            # "setup complete." Using the same check as _start_command_inner/profile_command
            # so these three entry points can never disagree with each other again.
            setup_kb = InlineKeyboardMarkup([[InlineKeyboardButton("📍 FINISH SETUP", callback_data="regloc_start|0")]])
            await edit_rich_message_safe(
                context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
                html_content="📍 You haven't finished setup yet — set your city and country first.",
                reply_markup=setup_kb
            )
            return
        from src.database import db_set_last_utility_mid
        await asyncio.to_thread(db_set_last_utility_mid, user_id, query.message.message_id)
        from src.database import db_get_user_top_topic, db_get_user_rank_summary
        subject_marks = await asyncio.to_thread(db_get_user_subject_marks, user_id)
        top_topic = await asyncio.to_thread(db_get_user_top_topic, user_id)
        rank_summary = await asyncio.to_thread(db_get_user_rank_summary, user_id)
        text = build_profile_card_text(profile, None, subject_marks, top_topic, rank_summary)
        kb = build_profile_main_keyboard(has_team=bool(profile.get("team_id")))
        # THE FIX: makes it unmistakable that the admin is looking at SOMEONE ELSE'S live profile,
        # not their own — same banner treatment on every profile-family screen while a session is
        # active, not just the initial imp_start confirmation.
        if is_impersonating:
            text = f"🎭 <b>ACTING AS <code>{user_id}</code></b> — tap 🛑 to stop.\n<hr/>\n{text}"
            kb_rows = kb.inline_keyboard + [[InlineKeyboardButton("🛑 STOP ACTING AS USER", callback_data="imp_stop|0")]]
            kb = InlineKeyboardMarkup(kb_rows)
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
        return

    elif action == "profile_popup":
        await query.answer()
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        if not profile or not profile.get("personal_city") or not profile.get("personal_country"):
            nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("📍 SET UP MY PROFILE", callback_data="regloc_start|0")]])
            await _open_utility_view(
                context, user_id, query.message.chat_id,
                "📍 You haven't finished setup yet. Type /start to set your city and country first.",
                nav_kb
            )
            return
        subject_marks = await asyncio.to_thread(db_get_user_subject_marks, user_id)
        text = build_profile_card_text(profile, None, subject_marks)
        kb = build_profile_main_keyboard(has_team=bool(profile.get("team_id")))
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
        from src.database import db_is_tournament_round_still_open, db_edit_tournament_answer, db_user_location_complete
        if not await asyncio.to_thread(db_user_location_complete, user_id):
            await query.answer("📍 Set your city & country first.", show_alert=True)
            gate_kb = InlineKeyboardMarkup([[InlineKeyboardButton("📍 SET MY LOCATION NOW", callback_data="regloc_start|0")]])
            await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content="🚫 Set your city &amp; country first before changing an answer.", reply_markup=gate_kb)
            return
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
        USER_PAYLOADS[user_id]["reg_leave_school"] = False
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
        from src.database import db_check_user_permission
        # in regloc_school_create AND wherever a school/city suggestion gets submitted:
        if not await asyncio.to_thread(db_check_user_permission, user_id, "requests"):
            await query.answer("🚫 You've been restricted from submitting requests.", show_alert=True)
            return
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
            "reg_leave_school": False,
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
        school_sid = None  # set below only when a BRAND NEW school suggestion is created this confirm

        if school_org_id:
            profile_before = await asyncio.to_thread(db_get_user_profile, user_id)
            old_org_id = profile_before.get("org_id") if profile_before else None
            if old_org_id and int(old_org_id) != int(school_org_id):
                await asyncio.to_thread(db_leave_organization, user_id, old_org_id)

            join_data = await asyncio.to_thread(db_join_organization_by_id, user_id, school_org_id)
            print(f"[DEBUG-REGLOC-CONFIRM] school join result for user={user_id}: {join_data}", flush=True)
            if not join_data:
                school_msg = "⚠️ Could not join school."
            elif join_data.get("scope_blocked"):
                school_msg = f"🔒 {html.escape(join_data['org_name'])} is restricted — {join_data.get('reason','')}"
            elif join_data.get("role_assigned") == "pending":
                school_msg = f"📥 Request sent to <b>{html.escape(join_data['org_name'])}</b> — awaiting admin approval."
            else:
                school_msg = f"✅ Joined <b>{html.escape(join_data['org_name'])}</b>!"
        elif school_name and session.get("reg_school_is_new"):
            from src.database import db_create_location_suggestion, db_get_all_admin_ids
            org_tag = session.get("reg_new_org_tag")
            reg_city = session.get("reg_city")
            reg_country = session.get("reg_country")
            new_org_id = None

            try:
                profile_before = await asyncio.to_thread(db_get_user_profile, user_id)
                old_org_id = profile_before.get("org_id") if profile_before else None
                if old_org_id:
                    await asyncio.to_thread(db_leave_organization, user_id, old_org_id)

                new_org_id = await asyncio.to_thread(
                    db_create_organization, school_name, org_tag, user_id,
                    "School", True, reg_city, reg_country, "pending"
                )
            except Exception as e:
                traceback.print_exc()
                print(f"[REGLOC-SCHOOL-CREATE-ERROR] user={user_id} name={school_name!r} tag={org_tag!r} city={reg_city!r} country={reg_country!r}: {e}", flush=True)
                school_msg = (
                    f"⚠️ Could not create the new school team — please try again from 📍 LOCATIONS &amp; SCHOOL.\n"
                    f"<code>{html.escape(str(e))[:150]}</code>"
                )

            if new_org_id:
                try:
                    # THE FIX: no longer sends its own admin message here. _regloc_finish
                    # (called right after this) is now the SINGLE place that notifies
                    # admins — it collects whatever is actually new this confirm (city,
                    # school, or both) and sends exactly ONE combined message instead of
                    # two separate ones. This is what "must confirm both at the same
                    # time" needed.
                    school_sid = await asyncio.to_thread(db_create_location_suggestion, "school", school_name, reg_country, user_id, new_org_id)
                    school_msg = f"🏫 <b>{html.escape(school_name)}</b> submitted for review — you'll be linked once approved."
                except Exception as e:
                    traceback.print_exc()
                    print(f"[REGLOC-SCHOOL-NOTIFY-ERROR] org_id={new_org_id} created OK, but suggestion creation failed: {e}", flush=True)
                    school_msg = (
                        f"✅ School created, but the review request failed to save — an admin can still "
                        f"approve <code>{new_org_id}</code> manually.\n"
                        f"<code>{html.escape(str(e))[:150]}</code>"
                    )

        await _regloc_finish(context, query.message.chat_id, query.message.message_id, user_id, school_msg=school_msg, school_sid=school_sid)
        return

    elif action == "loc_toggle_close":
        from src.database import db_is_admin, db_set_location_suggestion_closed, db_get_location_suggestion
        if not await asyncio.to_thread(db_is_admin, user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        ls_id, target = int(d_id), (data[2] == "1")
        await asyncio.to_thread(db_set_location_suggestion_closed, ls_id, target)
        await query.answer("Closed — student can't reply until reopened." if target else "Reopened.")
        ls = await asyncio.to_thread(db_get_location_suggestion, ls_id)
        from src.database import db_get_location_suggestion_thread
        thread = await asyncio.to_thread(db_get_location_suggestion_thread, ls_id)
        viewer_tz = await asyncio.to_thread(db_get_user_timezone, user_id)
        from src.rendering.html_views import build_location_suggestion_item_text
        text = build_location_suggestion_item_text(ls, thread, viewer_tz)
        rows = []
        if ls['status'] == 'pending':
            rows.append([InlineKeyboardButton("✅ APPROVE", callback_data=f"loc_review|{ls_id}|1"), InlineKeyboardButton("🚫 REJECT", callback_data=f"loc_review|{ls_id}|0")])
        elif ls['status'] == 'rejected':
            rows.append([InlineKeyboardButton("✅ APPROVE INSTEAD", callback_data=f"loc_review|{ls_id}|1")])
        elif ls['status'] == 'approved':
            rows.append([InlineKeyboardButton("🚫 REJECT INSTEAD", callback_data=f"loc_review|{ls_id}|0")])
        if ls.get('is_closed'):
            rows.append([InlineKeyboardButton("🔓 REOPEN CONVERSATION", callback_data=f"loc_toggle_close|{ls_id}|0")])
        else:
            rows.append([InlineKeyboardButton("💬 MESSAGE STUDENT", callback_data=f"loc_review_msg|{ls_id}")])
            rows.append([InlineKeyboardButton("🔒 CLOSE CONVERSATION", callback_data=f"loc_toggle_close|{ls_id}|1")])
        rows.append([InlineKeyboardButton("🔙 QUEUE", callback_data="loc_admin_browse|all|pending:0")])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=InlineKeyboardMarkup(rows))
        return

    elif action == "loc_user_reply":
        sid = int(d_id)
        from src.database import db_get_location_suggestion
        ls = await asyncio.to_thread(db_get_location_suggestion, sid)
        if ls and ls.get('is_closed'):
            await query.answer("This conversation is closed — an admin needs to reopen it.", show_alert=True)
            return
        await query.answer()
        USER_STATES[user_id] = "AWAITING_USER_LOCATION_REPLY"
        USER_PAYLOADS[user_id] = {"suggestion_id": sid, "edit_mid": query.message.message_id}
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ CANCEL", callback_data="fsm_cancel|privacy_menu")]])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content="✍️ <b>Type your reply:</b>", reply_markup=cancel_kb)
        return

    elif action == "view_org":
        await query.answer()
        org_id = int(d_id)
        await _render_team_details(context, query.message.chat_id, query.message.message_id, user_id, org_id, "all", "score", "desc", is_impersonating)
        return

    elif action == "team_grade_filter":
        await query.answer()
        org_id, gf, sort_field, sort_dir = int(d_id), data[2], data[3], data[4]
        await _render_team_details(context, query.message.chat_id, query.message.message_id, user_id, org_id, gf, sort_field, sort_dir)
        return

    elif action == "team_sort":
        await query.answer()
        org_id, gf, sort_field, sort_dir = int(d_id), data[2], data[3], data[4]
        await _render_team_details(context, query.message.chat_id, query.message.message_id, user_id, org_id, gf, sort_field, sort_dir)
        return

    elif action == "leave_org_warn":
        await query.answer()
        org_id = int(d_id)
        from src.database import db_get_user_org_role
        current_role = await asyncio.to_thread(db_get_user_org_role, user_id, org_id)
        if current_role == "creator":
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
                    # THE FIX: this is a SEPARATE message from the leaver's own
                    # confirmation (which already had a Close button) — this one,
                    # sent to the OTHER admins, was built with no reply_markup at
                    # all. Exactly the message in your screenshot.
                    leave_notify_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔚 CLOSE", callback_data="close_portal|0")]])
                    await context.bot.send_message(
                        chat_id=int(admin_id),
                        text=f"🚪 <b>{leaver_name}</b> has left your team.",
                        parse_mode="HTML",
                        reply_markup=leave_notify_kb
                    )
                except Exception:
                    pass

        close_kb = InlineKeyboardMarkup([[InlineKeyboardButton("👤 OPEN MY DASHBOARD", callback_data="privacy_menu|0")],
                                          [InlineKeyboardButton("🔚 CLOSE", callback_data="close_portal|0")]])
        await query.edit_message_text(msg, reply_markup=close_kb, parse_mode="HTML")
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
            is_city = (sug['kind'] == 'city')
            for uid in result.get("affected_users", []):
                try:
                    if approve:
                        nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("👤 MY PROFILE", callback_data="privacy_menu|0")]])
                        if is_city:
                            msg = f"✅ <b>Your city was approved!</b>\n📍 {html.escape(sug['name'])}, {html.escape(sug.get('country') or '')} now shows on your profile and leaderboards."
                        else:
                            msg = f"✅ <b>Your school was approved!</b>\n🏫 {html.escape(sug['name'])} is now visible and joinable by other students."
                    else:
                        nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("📍 REGISTER AGAIN", callback_data="regloc_start|0")]])
                        if is_city:
                            msg = (
                                f"🚫 <b>Your suggested city wasn't approved.</b>\n"
                                f"📍 <b>{html.escape(sug['name'])}</b> has been removed from your profile — "
                                f"your city &amp; country are now unset.\n\n"
                                f"⚠️ <b>You need a city and country on file to keep answering questions.</b> "
                                f"Tap below to register again with the correct spelling."
                            )
                        else:
                            msg = (
                                f"🚫 <b>Your suggested school wasn't approved.</b>\n"
                                f"🏫 <b>{html.escape(sug['name'])}</b> has been removed — you're no longer linked to it.\n\n"
                                f"You can register a different school anytime, or continue as \"Not a student.\""
                            )
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
        item_rows.append([InlineKeyboardButton("🔒 Closed Conversations", callback_data=f"loc_admin_browse|{kind}|closed:0")])
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
        try:
            thread = await asyncio.to_thread(db_get_location_suggestion_thread, ls_id)
            viewer_tz = await asyncio.to_thread(db_get_user_timezone, user_id)

            from src.rendering.html_views import build_location_suggestion_item_text
            text = build_location_suggestion_item_text(ls, thread, viewer_tz)

            rows = []
            # THE FIX: "MESSAGE STUDENT" used to be added TWICE — once here for
            # pending items, and again unconditionally below (since a pending item is
            # never closed). This is exactly the duplicate button in your screenshot.
            # Now added exactly once, in the is_closed block below, for every status.
            if ls['status'] == 'pending':
                rows.append([
                    InlineKeyboardButton("✅ APPROVE", callback_data=f"loc_review|{ls_id}|1"),
                    InlineKeyboardButton("🚫 REJECT", callback_data=f"loc_review|{ls_id}|0")
                ])
                rows.append([InlineKeyboardButton("⏳ PENDING QUEUE", callback_data=f"loc_review|{ls_id}|-1")])
            elif ls['status'] == 'rejected':
                rows.append([InlineKeyboardButton("✅ APPROVE INSTEAD", callback_data=f"loc_review|{ls_id}|1")])
            elif ls['status'] == 'approved':
                rows.append([InlineKeyboardButton("🚫 REJECT INSTEAD", callback_data=f"loc_review|{ls_id}|0")])

            if ls.get('is_closed'):
                rows.append([InlineKeyboardButton("🔓 REOPEN CONVERSATION", callback_data=f"loc_toggle_close|{ls_id}|0")])
            else:
                rows.append([InlineKeyboardButton("💬 MESSAGE STUDENT", callback_data=f"loc_review_msg|{ls_id}")])
                rows.append([InlineKeyboardButton("🔒 CLOSE CONVERSATION", callback_data=f"loc_toggle_close|{ls_id}|1")])
            if ls['kind'] == 'school' and ls.get('org_id'):
                rows.append([InlineKeyboardButton("🏫 VIEW SCHOOL DETAILS", callback_data=f"view_org|{ls['org_id']}")])
            rows.append([InlineKeyboardButton("🔙 QUEUE", callback_data=f"loc_admin_browse|{return_state.split(':')[0]}|{':'.join(return_state.split(':')[1:])}")])
            import time as _t
            nonce = f"\n<i>​{int(_t.time()*1000) % 100000}</i>"
            await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text + nonce, reply_markup=InlineKeyboardMarkup(rows))
            # Same double-answer bug as fb_item — was calling answer() up front AND
            # again in the except, so a failure here was completely silent. This screen
            # IS the dedicated admin↔student conversation page (full thread + REPLY,
            # which loops back here via _fsm_advance) — it was just never rendering.
            await query.answer()
        except Exception as item_err:
            traceback.print_exc()
            print(f"[LOC-ITEM-ERROR] ls_id={ls_id}: {item_err}", flush=True)
            await query.answer(f"Error: {type(item_err).__name__}: {str(item_err)[:150]}", show_alert=True)
        return

    elif action == "loc_user_item":
        ls_id = int(d_id)
        return_offset = data[2] if len(data) > 2 else "0"
        try:
            from src.database import db_get_location_suggestion, db_get_location_suggestion_thread
            ls = await asyncio.to_thread(db_get_location_suggestion, ls_id)
            if not ls or str(ls.get("submitted_by")) != str(user_id):
                await query.answer("Not found.", show_alert=True)
                return
            thread = await asyncio.to_thread(db_get_location_suggestion_thread, ls_id)
            viewer_tz = await asyncio.to_thread(db_get_user_timezone, user_id)
            from src.rendering.html_views import build_location_suggestion_item_text
            text = build_location_suggestion_item_text(ls, thread, viewer_tz)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("💬 REPLY", callback_data=f"loc_user_reply|{ls_id}")],
                [InlineKeyboardButton("🔙 BACK TO LIST", callback_data=f"my_feedback|{return_offset}")],
                [InlineKeyboardButton("👤 PROFILE", callback_data="privacy_menu|0")]
            ])
            await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=kb)
            await query.answer()
        except Exception as loc_item_err:
            traceback.print_exc()
            print(f"[LOC-USER-ITEM-ERROR] ls_id={ls_id}: {loc_item_err}", flush=True)
            await query.answer(f"Error: {type(loc_item_err).__name__}: {str(loc_item_err)[:150]}", show_alert=True)
        return

    elif action == "fsm_create_org":
        await query.answer()
        from src.database import db_check_user_permission
        if not await asyncio.to_thread(db_check_user_permission, user_id, "team_create"):
            await query.answer("🚫 You've been restricted from creating teams.", show_alert=True)
            return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 OPEN TEAM (anyone can join)", callback_data="create_team_open|0")],
            [InlineKeyboardButton("🔒 DEDICATED TEAM (restricted)", callback_data="create_team_dedicated_menu|0")],
            [InlineKeyboardButton("❌ CANCEL", callback_data="fsm_cancel|alliance_portal")]
        ])
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=(
                "<h2>✨ CREATE A TEAM</h2>\n"
                "<blockquote>"
                "🌐 <b>Open</b> — any student, from any city, country, or school, can join.\n\n"
                "🔒 <b>Dedicated</b> — restricted to students who share your own country, city, "
                "or school. Only options that match YOUR profile are offered."
                "</blockquote>"
            ),
            reply_markup=kb
        )
        return

    elif action == "create_team_open":
        await query.answer()
        USER_STATES[user_id] = "AWAITING_ORG_NAME"
        USER_PAYLOADS[user_id] = {"edit_mid": query.message.message_id, "team_scope": "open"}
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ CANCEL & RETURN", callback_data="fsm_cancel|alliance_portal")]])
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content="✍️ <b>NEW OPEN TEAM — Name</b>\n\nType the full formal name of your team:" + FSM_INPUT_HINT,
            reply_markup=cancel_kb
        )
        return

    elif action == "create_team_dedicated_menu":
        await query.answer()
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        if not profile:
            await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content="⚠️ Complete /start first.", reply_markup=return_kb)
            return

        country = profile.get("personal_country")
        city = profile.get("personal_city")
        has_school = bool(profile.get("org_id"))

        rows = []
        if country:
            rows.append([InlineKeyboardButton(f"🌍 Dedicated to {country}", callback_data="create_team_dedicated|country")])
        if city:
            rows.append([InlineKeyboardButton(f"🏙️ Dedicated to {city}", callback_data="create_team_dedicated|city")])
        if has_school:
            rows.append([InlineKeyboardButton("🏫 Dedicated to my school", callback_data="create_team_dedicated|school")])
        if not rows:
            await edit_rich_message_safe(
                context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
                html_content="⚠️ You need a city, country, or school on file first — set one from 📍 LOCATIONS &amp; SCHOOL.",
                reply_markup=return_kb
            )
            return
        rows.append([InlineKeyboardButton("🔙 BACK", callback_data="fsm_create_org|0")])
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content="<h2>🔒 DEDICATED TEAM</h2>\nYou can only dedicate a team to what's already on YOUR own profile:",
            reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    elif action == "create_team_dedicated":
        await query.answer()
        scope = d_id
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        scope_value = {"country": profile.get("personal_country"), "city": profile.get("personal_city"),
                        "school": profile.get("org_name")}.get(scope)
        USER_STATES[user_id] = "AWAITING_ORG_NAME"
        USER_PAYLOADS[user_id] = {
            "edit_mid": query.message.message_id, "team_scope": scope, "scope_value": scope_value,
            "org_city": profile.get("personal_city"), "org_country": profile.get("personal_country"),
        }
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data="create_team_dedicated_menu|0")]])
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=f"✍️ <b>NEW TEAM — dedicated to {html.escape(str(scope_value))}</b>\n\nType the full formal name of your team:" + FSM_INPUT_HINT,
            reply_markup=cancel_kb
        )
        return

    elif action == "team_visibility":
        await query.answer()
        is_public = (d_id == "1")
        USER_PAYLOADS.setdefault(user_id, {})["is_public"] = is_public
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ CANCEL", callback_data="fsm_cancel|alliance_portal")]])
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content="✍️ <b>ONE LAST THING — Team Description</b>\n\nWrite a short, amazing description so other students know what your team is about:" + FSM_INPUT_HINT,
            reply_markup=cancel_kb
        )
        return

    elif action == "confirm_team_invite":
        await query.answer()
        join_token = d_id
        user = query.from_user

        from src.database import db_join_organization_by_token, db_get_user_profile as _get_prof, db_set_user_referrer, db_count_referrals
        pre_existing_profile = await asyncio.to_thread(_get_prof, user_id)
        is_new_to_bot = pre_existing_profile is None

        join_data = await asyncio.to_thread(db_join_organization_by_token, user_id, join_token)
        if not join_data:
            msg = "⚠️ This team invite link is invalid or the team no longer exists."
        elif join_data.get("already_member"):
            msg = f"ℹ️ You're already on <b>{join_data['org_name']}</b> as <b>{join_data['role_assigned'].title()}</b> — nothing to do here."
        elif join_data.get("already_pending"):
            msg = f"📥 Your join request for <b>{join_data['org_name']}</b> is still pending admin approval."
        elif join_data["role_assigned"] == "pending":
            msg = f"📥 <b>Request sent!</b> <b>{join_data['org_name']}</b> requires admin approval."
            await _notify_org_admins_pending_request(context, join_data["org_id"], join_data["org_name"], user)
        else:
            msg = f"✅ <b>You're in!</b> You're now registered under <b>{join_data['org_name']}</b>."

        creator_id = join_data.get("creator_id") if join_data else None
        if is_new_to_bot and creator_id and str(creator_id) != str(user_id):
            linked = await asyncio.to_thread(db_set_user_referrer, user_id, creator_id)
            if linked:
                try:
                    ref_count = await asyncio.to_thread(db_count_referrals, creator_id)
                    new_name = html.escape(user.first_name or user.username or "A student")
                    should_notify = (ref_count <= 10) or (ref_count % 5 == 0)
                    if should_notify:
                        referral_nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("👤 GO TO PROFILE", callback_data="privacy_menu|0")]])
                        await context.bot.send_message(
                            chat_id=int(creator_id),
                            text=f"🤝 <b>{new_name}</b> joined the bot through your team's invite link!\nYou'll earn bonus marks from their correct answers.\n\n📊 Total referrals so far: <b>{ref_count}</b>",
                            parse_mode="HTML", reply_markup=referral_nav_kb
                        )
                except Exception:
                    pass

        # THE FIX (close button): every join/leave notification used to only offer "MY PROFILE" —
        # nothing to dismiss it with. Now every terminal notification gets a CLOSE option too.
        result_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 MY TEAM", callback_data=f"view_org|{join_data['org_id']}" if join_data and join_data.get("org_id") else "privacy_menu|0")],
            [InlineKeyboardButton("🔚 CLOSE", callback_data="close_portal|0")]
        ])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=msg, reply_markup=result_kb)
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
        if not profile or not profile.get("personal_city") or not profile.get("personal_country"):
            await query.answer("📍 Please set your city and country first via /start.", show_alert=True)
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
            await _notify_org_admins_pending_request(context, org_id, join_data['org_name'], query.from_user)
        else:
            text = f"✅ <b>You're in!</b> You're now registered under <b>{join_data['org_name']}</b>."
        close_kb = InlineKeyboardMarkup([[InlineKeyboardButton("👤 OPEN MY DASHBOARD", callback_data="privacy_menu|0")],
                                          [InlineKeyboardButton("🔚 CLOSE", callback_data="close_portal|0")]])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=close_kb)
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
        # THE FIX: this used to render as plain <code> text — copyable, but not
        # something you could tap to actually open. Now a real hyperlink that opens
        # the join flow directly, with the raw link still shown below for sharing/copy.
        invite_text = (
            f"🔗 <b>INVITE LINK FOR {html.escape(row['org_name'])}</b>\n\n"
            f"Tap below to preview the join screen yourself, or share the link so anyone who opens it "
            f"joins (or requests to join) your team automatically:\n\n"
            f"<a href='{invite_link}'>{invite_link}</a>"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 OPEN INVITE LINK", url=invite_link)],
            [InlineKeyboardButton("🔙 BACK TO TEAM", callback_data=f"view_org|{org_id}")]
        ])
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
        from src.database import db_check_user_permission
        if not await asyncio.to_thread(db_check_user_permission, user_id, "feedback"):
            await query.answer("🚫 You've been restricted from sending feedback. Contact an admin.", show_alert=True)
            return
        try:
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
            # THE FIX: answered ONCE, only after the work succeeds — same pattern already
            # used in fb_item/fb_kanban/loc_admin_item earlier in this thread. The old
            # version called query.answer() unconditionally up top with no try/except, so
            # any failure in edit_rich_message_safe (a bad state, a stale message, etc.)
            # had no way to surface — the tap would just look like nothing happened.
            await query.answer()
        except Exception as fb_cat_err:
            traceback.print_exc()
            print(f"[FB-CAT-ERROR] category={d_id} user={user_id}: {fb_cat_err}", flush=True)
            await query.answer(f"Error: {type(fb_cat_err).__name__}: {str(fb_cat_err)[:150]}", show_alert=True)
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
            viewer_tz = await asyncio.to_thread(db_get_user_timezone, user_id)
            kb = _build_feedback_detail_keyboard(fb_id, return_state, is_closed=fb.get('is_closed', False))
            await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=build_feedback_thread_text(fb, thread, viewer_tz), reply_markup=kb)

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
        from src.database import db_is_admin, db_get_feedback_by_id as _get_fb
        if not await asyncio.to_thread(db_is_admin, user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        fb_id = int(d_id)
        fb = await asyncio.to_thread(_get_fb, fb_id)
        if not fb:
            await query.answer("Not found.", show_alert=True)
            return
        if fb.get("is_closed"):
            await query.answer("This conversation is closed — reopen it first.", show_alert=True)
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

    elif action == "fb_toggle_close":
        # THE FIX: db_get_feedback_by_id is already imported at the top of callbacks.py.
        # Locally re-importing it HERE made Python treat it as local for the ENTIRE
        # _handle_callback_inner function (every elif branch shares one scope) — so
        # fb_reply/fb_item/fb_status/fb_view, which reference the bare name without
        # their own local import, crashed with UnboundLocalError the instant they ran.
        # This is the exact same bug class as db_get_user_timezone earlier in this thread.
        from src.database import db_is_admin, db_set_feedback_closed
        if not await asyncio.to_thread(db_is_admin, user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        fb_id, target = int(d_id), (data[2] == "1")
        await asyncio.to_thread(db_set_feedback_closed, fb_id, target)
        await query.answer("Closed — user can't reply until reopened." if target else "Reopened.")
        fb = await asyncio.to_thread(db_get_feedback_by_id, fb_id)
        thread = await asyncio.to_thread(db_get_feedback_thread, fb_id)
        viewer_tz = await asyncio.to_thread(db_get_user_timezone, user_id)
        # THE FIX: is_closed was never passed here — the keyboard always defaulted to
        # "not closed" regardless of what just happened, so REOPEN never actually showed.
        kb = _build_feedback_detail_keyboard(fb_id, None, is_closed=target)
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=build_feedback_thread_text(fb, thread, viewer_tz), reply_markup=kb)
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
        item_rows.append([InlineKeyboardButton("🔒 Closed Conversations", callback_data=f"fb_browse|{category}|closed:0")])

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
        from src.database import db_is_admin, db_get_feedback_recent_by_status, db_get_feedback_stats
        if not await asyncio.to_thread(db_is_admin, user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        try:
            stats = await asyncio.to_thread(db_get_feedback_stats)
            recent_by_status = await asyncio.to_thread(db_get_feedback_recent_by_status, 3)
            from src.rendering.html_views import build_feedback_kanban_text, build_feedback_kanban_keyboard
            text = build_feedback_kanban_text(stats, recent_by_status)
            kb = build_feedback_kanban_keyboard()
            import time as _t
            nonce = f"\n<i>​{int(_t.time()*1000) % 100000}</i>"
            await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text + nonce, reply_markup=kb)
            # Same double-answer bug — this is the confirmed root cause behind
            # "Kanban isn't even interactive." It IS its own message/page; it just
            # never got to show you why it was failing.
            await query.answer()
        except Exception as kanban_err:
            traceback.print_exc()
            print(f"[KANBAN-ERROR] {kanban_err}", flush=True)
            await query.answer(f"Kanban error: {type(kanban_err).__name__}: {str(kanban_err)[:150]}", show_alert=True)
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
        # THE FIX (new feature): the directory listed users as plain text with no way to open a

        # given one — "🔧 MANAGE" was described in an earlier pass but never actually landed here.
        # One button per row, opening a small per-user action menu (view / manage) instead of
        # cramming 2 buttons × 15 rows into one giant keyboard.
        buttons = []
        for u in users:
            label = format_public_name(u)[:24]
            buttons.append([InlineKeyboardButton(f"👤 {label}", callback_data=f"admin_user_actions|{u['user_id']}")])
        nav_row = []
        if offset > 0:
            nav_row.append(InlineKeyboardButton("⬅️ PREV", callback_data=f"admin_users|{max(0, offset-15)}"))
        if len(users) == 15:
            nav_row.append(InlineKeyboardButton("NEXT ➡️", callback_data=f"admin_users|{offset+15}"))
        if nav_row:
            buttons.append(nav_row)
        buttons.append([InlineKeyboardButton("🔙 BACK TO DASHBOARD", callback_data="admin_dashboard|0")])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=InlineKeyboardMarkup(buttons))
        return

    elif action == "my_feedback":
        offset = int(d_id)
        try:
            from src.database import db_get_user_feedback_and_requests, db_count_user_feedback_and_requests
            from src.rendering.html_views import build_user_feedback_requests_list_text
            items = await asyncio.to_thread(db_get_user_feedback_and_requests, user_id, 5, offset)
            total = await asyncio.to_thread(db_count_user_feedback_and_requests, user_id)
            text = build_user_feedback_requests_list_text(items, total)

            item_rows = []
            for item in items:
                label = str(item['label'])[:24]
                if item['kind'] == 'feedback':
                    item_rows.append([InlineKeyboardButton(f"#{item['id']} · {label}", callback_data=f"fb_view|{item['id']}|{offset}")])
                else:
                    item_rows.append([InlineKeyboardButton(f"#{item['id']} · {label}", callback_data=f"loc_user_item|{item['id']}|{offset}")])

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
            # THE FIX: same double-answer-on-exception bug as fb_item/loc_admin_item/fb_kanban —
            # query.answer() was called BEFORE the DB work, so a failure there had its error
            # alert silently swallowed by Telegram rejecting the second answer() call. Answered
            # exactly once now, only after the screen actually renders.
            await query.answer()
        except Exception as my_fb_err:
            traceback.print_exc()
            print(f"[MY-FEEDBACK-ERROR] user={user_id} offset={offset}: {my_fb_err}", flush=True)
            await query.answer(f"Error: {type(my_fb_err).__name__}: {str(my_fb_err)[:150]}", show_alert=True)
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
        fb_id = int(d_id)
        return_state = data[2] if len(data) > 2 else None
        fb = await asyncio.to_thread(db_get_feedback_by_id, fb_id)
        if not fb:
            await query.answer("Not found.", show_alert=True)
            return
        try:
            thread = await asyncio.to_thread(db_get_feedback_thread, fb_id)
            viewer_tz = await asyncio.to_thread(db_get_user_timezone, user_id)
            # THE FIX: without this, opening a closed item from the queue always
            # rendered "CLOSE" instead of "REOPEN" — is_closed was never read from fb.
            kb = _build_feedback_detail_keyboard(fb_id, return_state, is_closed=fb.get('is_closed', False))
            text = build_feedback_thread_text(fb, thread, viewer_tz)
            import time as _t
            nonce = f"\n<i>​{int(_t.time()*1000) % 100000}</i>"
            await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text + nonce, reply_markup=kb)
            # THE FIX: query.answer() was called ONCE up front, unconditionally, then
            # AGAIN inside this except block on failure. Telegram rejects a second
            # answer() on the same callback outright, which silently swallowed the exact
            # error alert this code was trying to show — a real rendering failure here
            # produced ZERO visible feedback. That is the entire "not responding" bug.
            # Now answered exactly once, only after the work actually succeeds.
            await query.answer()
        except Exception as fb_item_err:
            traceback.print_exc()
            print(f"[FB-ITEM-ERROR] fb_id={fb_id}: {fb_item_err}", flush=True)
            await query.answer(f"Error: {type(fb_item_err).__name__}: {str(fb_item_err)[:150]}", show_alert=True)
        return

    elif action == "fb_view":
        fb_id = int(d_id)
        return_offset = data[2] if len(data) > 2 else "0"
        try:
            fb = await asyncio.to_thread(db_get_feedback_by_id, fb_id)
            if not fb or str(fb.get("user_id")) != str(user_id):
                await query.answer("Not found.", show_alert=True)
                return
            thread = await asyncio.to_thread(db_get_feedback_thread, fb_id)
            viewer_tz = await asyncio.to_thread(db_get_user_timezone, user_id)
            # THE FIX: REPLY was always rendered regardless of is_closed — this is why
            # a student could still message into a conversation the admin had closed.
            reply_row = [] if fb.get('is_closed') else [InlineKeyboardButton("💬 REPLY", callback_data=f"fb_user_reply|{fb_id}|{return_offset}")]
            kb = InlineKeyboardMarkup([
                reply_row,
                [InlineKeyboardButton("🔙 BACK TO LIST", callback_data=f"my_feedback|{return_offset}")],
                [InlineKeyboardButton("👤 PROFILE", callback_data="privacy_menu|0")]
            ]) if reply_row else InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 BACK TO LIST", callback_data=f"my_feedback|{return_offset}")],
                [InlineKeyboardButton("👤 PROFILE", callback_data="privacy_menu|0")]
            ])
            await edit_rich_message_safe(
                context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
                html_content=build_feedback_thread_text(fb, thread, viewer_tz), reply_markup=kb
            )
            await query.answer()
        except Exception as fb_view_err:
            traceback.print_exc()
            print(f"[FB-VIEW-ERROR] fb_id={fb_id}: {fb_view_err}", flush=True)
            await query.answer(f"Error: {type(fb_view_err).__name__}: {str(fb_view_err)[:150]}", show_alert=True)
        return

    elif action == "fb_user_reply":
        fb_id = int(d_id)
        return_offset = data[2] if len(data) > 2 else "0"
        fb = await asyncio.to_thread(db_get_feedback_by_id, fb_id)
        if not fb or str(fb.get("user_id")) != str(user_id):
            await query.answer("Not found.", show_alert=True)
            return
        # THE FIX: this was the actual open door. loc_user_reply already checked
        # is_closed for location requests; this equivalent for feedback never did,
        # so a student could always re-enter reply mode no matter what the admin set.
        if fb.get('is_closed'):
            await query.answer("This conversation is closed — an admin needs to reopen it.", show_alert=True)
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
        kb = build_profile_main_keyboard(has_team=bool(profile.get("team_id")))
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

        # THE FIX: db_get_user_timezone is already imported at the top of this file.
        # Re-importing it locally HERE made Python treat it as a local variable for
        # the ENTIRE _handle_callback_inner function (all elif branches share one
        # scope) — so any other branch (loc_admin_item, fb_view, "My Feedback &
        # Requests") that used it WITHOUT its own local import crashed with
        # UnboundLocalError before this line ever ran. Removing the redundant import
        # here restores the normal module-level reference everywhere.
        from src.database import db_get_org_membership_log
        from src.rendering.html_views import build_org_history_text
        log_rows = await asyncio.to_thread(db_get_org_membership_log, org_id)
        viewer_tz = await asyncio.to_thread(db_get_user_timezone, user_id)
        text = build_org_history_text(org_details, log_rows, viewer_tz)

        pending_rows = [r for r in log_rows if r.get('state') == 'pending']
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
        from src.database import db_get_user_org_role
        actor_role = await asyncio.to_thread(db_get_user_org_role, user_id, org_id)
        if actor_role not in ("creator", "admin"):
            await query.answer("Only team admins can process requests.", show_alert=True)
            return

        if decision == "-1":
            await query.answer("Left pending — no action taken.")
            # THE FIX: this used to just answer and abandon the message as-is — "does
            # nothing" from the admin's perspective, with no way back to the request
            # list. Now it redraws the same MEMBERS & REQUESTS screen (with this
            # request still pending, still showing its own ✅/❌ buttons) so the admin
            # can come back and approve/reject it later from the exact same place.
            from src.database import db_get_org_membership_log, db_get_user_org_role
            actor_role = await asyncio.to_thread(db_get_user_org_role, user_id, org_id)
            if actor_role not in ("creator", "admin"):
                return
            conn = engine.get_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT * FROM organizations WHERE org_id = %s;", (org_id,))
                    org_details = cur.fetchone()
            finally:
                engine.release_connection(conn)
            if not org_details:
                return
            from src.rendering.html_views import build_org_history_text
            log_rows = await asyncio.to_thread(db_get_org_membership_log, org_id)
            # THE FIX: was a literal "UTC" string — a second, independent spot still
            # ignoring the viewer's own timezone. db_get_user_timezone is already
            # imported at the top of this file, no local import needed here.
            viewer_tz = await asyncio.to_thread(db_get_user_timezone, user_id)
            text = build_org_history_text(org_details, log_rows, viewer_tz)
            pending_rows = [r for r in log_rows if r.get('state') == 'pending']
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

        from src.database import db_get_user_question_matrix, db_count_user_question_matrix, db_is_admin
        from src.rendering.html_views import build_my_answers_list_text, build_my_answers_keyboard
        viewer_is_admin = await asyncio.to_thread(db_is_admin, user_id)
        rows = await asyncio.to_thread(db_get_user_question_matrix, user_id, subject, filter_mode, 8, offset, sort_field, sort_dir, viewer_is_admin)
        total = await asyncio.to_thread(db_count_user_question_matrix, user_id, subject, filter_mode, viewer_is_admin)
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

    elif action == "admin_manage_user":
        from src.database import db_is_admin, db_get_user_permissions
        if not await asyncio.to_thread(db_is_admin, user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        target = d_id
        await query.answer()
        perms = await asyncio.to_thread(db_get_user_permissions, target)
        def _row(key, label):
            on = perms.get(key, True)
            return [InlineKeyboardButton(f"{'✅' if on else '🚫'} {label}", callback_data=f"admin_toggle_perm|{target}|{key}|{0 if on else 1}")]
        kb = InlineKeyboardMarkup([
            _row("bot_access", "Bot access"),
            _row("feedback", "Feedback"),
            _row("requests", "Location/School requests"),
            _row("team_create", "Create teams"),
            [InlineKeyboardButton("🔙 DIRECTORY", callback_data="admin_users|0")]
        ])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=f"🔧 <b>Permissions — user <code>{target}</code></b>\n\nTap to toggle. ✅ = allowed, 🚫 = blocked.", reply_markup=kb)
        return

    elif action == "admin_toggle_perm":
        from src.database import db_is_admin, db_set_user_permission
        if not await asyncio.to_thread(db_is_admin, user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        target, perm_key, new_val = d_id, data[2], data[3] == "1"
        await asyncio.to_thread(db_set_user_permission, target, perm_key, new_val, user_id)
        await query.answer(f"{perm_key}: {'allowed' if new_val else 'blocked'}")
        # redraw same panel
        from src.database import db_get_user_permissions
        perms = await asyncio.to_thread(db_get_user_permissions, target)
        def _row(key, label):
            on = perms.get(key, True)
            return [InlineKeyboardButton(f"{'✅' if on else '🚫'} {label}", callback_data=f"admin_toggle_perm|{target}|{key}|{0 if on else 1}")]
        kb = InlineKeyboardMarkup([
            _row("bot_access", "Bot access"), _row("feedback", "Feedback"),
            _row("requests", "Location/School requests"), _row("team_create", "Create teams"),
            [InlineKeyboardButton("🔙 DIRECTORY", callback_data="admin_users|0")]
        ])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=f"🔧 <b>Permissions — user <code>{target}</code></b>\n\nTap to toggle.", reply_markup=kb)
        return

    elif action == "admin_user_actions":
        from src.database import db_is_admin, db_get_user_profile
        if not await asyncio.to_thread(db_is_admin, user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        target = d_id
        await query.answer()
        profile = await asyncio.to_thread(db_get_user_profile, target)
        name = format_public_name(profile) if profile else f"User {target}"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👁️ VIEW PROFILE (READ-ONLY)", callback_data=f"admin_view_profile|{target}")],
            [InlineKeyboardButton("🔧 PERMISSIONS", callback_data=f"admin_manage_user|{target}")],
            [InlineKeyboardButton("🔙 BACK TO DIRECTORY", callback_data="admin_users|0")]
        ])
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=f"<h3>👤 {html.escape(name)}</h3>\n<code>{target}</code>\n\nWhat would you like to do?",
            reply_markup=kb
        )
        return

    elif action == "admin_view_profile":
        # THE FIX: db_get_user_profile is already imported at the top of this file. Re-importing
        # it locally here made Python treat it as local for the ENTIRE _handle_callback_inner
        # function — every elif branch shares one scope — so privacy_menu/profile_popup, which
        # reference the bare name without their own local import, crashed with UnboundLocalError
        # before this line ever executed on their path. This is the same bug class fixed 4 times
        # already in this thread, from a different branch each time.
        from src.database import (
            db_is_admin, db_get_user_subject_marks,
            db_get_user_top_topic, db_get_user_rank_summary, db_check_impersonation_granted
        )
        if not await asyncio.to_thread(db_is_admin, user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        target = d_id
        await query.answer()
        profile = await asyncio.to_thread(db_get_user_profile, target)
        if not profile:
            await edit_rich_message_safe(
                context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
                html_content="⚠️ This user has no profile on file yet.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 DIRECTORY", callback_data="admin_users|0")]])
            )
            return
        subject_marks = await asyncio.to_thread(db_get_user_subject_marks, target)
        top_topic = await asyncio.to_thread(db_get_user_top_topic, target)
        rank_summary = await asyncio.to_thread(db_get_user_rank_summary, target)
        card_text = build_profile_card_text(profile, None, subject_marks, top_topic, rank_summary)

        # THE FIX (expanded read-only view): was profile-card-only. Now also pulls team
        # membership, feedback history, and location/school request history — everything an
        # admin might need to see about a user, in one screen, with zero edit controls.
        from src.database import db_get_user_dossier_for_admin
        from src.rendering.html_views import build_admin_dossier_text
        dossier = await asyncio.to_thread(db_get_user_dossier_for_admin, target)
        dossier_text = build_admin_dossier_text(dossier)

        text = (
            f"👁️ <b>ADMIN VIEW — READ ONLY</b>\n"
            f"Viewing <code>{target}</code>'s account exactly as they see it. "
            f"<i>Nothing here can be changed — updates only happen through 🎭 Act As This User.</i>\n"
            f"<hr/>\n{card_text}\n{dossier_text}"
        )
        already_granted = await asyncio.to_thread(db_check_impersonation_granted, target, user_id)
        kb_rows = []
        if already_granted:
            kb_rows.append([InlineKeyboardButton("🎭 ACT AS THIS USER", callback_data=f"imp_start|{target}")])
        else:
            kb_rows.append([InlineKeyboardButton("🎭 REQUEST TO ACT AS USER", callback_data=f"imp_request|{target}")])
        kb_rows.append([InlineKeyboardButton("🔙 BACK", callback_data=f"admin_user_actions|{target}")])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=text, reply_markup=InlineKeyboardMarkup(kb_rows))
        return

    elif action == "imp_request":
        from src.database import db_is_admin, db_request_impersonation
        if not await asyncio.to_thread(db_is_admin, user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        target = d_id
        await query.answer("Request sent to the student.")
        await asyncio.to_thread(db_request_impersonation, target, user_id)
        admin_name = html.escape(query.from_user.first_name or query.from_user.username or "An admin")
        consent_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ ALLOW", callback_data=f"imp_respond|{user_id}|1"),
            InlineKeyboardButton("❌ DENY", callback_data=f"imp_respond|{user_id}|0")
        ]])
        try:
            await context.bot.send_message(
                chat_id=int(target),
                text=(
                    f"🎭 <b>Support access request</b>\n\n"
                    f"<b>{admin_name}</b> (support team) would like to temporarily act as you in "
                    f"the bot — this helps them see and reproduce exactly what you're seeing while "
                    f"troubleshooting an issue.\n\n"
                    f"Nothing happens unless you tap ALLOW, and you can revoke this at any time from "
                    f"⚙️ Settings → 🎭 Revoke Support Access."
                ),
                parse_mode="HTML", reply_markup=consent_kb
            )
        except Exception:
            pass
        return

    elif action == "imp_respond":
        admin_id, allow = data[1], (data[2] == "1")
        from src.database import db_respond_impersonation
        await asyncio.to_thread(db_respond_impersonation, user_id, admin_id, allow)
        await query.answer("Access granted." if allow else "Request denied.")
        msg = (
            "✅ <b>Access granted.</b> The support team can now act as you until you revoke it "
            "from ⚙️ Settings → 🎭 Revoke Support Access."
            if allow else
            "❌ <b>Request denied.</b> Nothing on your account changes."
        )
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id, html_content=msg, reply_markup=None)
        if allow:
            try:
                await context.bot.send_message(chat_id=int(admin_id), text="✅ The student granted you support access. Reopen their profile and tap 🎭 ACT AS THIS USER.")
            except Exception:
                pass
        return

    elif action == "imp_start":
        from src.database import db_is_admin, db_check_impersonation_granted
        if not await asyncio.to_thread(db_is_admin, user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        target = d_id
        # THE FIX: re-verified fresh against the DB every time, not cached — this is what makes
        # a user's revoke actually take effect immediately, not just at the moment of the request.
        if not await asyncio.to_thread(db_check_impersonation_granted, target, user_id):
            await query.answer("This user hasn't granted you access (or has since revoked it).", show_alert=True)
            return
        from src.config import IMPERSONATION_SESSIONS
        IMPERSONATION_SESSIONS[str(user_id)] = str(target)
        await query.answer("🎭 Now acting as this user.")
        stop_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛑 STOP ACTING AS USER", callback_data="imp_stop|0")]])
        await edit_rich_message_safe(
            context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content=(
                f"🎭 <b>Acting as user <code>{target}</code></b>\n\n"
                f"Every button you tap now runs as if this student tapped it — their profile, "
                f"their team, their feedback. Tap 🛑 below the moment you're done."
            ),
            reply_markup=stop_kb
        )
        return

    elif action == "imp_stop":
        from src.config import IMPERSONATION_SESSIONS
        was_target = IMPERSONATION_SESSIONS.pop(str(user_id), None)
        await query.answer("Stopped acting as user." if was_target else "Not currently impersonating.")
        profile_nav_kb = InlineKeyboardMarkup([[InlineKeyboardButton("👤 MY PROFILE", callback_data="privacy_menu|0")]])
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content="✅ <b>Back to your own admin account.</b>", reply_markup=profile_nav_kb)
        return

    elif action == "revoke_impersonation":
        from src.database import db_revoke_all_impersonation_grants
        await asyncio.to_thread(db_revoke_all_impersonation_grants, user_id)
        await query.answer("Any support access you granted has been revoked.")
        profile = await asyncio.to_thread(db_get_user_profile, user_id)
        kb = build_profile_settings_keyboard(profile.get("public_consent_granted", False))
        await edit_rich_message_safe(context.bot, chat_id=query.message.chat_id, message_id=query.message.message_id,
            html_content="🎛️ <b>SETTINGS</b>\n<hr/>\nVisibility, nickname, grade, or location.", reply_markup=kb)
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

                from src.database import db_user_location_complete
                if not await asyncio.to_thread(db_user_location_complete, user_id):
                    await query.answer("📍 Set your city & country first — check your DM.", show_alert=True)
                    gate_kb = InlineKeyboardMarkup([[InlineKeyboardButton("📍 SET MY LOCATION NOW", callback_data="regloc_start|0")]])
                    await send_rich_message_safe(
                        context.bot, chat_id=user_id,
                        html_content="🚫 <b>Set your city &amp; country first</b>\n\nBefore you can answer questions, we need your city and country on file — even if it's still pending admin review, that's enough to unlock answering.",
                        reply_markup=gate_kb
                    )
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

            from src.database import db_user_location_complete
            if not await asyncio.to_thread(db_user_location_complete, user_id):
                await query.answer("📍 Set your city & country first — check your DM.", show_alert=True)
                gate_kb = InlineKeyboardMarkup([[InlineKeyboardButton("📍 SET MY LOCATION NOW", callback_data="regloc_start|0")]])
                await send_rich_message_safe(
                    context.bot, chat_id=user_id,
                    html_content="🚫 <b>Set your city &amp; country first</b>\n\nBefore you can answer questions, we need your city and country on file — even if it's still pending admin review, that's enough to unlock answering.",
                    reply_markup=gate_kb
                )
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