# src/rendering/html_views.py
import html
import re
from src.config import CONFIG
from src.typography import clean_latex_to_unicode, lite_math, beautify_markdown_math
from src.rendering.latex_templates import get_day_from_tags, sanitize_tag_to_hashtag, is_complex
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

def replace_code_with_italic(text: str) -> str:
    return text.replace("<code>", "<i>").replace("</code>", "</i>") if text else ""

def format_public_name(row) -> str:
    """
    Formats a user's display identity following a safe, descending fallback chain:
    1. Custom Nickname (User configured nickname)
    2. Real Telegram Username (Preceded with @, if available and consent is True)
    3. Real Telegram First Name (Sanitized, if available and consent is True)
    4. Masked User ID (Default fallback)
    """
    if not row:
        return "Scholar"

    nickname = row.get('nickname')
    username = row.get('username')
    first_name = row.get('first_name')
    user_id = str(row.get('user_id') or '')
    consent = row.get('public_consent_granted', False)

    if nickname and nickname.strip():
        return html.escape(nickname.strip())
        
    if consent:
        if username and username.strip():
            un = username.strip().lstrip('@')
            return f"@{html.escape(un)}"
        if first_name and first_name.strip():
            return html.escape(first_name.strip())

    masked_id = f"Scholar ...{user_id[-4:]}" if len(user_id) >= 4 else "Scholar"
    return masked_id

def smart_truncate_html(text: str, max_len: int) -> str:
    if not text or len(text) <= max_len:
        return text or ""

    tokens = re.split(r'(<[^>]+>)', text)
    accumulated = ""
    char_count = 0
    open_tags = []

    for token in tokens:
        if token.startswith("<") and token.endswith(">"):
            tag_match = re.match(r'</?([a-zA-Z1-6-]+)', token)
            if tag_match:
                tag_name = tag_match.group(1).lower()
                if token.startswith("</"):
                    if open_tags and open_tags[-1] == tag_name:
                        open_tags.pop()
                else:
                    if not token.endswith("/>") and tag_name != "hr" and tag_name != "br":
                        open_tags.append(tag_name)
            accumulated += token
        else:
            if char_count + len(token) > max_len:
                remaining = max_len - char_count
                accumulated += token[:remaining] + "..."
                break
            accumulated += token
            char_count += len(token)

    for tag in reversed(open_tags):
        accumulated += f'</{tag}>'

    if accumulated.count('$') % 2 != 0:
        accumulated += '$'

    return accumulated

def get_grade_mastery_title(marks: int) -> str:
    if marks == 0: return "🌱 Candidate (Practice)"
    if marks < 50: return "🛡️ Bronze Scholar"
    if marks < 150: return "⚔️ Silver Elite"
    if marks < 500: return "👑 Gold Master"
    if marks < 1200: return "💎 Platinum Grandmaster"
    return "🌌 Legend"

def get_next_rank_info(marks: int) -> str:
    if marks == 0: return "Solve 1 question to unlock <b>Bronze Scholar</b> rank!"
    if marks < 50: return f"Earn <b>{50 - marks} Marks</b> to unlock <b>Silver Elite</b>"
    if marks < 150: return f"Earn <b>{150 - marks} Marks</b> to unlock <b>Gold Master</b>"
    if marks < 500: return f"Earn <b>{500 - marks} Marks</b> to unlock <b>Platinum Grandmaster</b>"
    if marks < 1200: return f"Earn <b>{1200 - marks} Marks</b> to unlock <b>Legend</b>"
    return "Maximum Mastery Level Reached! 🌌"

def build_closed_static_view(q, display_id: str, compact=False, continuation=False, round_number: int = None, total_rounds: int = None) -> str:
    correct_letter = chr(65 + q['correct_option'])
    day_str = get_day_from_tags(q.get('tags', []))
    round_tag = f" • Round {round_number}/{total_rounds}" if round_number and total_rounds else ""

    hashtag_list = [sanitize_tag_to_hashtag(t) for t in q.get('tags', [])]
    channel_name = CONFIG.get("channel", "@QuizOva")
    channel_username = channel_name.lstrip('@')

    footer = (
        f"\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>REF <code>{display_id}</code></b>{round_tag} │ <a href='https://t.me/{channel_username}'>{channel_name}</a>\n"
        f"{' '.join(hashtag_list)}"
    )

    from src.rendering.latex_templates import has_real_diagram
    has_tikz = has_real_diagram(q)

    if compact:
        raw_question = beautify_markdown_math(q['question'])
        body_plain = f"<b>PROBLEM PROPOSITION</b>{round_tag}\n{raw_question}"

        opts_list = []
        for i, o in enumerate(q['options']):
            opts_list.append(f"• <b>{chr(65+i)})</b> {beautify_markdown_math(o)}")
        opts_block = "📋 <b>OPTIONS:</b>\n" + "\n".join(opts_list)

        spoiler_content = f"🎯 <b>CORRECT OPTION: [{correct_letter}]</b>"
        spoiler_block = f"🎯 <b>TAP TO REVEAL KEY ANSWER:</b>\n<tg-spoiler>{spoiler_content}</tg-spoiler>"

        components = [body_plain, opts_block, spoiler_block, footer]
        caption_text = "\n\n".join(components)

        from src.rendering.rich_helpers import convert_to_legacy_html
        legacy_text = convert_to_legacy_html(caption_text)

        if len(legacy_text) > 1000:
            excess = len(legacy_text) - 980
            allowed_question_len = max(150, len(raw_question) - excess)

            truncated_question = smart_truncate_html(raw_question, allowed_question_len)
            body_plain = f"<b>PROBLEM PROPOSITION</b>{round_tag}\n{truncated_question}"

            components = [body_plain, opts_block, spoiler_block, footer]
            caption_text = "\n\n".join(components)

        return caption_text

    banner = f"📚📚📚 <b>{q.get('subject','QUESTION').upper()}</b>{round_tag} 📚📚📚\n"
    body = (
        f"<blockquote>"
        f"<b>PROBLEM PROPOSITION</b><br/>"
        f"{beautify_markdown_math(q['question'])}"
        f"</blockquote>"
    )
    if has_tikz:
        body += '\n<p><img src="tg://photo?id=quiz_diagram"/></p>'

    opts_list = ["📋 <b>OPTIONS</b>", "<ul>"]
    for i, o in enumerate(q['options']):
        opts_list.append(f"  <li><b>{chr(65+i)})</b> {beautify_markdown_math(o)}</li>")
    opts_list.append("</ul>")
    opts_block = "\n".join(opts_list)

    exp = q.get("poll_explanation", {})
    why = exp.get('why', 'No detailed explanation provided.')
    rule_text = exp.get('governing_principle') or exp.get('rule') or 'General Concept'

    # IMPORTANT: no <blockquote> tags inside the spoiler below — Telegram
    # clients silently drop the spoiler blur when a blockquote is nested
    # inside <tg-spoiler>, even though the API accepts it. Grouping is done
    # with bold headers + dividers instead, so the whole thing stays hidden.
    general_principle = (
        f"🏛️ <b>GENERAL PRINCIPLE:</b>\n"
        f"<i>{beautify_markdown_math(rule_text)}</i>"
    )

    step_by_step_parts = [
        f"🔢 <b>STEP-BY-STEP DERIVATION:</b>\n"
        f"{beautify_markdown_math(why)}"
    ]
    if exp.get('analogy'):
        step_by_step_parts.append(f"💡 <b>Analogy:</b>\n{beautify_markdown_math(exp['analogy'])}")
    if exp.get('memory_tip'):
        step_by_step_parts.append(f"🧠 <b>Memory Tip:</b>\n{beautify_markdown_math(exp['memory_tip'])}")
    step_by_step = "\n\n".join(step_by_step_parts)

    options_analysis = q.get('options_analysis', [])
    breakdown_parts = [f"🔍 <b>OPTION BREAKDOWN</b>\n"]
    for i, o_text in enumerate(q['options']):
        let = chr(65 + i)
        is_correct = (i == q['correct_option'])
        status_icon = "🟢" if is_correct else "⚪"

        why_text = ""
        example_text = ""
        if i < len(options_analysis):
            why_text = options_analysis[i].get('why', '')
            example_text = options_analysis[i].get('example', '')

        analysis_line = f"{status_icon} <b>Option {let} ({beautify_markdown_math(o_text)}):</b> {beautify_markdown_math(why_text)}"
        if example_text:
            analysis_line += f"\n  {beautify_markdown_math(example_text)} ."
        breakdown_parts.append(analysis_line)
    breakdown_block = "\n".join(breakdown_parts)

    general_principle = replace_code_with_italic(general_principle)
    step_by_step = replace_code_with_italic(step_by_step)
    breakdown_block = replace_code_with_italic(breakdown_block)

    divider = "┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"
    spoiler_content = (
        f"🎯 <b>CORRECT OPTION: [{correct_letter}]</b>\n\n"
        f"{general_principle}\n{divider}\n"
        f"{step_by_step}\n{divider}\n"
        f"{breakdown_block}"
    )
    spoiler_content = spoiler_content.replace("<tg-math-block>", "<tg-math>").replace("</tg-math-block>", "</tg-math>")

    components = [
        banner,
        body,
        opts_block,
        f"<hr/>\n🎯 <b>TAP TO REVEAL KEY ANSWER & SOLUTION:</b>\n<tg-spoiler>{spoiler_content.strip()}</tg-spoiler>",
        footer
    ]

    if continuation:
        connection_header = f"<b>📖 DETAILED EXPLANATION SHEET • REF <code>{display_id}</code>{round_tag}</b>\n<hr/>"
        components = [
            banner,
            connection_header,
            body,
            opts_block,
            f"<hr/>\n🎯 <b>TAP TO REVEAL KEY ANSWER & SOLUTION:</b>\n<tg-spoiler>{spoiler_content.strip()}</tg-spoiler>",
            footer
        ]

    final_html = "\n\n".join(components)

    problems = check_tag_balance(final_html)
    if problems:
        from src.debug_log import dlog
        dlog(f"[TAG-BALANCE-WARNING] q_id={q.get('id')} display_id={display_id} issues: {problems}")

    return final_html

def build_answered_view(q, display_id: str, user_idx: int, show_derivation=False, show_perf=False, mode="compact", compact=None, perf_card=None, continuation=False) -> str:
    if compact is not None:
        show_derivation = not compact
        show_perf = False
    elif mode == "detailed":
        show_derivation = True
        show_perf = False
    elif mode == "performance":
        show_derivation = False
        show_perf = True
    elif mode in ["all", "both"]:
        show_derivation = True
        show_perf = True

    correct_idx = q['correct_option']
    letters = ["A", "B", "C", "D", "E"]
    user_letter = letters[user_idx] if user_idx < len(letters) else "?"
    user_status = "🟩 CORRECT" if user_idx == correct_idx else "🟥 INCORRECT"
    correct_letter = letters[correct_idx]

    exp = q.get("poll_explanation", {})
    why = exp.get('why', 'No step-by-step derivation available.')
    rule_text = exp.get('governing_principle') or exp.get('rule') or 'General Concept'

    general_principle = (
        f"<blockquote>"
        f"<b>🏛️ GENERAL PRINCIPLE:</b><br/>"
        f"<i>{beautify_markdown_math(rule_text)}</i>"
        f"</blockquote>"
    )

    step_by_step_parts = [
        f"<blockquote expandable>"
        f"<b>🔢 STEP-BY-STEP DERIVATION:</b>\n"
        f"{beautify_markdown_math(why)}"
    ]
    if exp.get('analogy'):
        step_by_step_parts.append(f"<b>💡 Analogy</b>\n{beautify_markdown_math(exp['analogy'])}")
    if exp.get('memory_tip'):
        step_by_step_parts.append(f"<b>🧠 Memory Tip</b>\n{beautify_markdown_math(exp['memory_tip'])}")
    step_by_step_parts.append("</blockquote>")

    step_by_step = "\n".join(step_by_step_parts)

    options_analysis = q.get('options_analysis', [])
    breakdown_parts = [
        f"<blockquote expandable>",
        f"<b>🔍 OPTION BREAKDOWN:</b>\n"
    ]
    for i, o_text in enumerate(q['options']):
        let = chr(65 + i)
        is_correct_opt = (i == correct_idx)
        status_icon = "🟢" if is_correct_opt else "⚪"

        why_text = ""
        example_text = ""
        if i < len(options_analysis):
            why_text = options_analysis[i].get('why', '')
            example_text = options_analysis[i].get('example', '')

        analysis_line = f"{status_icon} <b>Option {let} ({beautify_markdown_math(o_text)}):</b> {beautify_markdown_math(why_text)}"
        if example_text:
            analysis_line += f"\n  {beautify_markdown_math(example_text)}"
        breakdown_parts.append(analysis_line)
    breakdown_parts.append("</blockquote>")
    breakdown_block = "\n".join(breakdown_parts)

    general_principle = replace_code_with_italic(general_principle)
    step_by_step = replace_code_with_italic(step_by_step)
    breakdown_block = replace_code_with_italic(breakdown_block)

    score_segment = ""
    if show_perf and perf_card:
        is_repeat = not perf_card.get('first_try', True)
        orig_marks = perf_card.get('marks_awarded', 0)
        speed_tier = perf_card.get('speed_tier')

        status_prefix = "⚠️ <b>Score Locked (Previously Answered):</b><br/>" if is_repeat else ""

        tier_labels = {
            "lightning": "⚡ <b>LIGHTNING FAST!</b> (×1.5 speed bonus)",
            "fast": "🏃 <b>QUICK ANSWER!</b> (×1.2 speed bonus)",
            "standard": "🟩 <b>CORRECT!</b>",
        }

        if orig_marks > 0:
            tier_line = tier_labels.get(speed_tier, "🟩 <b>CORRECT!</b>")
            marks_notice = f"{status_prefix}{tier_line} <b>(+{orig_marks} Marks)</b>"
        else:
            marks_notice = f"{status_prefix}🟥 <b>INCORRECT.</b> No marks awarded. <b>(+0 Marks)</b>"

        master = get_grade_mastery_title(perf_card['total_marks'])

        # Display custom public nickname on their personal report card view
        display_name = perf_card.get('nickname') or "Not Set"

        score_segment = (
            f"<hr/>\n"
            f"📊 <b>STUDY PERFORMANCE CARD</b>\n"
            f"<p>{marks_notice}</p>\n"
            f"<table>"
            f"  <tr>"
            f"    <td>👤 <b>Public Nickname:</b></td>"
            f"    <td><b>{html.escape(display_name)}</b></td>"
            f"  </tr>"
            f"  <tr>"
            f"    <td>🎒 <b>Academic Level:</b></td>"
            f"    <td>Grade {perf_card.get('grade', 12)}</td>"
            f"  </tr>"
            f"  <tr>"
            f"    <td>📝 <b>Practice Score:</b></td>"
            f"    <td><b>{perf_card['total_marks']} Marks</b></td>"
            f"  </tr>"
            f"  <tr>"
            f"    <td>🏆 <b>Mastery Level:</b></td>"
            f"    <td><b>{master}</b></td>"
            f"  </tr>"
            f"  <tr>"
            f"    <td>🎯 <b>Accuracy Rate:</b></td>"
            f"    <td><b>{perf_card['accuracy']}%</b> ({perf_card['correct']} of {perf_card['total']})</td>"
            f"  </tr>"
            f"</table>"
        )

    if continuation:
        parts = []
        if show_derivation:
            parts.append(f"<b>📖 DERIVATION DETAILS:</b>\n{general_principle}\n{step_by_step}\n{breakdown_block}")
        if show_perf and score_segment:
            parts.append(score_segment)

        connection_header = f"<b>📝 DETAILED EXPLANATION SHEET • REF <code>{display_id}</code></b>\n<hr/>"
        return f"{connection_header}\n" + "\n\n".join(parts)

    body = (
        f"<blockquote>"
        f"<b>PROBLEM PROPOSITION</b><br/>"
        f"{beautify_markdown_math(q['question'])}"
        f"</blockquote>"
    )
    from src.rendering.latex_templates import has_real_diagram
    if has_real_diagram(q):
        body += '\n<p><img src="tg://photo?id=quiz_diagram"/></p>'

    user_val = q['options'][user_idx] if user_idx < len(q['options']) else "Unknown"
    correct_val = q['options'][correct_idx]

    status_block = (
        f"<hr/>\n"
        f"🎯 <b>Your Selection:</b> {user_letter} │ {lite_math(user_val)} ({user_status})\n"
        f"⭐ <b>Correct Option:</b> <b>[{correct_letter} │ {lite_math(correct_val)}]</b>"
    )

    opts_block = ""
    explanation_block = ""
    analysis_block = ""

    if show_derivation:
        opts_list = ["📋 <b>OPTIONS</b>", "<ul>"]
        for i, o in enumerate(q['options']):
            opts_list.append(f"  <li><b>{chr(65+i)})</b> {beautify_markdown_math(o)}</li>")
        opts_list.append("</ul>")
        opts_block = "\n" + "\n".join(opts_list)
        explanation_block = f"\n{general_principle}\n{step_by_step}"
        analysis_block = f"\n{breakdown_block}"

    hashtag_list = [sanitize_tag_to_hashtag(t) for t in q.get('tags', [])]
    channel_name = CONFIG.get("channel", "@QuizOva")
    channel_username = channel_name.lstrip('@')

    footer = (
        f"\n<hr/>\n"
        f"<b>REF <code>{display_id}</code></b> │ <a href='https://t.me/{channel_username}'>{channel_name}</a>\n"
        f"{' '.join(hashtag_list)}"
    )

    components = [body]
    if opts_block:
        components.append(opts_block)
    components.append(status_block)
    if explanation_block:
        components.append(explanation_block)
    if analysis_block:
        components.append(analysis_block)
    if score_segment:
        components.append(score_segment)
    components.append(footer)

    return "\n".join(components)

def build_answered_keyboard(d_id: str, user_selection: int, show_derivation: bool, show_perf: bool, is_photo=False, message_id: str = None) -> InlineKeyboardMarkup:
    prefix = "toggle_photo" if is_photo else "toggle"
    buttons = []

    if show_derivation:
        buttons.append([InlineKeyboardButton("↩️ HIDE SOLUTION DETAILS", callback_data=f"{prefix}|{d_id}|{user_selection}|0|{1 if show_perf else 0}")])
    else:
        buttons.append([InlineKeyboardButton("📖 REVEAL COMPLETE DERIVATION", callback_data=f"{prefix}|{d_id}|{user_selection}|1|{1 if show_perf else 0}")])

    if show_perf:
        buttons.append([InlineKeyboardButton("↩️ HIDE PERFORMANCE CARD", callback_data=f"{prefix}|{d_id}|{user_selection}|{1 if show_derivation else 0}|0")])
    else:
        buttons.append([InlineKeyboardButton("📊 VIEW PERFORMANCE CARD", callback_data=f"{prefix}|{d_id}|{user_selection}|{1 if show_derivation else 0}|1")])

    channel_username = CONFIG.get("channel", "QuizOva").lstrip('@')
    if message_id:
        return_url = f"https://t.me/{channel_username}/{message_id}"
    else:
        return_url = f"https://t.me/{channel_username}"

    buttons.append([InlineKeyboardButton("📣 RETURN TO CHANNEL", url=return_url)])
    return InlineKeyboardMarkup(buttons)

def build_keyboard(q, display_id: str) -> InlineKeyboardMarkup:
    letters = ["𝗔", "𝗕", "𝗖", "𝗗", "𝗘"]
    bot_user = CONFIG.get("bot_username", "QuizOvaBot")
    buttons = []
    for i, opt in enumerate(q['options']):
        clean_opt = lite_math(opt)
        label = f"{letters[i]} │ {clean_opt}"
        url = f"https://t.me/{bot_user}?start=ans_{display_id}_{i}"
        buttons.append([InlineKeyboardButton(label, url=url)])
    return InlineKeyboardMarkup(buttons)

def build_interactive_keyboard(q, display_id: str) -> InlineKeyboardMarkup:
    letters = ["𝗔", "𝗕", "𝗖", "𝗗", "𝗘"]
    buttons = []
    for i, opt in enumerate(q['options']):
        clean_opt = lite_math(opt)
        label = f"{letters[i]} │ {clean_opt}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"ans|{display_id}|{i}")])
    return InlineKeyboardMarkup(buttons)

def generate_poll_hint(q):
    exp = q.get("poll_explanation", {})
    custom_hint = exp.get("poll_hint") or exp.get("hint")
    if custom_hint:
        cleaned = clean_latex_to_unicode(custom_hint)
        return cleaned[:195] if len(cleaned) > 195 else cleaned
    clean_rule = lite_math(exp.get("governing_principle") or exp.get("rule") or "")
    clean_why = lite_math(exp.get("why", ""))
    if clean_rule:
        combined = f"Rule: {clean_rule}"
        equations = re.findall(r'([A-Za-z\d\-\[\]\(\)]+\s*=\s*[^.\n]+)', clean_why)
        if equations and len(f"{combined} | {equations[-1].strip()}") <= 195:
            return f"{combined} | {equations[-1].strip()}"
        if len(combined) <= 195:
            return combined
    for sentence in re.split(r'(?<=[.!?])\s+', clean_why):
        if len(sentence) <= 195 and any(sym in sentence for sym in ["=", "√", "∫", "π", "θ", "°"]):
            return sentence
    return f"Apply {clean_rule[:100]}."[:195] if clean_rule else "Check Premium UI for derivations."[:195]


def build_tournament_announcement_text(meta: dict, remaining_delay: int = None) -> str:
    """
    Builds a highly structured HTML card for upcoming tournament showdowns.
    """
    subject = meta.get("subject", "GENERAL").upper()
    topics = meta.get("topics", [])
    diff_summary = meta.get("difficulty_summary", "N/A")
    total_count = meta.get("total_count", 0)
    round_seconds = meta.get("round_seconds", 60)
    cooldown_seconds = meta.get("cooldown_seconds", 15)
    
    time_utc = meta.get("target_time_utc", "N/A")
    time_eat = meta.get("target_time_eat", "N/A")
    
    topics_formatted = " │ ".join([f"<code>{t}</code>" for t in topics[:4]])
    if len(topics) > 4:
        topics_formatted += f" and {len(topics) - 4} more"

    if remaining_delay is not None and remaining_delay > 0:
        mins, secs = divmod(remaining_delay, 60)
        countdown_lbl = f"⏳ <b>COUNTDOWN:</b> <code>{mins}m {secs:02d}s remaining</code>"
    else:
        countdown_lbl = "⚔️ <b>STATUS:</b> <code>Match starting now...</code>"

    text = (
        f"📢 <b>LIVE TOURNAMENT SHOWDOWN ALERT</b> ⚔️\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Scholars, prepare yourself! A live synchronized quiz tournament series is about to begin. "
        f"Compete in real-time with other active peers.\n\n"
        f"📋 <b>EXAMINATION CANVAS SCOPE:</b>\n"
        f" ├─ 📚 <b>Subject:</b> <b>{subject}</b>\n"
        f" ├─ 🏷️  <b>Topics Covered:</b> {topics_formatted}\n"
        f" ├─ 📈 <b>Difficulty Curve:</b> <code>{diff_summary}</code>\n"
        f" └─ 🔢 <b>Total Questions:</b> <code>{total_count} exercises</code>\n\n"
        f"⏱️ <b>ROUND METRICS & TIMING:</b>\n"
        f" ├─ ⏳ <b>Active Round Timer:</b> <code>{round_seconds} seconds</code>\n"
        f" └─ ❄️ <b>Rest/Interval Cooldown:</b> <code>{cooldown_seconds} seconds</code>\n\n"
        f"📅 <b>SYNCHRONIZED GLOBAL START TIME:</b>\n"
        f" ├─ 🌍 <b>Universal Time:</b> <code>{time_utc}</code>\n"
        f" ├─ 🇪🇹 <b>East African Time:</b> <code>{time_eat}</code>\n"
        f" └─ {countdown_lbl}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 <i>Tip: Set your notifications ON! Correct and rapid answers earn speed-bonus leaderboard marks.</i>"
    )
    return text


def build_profile_card_text(profile: dict, roster: list = None, subject_marks: list = None) -> str:
    name = format_public_name(profile)
    grade = profile.get("grade") or "—"
    marks = profile.get("total_marks", 0)
    streak = profile.get("current_streak", 0)
    total = profile.get("total", 0)
    correct = profile.get("correct", 0)
    accuracy = int((correct / total) * 100) if total > 0 else 0
    mastery = get_grade_mastery_title(marks)
    next_rank = get_next_rank_info(marks)
    visibility = "🟢 Public" if profile.get("public_consent_granted") else "🕵️ Private"
    city = profile.get("personal_city") or "Not set"

    org_tag = profile.get("org_tag")
    org_name = profile.get("org_name")
    team_line = (
        f"🏫 <b>{html.escape(org_name)}</b> <code>#{org_tag}</code>"
        if org_tag else
        "🏫 <i>No team yet — tap MY TEAM to join or create one</i>"
    )

    subj_line = ""
    if subject_marks:
        top = sorted(subject_marks, key=lambda s: s['marks'], reverse=True)[:2]
        names = [html.escape(str(s['subject']).title()) for s in top]
        subj_line = f"\n📚 <b>Top:</b> {' · '.join(names)}"

    return (
        f"👤 <b>{name}</b>  ·  Grade {grade}\n"
        f"{mastery}\n"
        f"<hr/>\n"
        f"<b>{marks} Marks</b>  ·  🔥 {streak}d streak  ·  🎯 {accuracy}%\n"
        f"<i>{next_rank}</i>{subj_line}\n"
        f"<hr/>\n"
        f"{visibility}  ·  📍 {city}\n"
        f"{team_line}"
    )


def build_profile_main_keyboard(has_team: bool) -> InlineKeyboardMarkup:
    team_label = "🏫 MY TEAM" if has_team else "🏰 JOIN / CREATE TEAM"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎛️ SETTINGS", callback_data="settings_menu|0"),
         InlineKeyboardButton(team_label, callback_data="alliance_portal|0")],
        [InlineKeyboardButton("📖 HOW IT WORKS", callback_data="full_docs|0"),
         InlineKeyboardButton("🔙 CLOSE", callback_data="close_portal|0")]
    ])


def build_profile_settings_keyboard(public_consent_granted: bool) -> InlineKeyboardMarkup:
    consent_label = "🔴 GO PRIVATE" if public_consent_granted else "🟢 GO PUBLIC"
    consent_target = "0" if public_consent_granted else "1"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(consent_label, callback_data=f"toggle_consent|{consent_target}")],
        [InlineKeyboardButton("✍️ NICKNAME", callback_data="set_nick_fsm|0"),
         InlineKeyboardButton("🎒 GRADE", callback_data="reselect_grade_panel|0")],
        [InlineKeyboardButton("📍 LOCATION", callback_data="set_location_fsm|0")],
        [InlineKeyboardButton("🔙 BACK TO PROFILE", callback_data="privacy_menu|0")]
    ])

def build_alliance_info_text() -> str:
    """Single explainer card: how to reference/join a team, how scoring works, and roles."""
    return (
        f"❓ <b>HOW STUDY ALLIANCES WORK</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔑 <b>1. Referencing a Team</b>\n"
        f"Every team has a short <b>Team Code</b> (e.g. <code>#ABYSSINIA</code>) set by its creator. Get the code "
        f"from your school admin, then tap <b>🔑 JOIN TEAM</b> and enter it — or use <code>/school CODE</code> directly.\n\n"
        f"🏆 <b>2. How Scoring Works</b>\n"
        f"Nothing extra to do — every correct answer you submit automatically adds the same Marks to your "
        f"team's collective total. Your personal score and your team's score grow from the same answers.\n\n"
        f"🌆 <b>3. City &amp; Country Standings</b>\n"
        f"On a team? Your team's registered city/country counts toward the City/Country leaderboards. "
        f"Solo? Set your own from your Profile (📍 UPDATE MY LOCATION) so your answers still count locally.\n\n"
        f"👑 <b>4. Roles</b>\n"
        f"<blockquote>"
        f"👑 <b>Creator</b> — approves join requests, can dissolve the team\n"
        f"🛡️ <b>Admin</b> — promoted by the creator, helps manage members\n"
        f"👤 <b>Member</b> — standard roster spot, scores for the team\n"
        f"</blockquote>\n"
        f"🌐 <b>Public teams</b> need creator approval to join. 🔒 <b>Private teams</b> let anyone with the code join instantly.\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━"
    )


def build_organization_card_text(org: dict, roster: list) -> str:
    """
    Builds a beautiful, simple, and expandable card displaying details and 
    member rosters for a specific school team.
    """
    name = org.get("org_name", "UNKNOWN").upper()
    tag = org.get("org_tag", "UNKNOWN")
    org_type = org.get("org_type", "School")
    privacy = "🌐 PUBLIC TEAM (Requires manual admission confirmation)" if org.get("is_public", True) else "🔒 PRIVATE TEAM (Direct access passcode Tag)"
    
    total_score = sum(r.get("total_marks", 0) for r in roster)
    avg_score = int(total_score / len(roster)) if roster else 0
    
    roster_lines = []
    medals = ["🥇", "🥈", "🥉", "▫️", "▫️", "▫️", "▫️", "▫️", "▫️", "▫️"]
    for idx, r in enumerate(roster[:10]):
        formatted_name = format_public_name(r)
        role_marker = " 👑" if r.get("org_role") == "creator" else " 🛡️" if r.get("org_role") == "admin" else ""
        roster_lines.append(f" {medals[idx]} <code>{formatted_name}</code> — <b>{r['total_marks']} Marks</b>{role_marker}")
        
    roster_block =  "\n".join(roster_lines) if roster_lines else "<i>No active scholars registered.</i>"

    text = (
        f"🏫 <b>SCHOOL TEAM: {name}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"• <b>Domain Code:</b> <code>#{tag}</code>\n"
        f"• <b>Admission Protocol:</b> {privacy}\n"
        f"• <b>Alliance Category:</b> {org_type}\n\n"
        f"📊 <b>TEAM STATS:</b>\n"
        f" ├─ Members: <code>{len(roster)} registered</code>\n"
        f" ├─ Total Team Score: <code>{total_score} Marks</code>\n"
        f" └─ Team Average: <code>{avg_score} Marks</code>\n\n"
        f"🏆 <b>TEAM LEADERBOARD (TOP 10):</b>\n"
        f"<blockquote expandable>\n"
        f"{roster_block}\n"
        f"</blockquote>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━"
    )
    return text


def build_comparative_standings_text(top_alliances: list, user_org: dict = None) -> str:
    """
    Renders an exceptionally beautiful league table comparing registered study groups.
    """
    lines = []
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    
    user_rank_str = "<i>You are not currently linked to any listed school teams.</i>"
    
    for idx, org in enumerate(top_alliances[:10]):
        is_user_team = user_org and (int(org['org_id']) == int(user_org['org_id']))
        tag_marker = f" <b>(Your Team)</b>" if is_user_team else ""
        lines.append(f" {medals[idx]} <code>#{org['org_tag']}</code> — <b>{org['total_score']} Marks</b> ({org['active_members']} members){tag_marker}")
        
        if is_user_team:
            user_rank_str = f"🏆 Your group <code>#{org['org_tag']}</code> is currently ranked <b>#{idx+1} globally</b>!"

    league_block = "\n".join(lines) if lines else "<i>No alliances have registered competitive scores yet.</i>"

    text = (
        f"📊 <b>GLOBAL ALLIANCE COMPARATIVE LEAGUE</b> 📊\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Review how school teams across the nation compare in collective academic mastery:\n\n"
        f"<blockquote expandable>\n"
        f"{league_block}\n"
        f"</blockquote>\n\n"
        f"<b>Your Comparative Standing:</b>\n"
        f"└─ {user_rank_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━"
    )
    return text


def build_champions_podium_html(ind_val: str, sch_val: str, city_val: str, cnt_val: str) -> str:
    """
    Grand finale standings card. Distinct crown banner + one expandable
    blockquote (collapsed by default) so this reads as a single compact
    card instead of blending into the round-complete/question cards.
    """
    text = (
        f"👑 <b>TOURNAMENT SERIES COMPLETE</b> 👑\n\n"
        f"All rounds in this synchronized showdown have been resolved!\n\n"
        f"<blockquote expandable>"
        f"🎖️ <b>GRAND CHAMPIONS LEAGUE</b>\n\n"
        f"🥇 <b>Individual:</b> {ind_val}\n"
        f"🏫 <b>School Alliance:</b> #{sch_val}\n"
        f"🌆 <b>City:</b> {city_val}\n"
        f"🌍 <b>Country:</b> {cnt_val}"
        f"</blockquote>\n\n"
        f"<i>Daily practice builds permanent mastery. See you at the next live challenge!</i> 🎓"
    )
    return text

def check_tag_balance(html_str: str) -> list:
    """
    Scans assembled HTML for unclosed/mismatched tags before it's sent to Telegram.
    Returns a list of problem descriptions (empty list = balanced). Self-closing
    tags (<br/>, <hr/>) and void tags (br, hr) are ignored.
    """
    stack = []
    problems = []
    for m in re.finditer(r'</?([a-zA-Z][\w-]*)[^>]*>', html_str):
        tag = m.group(1).lower()
        if tag in ("br", "hr"):
            continue
        is_close = m.group(0).startswith("</")
        is_self_closing = m.group(0).endswith("/>")
        if is_self_closing:
            continue
        if is_close:
            if stack and stack[-1] == tag:
                stack.pop()
            else:
                problems.append(f"unexpected closing </{tag}> at pos {m.start()}")
        else:
            stack.append(tag)
    if stack:
        problems.append(f"unclosed tag(s) at end of string: {stack}")
    return problems

def build_round_completion_text(display_id, total_users: int, accuracy_pct: int, podium_lines: list, alliance_lines: list, round_number: int = None, total_rounds: int = None) -> str:
    """
    'Round complete' scoreboard card. Checkered-flag banner (unique from the
    champions/question cards) + one expandable blockquote (collapsed by
    default) instead of two separate stacked blockquote boxes.
    """
    podium_block = "\n".join(podium_lines) if podium_lines else "<i>No correct answers recorded this round.</i>"
    round_tag = f" • Round {round_number}/{total_rounds}" if round_number and total_rounds else ""

    body = (
        f"🏁 <b>ROUND COMPLETE</b>{round_tag} 🏁\n"
        f"<b>REF <code>{display_id}</code></b>\n\n"
        f"👥 <b>{total_users}</b> submissions logged  │  🎯 <b>{accuracy_pct}%</b> accuracy\n"
    )

    stats_block = (
        f"<blockquote expandable>"
        f"🏆 <b>ROUND PODIUM (FASTEST CORRECT)</b>\n\n"
        f"{podium_block}"
    )
    if alliance_lines:
        stats_block += (
            f"\n\n🏫 <b>ALLIANCE GAINS THIS ROUND</b>\n\n"
            f"{chr(10).join(alliance_lines)}"
        )
    stats_block += "</blockquote>"

    return f"{body}\n{stats_block}"



HELP_TOPICS = {
    "start": ("🗺️ Quick Start", (
        "<b>🗺️ QUICK START</b>\n"
        "1️⃣ /start → pick your grade\n"
        "2️⃣ Answer in the channel → get scored by DM\n"
        "3️⃣ /profile → nickname, team, visibility\n"
        "4️⃣ /leaderboard → track your rank"
    )),
    "scoring": ("🏆 Scoring & Ranks", (
        "<b>🏆 HOW POINTS WORK</b>\n"
        "<code>Points = Difficulty × Speed × Challenge × Streak</code>\n\n"
        "🟢 Easy 3  │  🟡 Medium 6  │  🔴 Hard 12\n"
        "⚡ ≤60s ×1.5  │  🏃 ≤5min ×1.2  │  🚶 after ×1.0\n"
        "🎯 Above grade ×1.5  │  Same ×1.0  │  Below ×0.3\n\n"
        "<b>🎖️ Ranks</b>\n"
        "🌱 0 │ 🛡️ 1–49 │ ⚔️ 50–149 │ 👑 150–499 │ 💎 500–1199 │ 🌌 1200+"
    )),
    "streaks": ("🔥 Streaks", (
        "<b>🔥 DAILY STREAKS</b>\n"
        "Answer once a day to keep it alive.\n\n"
        "Days 1–2 ×1.0 │ Days 3–6 ×1.2 │ Day 7+ ×1.5\n\n"
        "Miss a day → resets to 1. Lifetime score is never touched."
    )),
    "tournaments": ("⚔️ Tournaments", (
        "<b>⚔️ LIVE TOURNAMENTS</b>\n"
        "Everyone answers the same question on the same clock.\n\n"
        "🏆 Round podium — top 3 fastest correct\n"
        "📊 Tournament-only leaderboard\n"
        "🔁 Every answer also counts toward your lifetime score\n\n"
        "Missing a round costs nothing."
    )),
    "teams": ("🏫 Study Teams", (
        "<b>🏫 STUDY ALLIANCES</b>\n"
        "Join or create from /profile → 🏰 MY TEAM.\n\n"
        "✅ Correct answers auto-add to your team's total\n"
        "🌐 Public teams need approval │ 🔒 Private join instantly\n"
        "🚪 Leaving never affects your personal score"
    )),
    "invite": ("🤝 Invite & Earn", (
        "<b>🤝 INVITE FRIENDS</b>\n"
        "/invite gives your personal link.\n\n"
        "When someone joins and answers correctly, you earn a small "
        "share — two levels deep. No reward for recruiting alone."
    )),
    "privacy": ("🔐 Privacy", (
        "<b>🔐 YOUR PRIVACY</b>\n"
        "🕵️ Private (default) — anonymous ID\n"
        "🟢 Public (opt-in) — your username/name\n"
        "✍️ Nickname — always available\n\n"
        "No selling, no ads. Toggle anytime in /profile → ⚙️ Settings."
    )),
    "commands": ("📋 Commands", (
        "<b>📋 ALL COMMANDS</b>\n"
        "/start — set grade\n"
        "/profile — score, streak, team\n"
        "/leaderboard — weekly rank\n"
        "/invite — referral link\n"
        "/feedback — report or track issues\n"
        "/name — set/clear nickname\n"
        "/school CODE — join a team"
    )),
}


def build_help_menu_text() -> str:
    return "<b>🎓 QUIZ MASTER PRO — HELP</b>\nPick a topic below 👇"


def build_help_menu_keyboard() -> InlineKeyboardMarkup:
    keys = list(HELP_TOPICS.keys())
    rows = []
    for i in range(0, len(keys), 2):
        rows.append([
            InlineKeyboardButton(HELP_TOPICS[k][0], callback_data=f"help_topic|{k}")
            for k in keys[i:i+2]
        ])
    rows.append([InlineKeyboardButton("🔙 CLOSE", callback_data="close_portal|0")])
    return InlineKeyboardMarkup(rows)


def build_help_topic_text(key: str) -> str:
    topic = HELP_TOPICS.get(key)
    return topic[1] if topic else "⚠️ Topic not found."


def build_help_topic_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 BACK TO HELP", callback_data="help_menu|0")
    ]])


def build_feedback_stats_text(stats: dict) -> str:
    from src.config import FEEDBACK_CATEGORIES, FEEDBACK_STATUS_LABELS
    cat_lines = [f" • {label}: <b>{stats['by_category'].get(key, 0)}</b>" for key, label in FEEDBACK_CATEGORIES.items()]
    status_lines = [f" • {label}: <b>{stats['by_status'].get(key, 0)}</b>" for key, label in FEEDBACK_STATUS_LABELS.items()]
    return (
        f"<h2>📊 FEEDBACK DASHBOARD</h2>\n<hr/>\n"
        f"<b>Total submissions:</b> {stats['total']}\n\n"
        f"<b>By Category</b>\n" + "\n".join(cat_lines) + "\n\n"
        f"<b>By Status</b>\n" + "\n".join(status_lines)
    )

def build_admin_dashboard_text(stats: dict) -> str:
    """Rich-text admin dashboard overview — sent via send_rich_message_safe so <table> renders
    as Telegram's native rich table, not the legacy bullet-list fallback."""
    if not stats:
        return "<h2>⚠️ ADMIN DASHBOARD</h2>\n<hr/>\nFailed to load stats — check the database connection."

    country_rows = "".join(
        f"<tr><td>{html.escape(str(r['country']))}</td><td>{r['cnt']}</td></tr>"
        for r in stats["by_country"]
    ) or "<tr><td colspan='2'><i>No location data yet.</i></td></tr>"

    subject_rows = "".join(
        f"<tr><td>{html.escape(str(r['subject']))}</td><td>{r['cnt']}</td></tr>"
        for r in stats["by_subject"]
    ) or "<tr><td colspan='2'><i>No questions imported yet.</i></td></tr>"

    return (
        "<h2>📊 ADMIN DASHBOARD</h2>\n<hr/>\n"
        f"<b>👥 Total Registered Students:</b> {stats['total_users']}\n"
        f"<b>🏫 Total School Teams:</b> {stats['total_orgs']}\n"
        f"<b>📝 Total Question Bank Size:</b> {stats['total_questions']}\n"
        f"<b>✅ Total Answers Submitted:</b> {stats['total_responses']}\n\n"
        "<h3>🌍 Students by Country</h3>\n"
        f"<table><tr><td><b>Country</b></td><td><b>Students</b></td></tr>{country_rows}</table>\n\n"
        "<h3>📚 Questions by Subject</h3>\n"
        f"<table><tr><td><b>Subject</b></td><td><b>Count</b></td></tr>{subject_rows}</table>"
    )

def build_feedback_item_text(fb: dict) -> str:
    from src.config import FEEDBACK_CATEGORIES, FEEDBACK_STATUS_LABELS
    name = format_public_name(fb)
    cat_label = FEEDBACK_CATEGORIES.get(fb['category'], fb['category'])
    status_label = FEEDBACK_STATUS_LABELS.get(fb['status'], fb['status'])
    reply_block = ""
    if fb.get('admin_reply'):
        reply_block = f"\n\n<b>💬 Your reply:</b>\n<blockquote>{html.escape(fb['admin_reply'])}</blockquote>"
    return (
        f"<b>#{fb['id']} • {cat_label}</b>\n"
        f"👤 {name} (<code>{fb['user_id']}</code>)\n"
        f"📌 Status: <b>{status_label}</b>\n\n"
        f"<blockquote>{html.escape(fb['message'])}</blockquote>"
        f"{reply_block}"
    )

def build_user_directory_text(users: list) -> str:
    """Rich-text paginated user list for the admin dashboard."""
    if not users:
        return "<h2>👥 USER DIRECTORY</h2>\n<hr/>\n<i>No users found.</i>"

    rows = []
    for u in users:
        name = format_public_name(u)
        rows.append(
            f"<tr><td>{html.escape(name)}</td><td>Gr.{u.get('grade') or '-'}</td>"
            f"<td>{u.get('total_marks', 0)}</td><td>{html.escape(str(u.get('country', '-')))}</td></tr>"
        )

    return (
        "<h2>👥 USER DIRECTORY (Most Recently Active)</h2>\n<hr/>\n"
        "<table><tr><td><b>Name</b></td><td><b>Grade</b></td><td><b>Marks</b></td><td><b>Country</b></td></tr>"
        + "".join(rows) + "</table>"
    )


def build_tournament_leaderboard_text(rows: list, current_round: int = None, total_rounds: int = None) -> str:
    """Ranking scoped ONLY to the current tournament run — sums marks across every
    round in this series, not the user's all-time/weekly total."""
    round_tag = f" • Round {current_round}/{total_rounds}" if current_round and total_rounds else ""
    if not rows:
        return f"🏆 <b>TOURNAMENT-ONLY RANKING{round_tag}</b>\n<i>No scores yet this tournament.</i>"

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    lines = [
        f"🏆 <b>TOURNAMENT-ONLY RANKING{round_tag}</b>",
        "<i>(Points from this tournament series only — not your all-time score)</i>\n"
    ]
    for i, r in enumerate(rows[:10]):
        name = format_public_name(r)
        tag = f" (#{r['alliance_tag']})" if r.get('alliance_tag') else ""
        lines.append(f" {medals[i]} {name}{tag} — <b>{r['tournament_score']} pts</b> ({r['tournament_correct']} correct)")
    return "\n".join(lines)

_STATUS_PIPELINE = ["open", "in_progress", "planned", "resolved"]
_STATUS_PIPELINE_ICONS = {"open": "🆕", "in_progress": "🔧", "planned": "🗓️", "resolved": "✅"}


def _status_progress_line(status: str) -> str:
    """Linear step-tracker so a user can see exactly where their request sits."""
    from src.config import FEEDBACK_STATUS_LABELS

    if status == "wontfix":
        return "🚫 <b>Not Planned</b> — reviewed, but won't be implemented."

    steps = []
    reached = False
    for key in _STATUS_PIPELINE:
        icon = _STATUS_PIPELINE_ICONS[key]
        label = FEEDBACK_STATUS_LABELS.get(key, key).split(" ", 1)[-1]
        if key == status:
            steps.append(f"<b>[{icon} {label}]</b>")
            reached = True
        elif not reached:
            steps.append(f"{icon} {label}")
        else:
            steps.append(f"<i>{icon} {label}</i>")
    return " → ".join(steps)


def build_user_feedback_list_text(items: list, total_count: int) -> str:
    if not items:
        return (
            "<b>📋 MY FEEDBACK &amp; REQUESTS</b>\n<hr/>\n"
            "<i>Nothing submitted yet. Tap ✍️ SUBMIT NEW FEEDBACK to report a bug, "
            "request a feature, or share your thoughts.</i>"
        )

    from src.config import FEEDBACK_CATEGORIES
    lines = [f"<b>📋 MY FEEDBACK &amp; REQUESTS</b>  <i>({total_count} total)</i>", "<hr/>"]
    for fb in items:
        icon = FEEDBACK_CATEGORIES.get(fb['category'], "💬").split(" ", 1)[0]
        status_icon = _STATUS_PIPELINE_ICONS.get(fb['status'], "🚫")
        reply_flag = " 💬" if fb.get('admin_reply') else ""
        snippet = html.escape(fb['message'][:60] + ("…" if len(fb['message']) > 60 else ""))
        lines.append(f"{icon} <b>#{fb['id']}</b>  {status_icon}{reply_flag}\n<i>{snippet}</i>")
    lines.append("<hr/>\n<i>Tap a card below to view full details.</i>")
    return "\n\n".join(lines)


def build_feedback_menu_text() -> str:
    """The unified /feedback entry card — category buttons plus the tracker option live here."""
    return (
        "<h2>💬 FEEDBACK &amp; FEATURE REQUESTS</h2>\n<hr/>\n"
        "What's this about? Pick a category to submit something new, or check the "
        "status of what you've already sent."
    )




def build_feedback_browse_list_text(items: list, category: str, status: str, offset: int, total: int) -> str:
    from src.config import FEEDBACK_CATEGORIES, FEEDBACK_STATUS_LABELS
    cat_label = "All Categories" if category == "all" else FEEDBACK_CATEGORIES.get(category, category)
    status_label = "All Statuses" if status == "all" else FEEDBACK_STATUS_LABELS.get(status, status.capitalize())

    if not items:
        return (
            f"<h2>💬 FEEDBACK QUEUE</h2>\n"
            f"<i>{cat_label} • {status_label}</i>\n<hr/>\n"
            f"<i>Nothing here right now.</i>"
        )

    lines = [f"<h2>💬 FEEDBACK QUEUE</h2>\n<i>{cat_label} • {status_label} • {offset+1}-{offset+len(items)} of {total}</i>\n<hr/>\n"]
    for fb in items:
        name = format_public_name(fb)
        cat_short = FEEDBACK_CATEGORIES.get(fb['category'], fb['category']).split(" ", 1)[0]
        status_icon = _STATUS_PIPELINE_ICONS.get(fb['status'], "🚫")
        snippet = html.escape(fb['message'][:70] + ("…" if len(fb['message']) > 70 else ""))
        lines.append(f"{status_icon} <b>#{fb['id']}</b> {cat_short} — {snippet}\n   <i>by {name}</i>")

    return "\n\n".join(lines)