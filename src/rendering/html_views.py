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

    show_real = row.get('show_real_identity', False)
    if consent and show_real:
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

    bot_username = CONFIG.get("bot_username")
    dm_link = f" · <a href='https://t.me/{bot_username}?start=view_{display_id}'>💬 My Answer</a>" if bot_username else ""
    footer = (
        f"\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>REF <code>{display_id}</code></b>{round_tag} │ <a href='https://t.me/{channel_username}'>{channel_name}</a>{dm_link}\n"
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

def build_answered_view(q, display_id: str, user_idx: int, show_derivation=False, show_perf=False, mode="compact", compact=None, perf_card=None, continuation=False, include_diagram=True) -> str:
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
    if include_diagram and has_real_diagram(q):
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
        buttons.append([InlineKeyboardButton("↩️ HIDE SOLUTION", callback_data=f"{prefix}|{d_id}|{user_selection}|0|{1 if show_perf else 0}")])
    else:
        buttons.append([InlineKeyboardButton("📖 SHOW SOLUTION", callback_data=f"{prefix}|{d_id}|{user_selection}|1|{1 if show_perf else 0}")])

    if show_perf:
        buttons.append([InlineKeyboardButton("↩️ HIDE STATS", callback_data=f"{prefix}|{d_id}|{user_selection}|{1 if show_derivation else 0}|0")])
    else:
        buttons.append([InlineKeyboardButton("📊 SHOW STATS", callback_data=f"{prefix}|{d_id}|{user_selection}|{1 if show_derivation else 0}|1")])


    channel_username = CONFIG.get("channel", "QuizOva").lstrip('@')
    if message_id:
        return_url = f"https://t.me/{channel_username}/{message_id}"
        return_label = "🔙 TO QUESTION"
    else:
        return_url = f"https://t.me/{channel_username}"
        return_label = "📣 TO CHANNEL"

    buttons.append([
        InlineKeyboardButton("👤 PROFILE", callback_data="profile_popup|0"),
        InlineKeyboardButton(return_label, url=return_url)
    ])
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
    Rich-text (table/blockquote) pre-tournament announcement — rendered via the app's
    sendRichMessage pipeline for a native Telegram table instead of a hand-drawn ├─ tree,
    with a graceful legacy-HTML fallback baked into send/edit_rich_message_safe.
    """
    subject = meta.get("subject", "GENERAL").upper()
    topics = meta.get("topics", [])
    diff_summary = meta.get("difficulty_summary", "N/A")
    total_count = meta.get("total_count", 0)
    round_seconds = meta.get("round_seconds", 60)
    cooldown_seconds = meta.get("cooldown_seconds", 15)

    time_utc = meta.get("target_time_utc", "N/A")
    time_eat = meta.get("target_time_eat", "N/A")

    shown_topics = topics[:4]
    topics_str = ", ".join(html.escape(str(t)) for t in shown_topics) or "General mix"
    if len(topics) > 4:
        topics_str += f" +{len(topics) - 4} more"

    if remaining_delay is not None and remaining_delay > 0:
        mins, secs = divmod(remaining_delay, 60)
        status_line = f"⏳ <b>Starts in {mins}m {secs:02d}s</b>"
    else:
        status_line = "⚔️ <b>Starting now...</b>"

    return (
        f"<h2>⚔️ LIVE TOURNAMENT SHOWDOWN</h2>\n"
        f"Scholars, get ready — everyone answers the same question on the same clock. Fastest correct answers climb the round podium.\n\n"
        f"<table>"
        f"<tr><td>📚 Subject</td><td><b>{html.escape(subject)}</b></td></tr>"
        f"<tr><td>🏷️ Topics</td><td>{topics_str}</td></tr>"
        f"<tr><td>📈 Difficulty</td><td>{html.escape(str(diff_summary))}</td></tr>"
        f"<tr><td>🔢 Questions</td><td>{total_count}</td></tr>"
        f"<tr><td>⏱️ Round timer</td><td>{round_seconds}s</td></tr>"
        f"<tr><td>❄️ Cooldown</td><td>{cooldown_seconds}s</td></tr>"
        f"</table>\n"
        f"<blockquote>"
        f"🌍 {html.escape(str(time_utc))}\n"
        f"🇪🇹 {html.escape(str(time_eat))}\n"
        f"{status_line}"
        f"</blockquote>\n"
        f"💡 <i>Turn notifications on — speed and accuracy both earn bonus marks.</i>"
    )


def build_profile_card_text(profile: dict, roster: list = None, subject_marks: list = None, top_topic: dict = None, rank_summary: dict = None) -> str:
    name = format_public_name(profile)
    has_school = bool(profile.get("org_id"))
    grade_line = f"Grade {profile.get('grade')}" if (has_school and profile.get("grade")) else "Not a Student"
    marks = profile.get("total_marks", 0)
    streak = profile.get("current_streak", 0)
    total = profile.get("total", 0)
    correct = profile.get("correct", 0)
    accuracy = int((correct / total) * 100) if total > 0 else 0
    avg_mark = round(marks / total, 1) if total > 0 else 0
    mastery = get_grade_mastery_title(marks)
    next_rank = get_next_rank_info(marks)
    visibility = "🟢 Public" if profile.get("public_consent_granted") else "🕵️ Private"
    city = profile.get("personal_city") or "Not set"
    country = profile.get("personal_country") or "Not set"

    org_tag = profile.get("org_tag")
    org_name = profile.get("org_name")
    school_line = (
        f"🏫 <b>{html.escape(org_name)}</b> <code>#{org_tag}</code>"
        if org_tag else
        "🏫 <i>No school set — 📍 Locations &amp; School to add one</i>"
    )
    team_tag = profile.get("team_tag")
    team_name = profile.get("team_name")
    team_line = (
        f"🏰 <b>{html.escape(team_name)}</b> <code>#{team_tag}</code>"
        if team_tag else
        "🏰 <i>No team yet — tap 🏰 STUDY ALLIANCE to join or create one</i>"
    )

    subj_line = ""
    if subject_marks:
        top = sorted(subject_marks, key=lambda s: s['marks'], reverse=True)[:2]
        names = [html.escape(str(s['subject']).title()) for s in top]
        subj_line = f"\n📚 <b>Top Subjects:</b> {' · '.join(names)}"

    topic_line = f"\n🔎 <b>Most Answered Topic:</b> {html.escape(top_topic['topic'])} ({top_topic['cnt']}×)" if top_topic else ""

    rank_block = ""
    if rank_summary:
        def _r(v): return f"#{v}" if v else "—"
        rank_block = (
            f"\n<hr/>\n<h3>🏆 Your Rank</h3>\n<table>"
            f"<tr><td>🏰 Team</td><td>{_r(rank_summary.get('team_rank'))}</td></tr>"
            f"<tr><td>🏫 School</td><td>{_r(rank_summary.get('school_rank'))}</td></tr>"
            f"<tr><td>🌆 City</td><td>{_r(rank_summary.get('city_rank'))}</td></tr>"
            f"<tr><td>🌍 Country</td><td>{_r(rank_summary.get('country_rank'))}</td></tr>"
            f"<tr><td>🌐 World</td><td>{_r(rank_summary.get('world_rank'))}</td></tr>"
            f"</table>"
        )

    return (
        f"👤 <b>{name}</b>  ·  {grade_line}\n"
        f"{mastery}\n"
        f"<hr/>\n"
        f"<b>{marks} Marks</b>  ·  📈 Avg {avg_mark}/q  ·  🔥 {streak}d streak  ·  🎯 {accuracy}%\n"
        f"<i>{next_rank}</i>{subj_line}{topic_line}\n"
        f"<hr/>\n"
        f"{visibility}  ·  📍 {city}, {country}\n"
        f"{school_line}\n"
        f"{team_line}"
        f"{rank_block}"
    )


def build_profile_main_keyboard(has_team: bool) -> InlineKeyboardMarkup:
    team_label = "🏫 MY TEAM" if has_team else "🏰 JOIN / CREATE TEAM"
    channel_username = CONFIG.get("channel", "QuizOva").lstrip('@')
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎛️ SETTINGS", callback_data="settings_menu|0"),
         InlineKeyboardButton(team_label, callback_data="alliance_portal|0")],
        [InlineKeyboardButton("🏆 LEADERBOARD", callback_data="menu_leaderboard|0"),
         InlineKeyboardButton("🤝 INVITE & EARN", callback_data="menu_invite|0")],
        [InlineKeyboardButton("📚 MY ANSWERS", callback_data="my_answers_menu|0")],
        [InlineKeyboardButton("💬 FEEDBACK", callback_data="fb_menu|0"),
         InlineKeyboardButton("📖 HOW IT WORKS", callback_data="full_docs|0")],
        [InlineKeyboardButton("📢 VISIT CHANNEL", url=f"https://t.me/{channel_username}")],
        [InlineKeyboardButton("🔙 CLOSE", callback_data="close_portal|0")]
    ])

def build_profile_settings_keyboard(public_consent_granted: bool) -> InlineKeyboardMarkup:
    consent_label = "🔴 GO PRIVATE" if public_consent_granted else "🟢 GO PUBLIC"
    consent_target = "0" if public_consent_granted else "1"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(consent_label, callback_data=f"toggle_consent|{consent_target}")],
        [InlineKeyboardButton("✍️ NICKNAME", callback_data="set_nick_fsm|0"),
         InlineKeyboardButton("🎒 GRADE", callback_data="reselect_grade_panel|0")],
        [InlineKeyboardButton("📍 LOCATIONS & SCHOOL", callback_data="loc_status_menu|0")],
        [InlineKeyboardButton("🔙 PROFILE", callback_data="privacy_menu|0")]
    ])
    
def build_location_status_text(profile: dict) -> str:
    city = profile.get("personal_city")
    country = profile.get("personal_country")
    city_status = profile.get("personal_city_status") or "approved"
    org_name = profile.get("org_name")
    org_tag = profile.get("org_tag")

    warning = ""
    if not city or not country:
        warning = (
            "<blockquote>⚠️ <b>Required:</b> a city and country on file (pending review is fine) "
            "unlocks answering questions.</blockquote>\n"
        )

    country_line = f"🌍 <b>Country:</b> {html.escape(country)}" if country else "🌍 <b>Country:</b> <i>Not set</i>"

    if city:
        note = " ⏳ <i>(pending approval)</i>" if city_status == "pending" else ""
        city_line = f"🏙️ <b>City:</b> {html.escape(city)}{note}"
    else:
        city_line = "🏙️ <b>City:</b> <i>Not set</i>"

    school_line = (
        f"🏫 <b>School:</b> {html.escape(org_name)} <code>#{org_tag}</code>"
        if org_name else
        "🏫 <b>School:</b> <i>Not a student on any team yet</i>"
    )

    return (
        "<h2>📍 LOCATIONS &amp; SCHOOL</h2>\n<hr/>\n"
        f"{warning}{country_line}\n{city_line}\n{school_line}\n<hr/>\n"
        "<i>Tap below to set or change any of these.</i>"
    )

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


def build_organization_card_text(org: dict, roster: list, sort_field: str = "score", sort_dir: str = "desc", branches: list = None) -> str:
    name = html.escape(org.get("org_name", "UNKNOWN"))
    tag = org.get("org_tag", "UNKNOWN")
    org_type = html.escape(str(org.get("org_type", "School")))
    privacy = "🌐 Public — needs admin approval" if org.get("is_public", True) else "🔒 Private — instant join by code"
    city = html.escape(str(org.get("city") or "—"))
    country = html.escape(str(org.get("country") or "—"))

    total_score = sum(r.get("total_marks", 0) for r in roster)
    avg_score = int(total_score / len(roster)) if roster else 0

    rows = list(roster)
    if sort_field == "name":
        rows.sort(key=lambda r: format_public_name(r).lower(), reverse=(sort_dir == "desc"))
    elif sort_field == "date":
        rows.sort(key=lambda r: r.get("joined_at") or "", reverse=(sort_dir == "desc"))
    else:
        rows.sort(key=lambda r: r.get("total_marks", 0), reverse=(sort_dir == "desc"))

    role_icon = {"creator": "👑", "admin": "🛡️"}
    table_rows = ["<tr><td><b>#</b></td><td><b>Scholar</b></td><td><b>Marks</b></td></tr>"]
    for i, r in enumerate(rows[:15]):
        icon = role_icon.get(r.get("org_role"), "")
        nm = html.escape(f"{icon} {format_public_name(r)}".strip())
        table_rows.append(f"<tr><td>{i+1}</td><td>{nm}</td><td>{r.get('total_marks', 0)}</td></tr>")
    roster_table = "<table>" + "".join(table_rows) + "</table>" if rows else "<i>No active scholars registered yet.</i>"

    branch_block = ""
    if branches:
        b_rows = ["<tr><td><b>Branch</b></td><td><b>City</b></td><td><b>Members</b></td><td><b>Score</b></td></tr>"]
        for b in branches:
            b_rows.append(
                f"<tr><td>{html.escape(b['branch_name'])}</td>"
                f"<td>{html.escape(b.get('city') or '—')}</td>"
                f"<td>{b.get('member_count', 0)}</td>"
                f"<td>{b.get('branch_score', 0)}</td></tr>"
            )
        branch_block = f"\n<h3>🏢 Branches</h3>\n<table>{''.join(b_rows)}</table>"

    scope = org.get("team_scope")
    scope_value = org.get("scope_value")
    scope_line = ""
    if scope and scope != "open":
        scope_line = f"🔒 Dedicated to: <b>{html.escape(str(scope_value or ''))}</b>\n"
    desc_line = f"<i>{html.escape(org['description'])}</i>\n" if org.get("description") else ""

    return (
        f"<h2>🏫 {name}</h2>\n"
        f"<code>#{tag}</code> · {org_type} · {privacy}\n"
        f"📍 {city}, {country}\n"
        f"{scope_line}{desc_line}"
        f"<hr/>\n"
        f"<b>{len(roster)}</b> members · <b>{total_score}</b> total marks · <b>{avg_score}</b> average\n"
        f"<hr/>\n"
        f"<h3>🏆 Roster</h3>\n"
        f"{roster_table}"
        f"{branch_block}"
    )


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


def build_champions_podium_html(meta: dict, ind_val: str, ind_score, sch_val: str, city_val: str, cnt_val: str) -> str:
    """
    Compact tournament-series wrap-up card. One header line (date · rounds · duration),
    one table, one line naming the ranking criterion — reads cleanly on both mobile and
    desktop without scrolling, instead of a long paragraph explaining the mechanics.
    """
    from datetime import datetime, timezone

    subject = meta.get("subject", "General")
    total_rounds = meta.get("total_count", 1)
    round_seconds = meta.get("round_seconds", 60)
    cooldown_seconds = meta.get("cooldown_seconds", 15)

    total_seconds = (total_rounds * round_seconds) + max(0, total_rounds - 1) * cooldown_seconds
    mins = total_seconds // 60
    duration_str = f"{mins}m" if mins else f"{total_seconds}s"
    date_str = datetime.now(timezone.utc).strftime("%b %d, %Y")
    score_str = f" · {ind_score} pts" if ind_score is not None else ""

    return (
        f"🏆 <b>TOURNAMENT COMPLETE — {html.escape(str(subject))}</b>\n"
        f"📅 {date_str} · 🔢 {total_rounds} rounds · ⏱ {duration_str}\n"
        f"<hr/>\n"
        f"<table>"
        f"<tr><td>🥇 Individual</td><td><b>{html.escape(ind_val)}</b>{score_str}</td></tr>"
        f"<tr><td>🏫 School</td><td>{html.escape(sch_val)}</td></tr>"
        f"<tr><td>🌆 City</td><td>{html.escape(city_val)}</td></tr>"
        f"<tr><td>🌍 Country</td><td>{html.escape(cnt_val)}</td></tr>"
        f"</table>\n"
        f"<i>Ranked by total tournament points — correct answers × speed bonus.</i>"
    )
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
    "repeats": ("🔁 Repeat Questions", (
        "<b>🔁 REPEAT QUESTIONS</b>\n"
        "If you see a 🔁 badge on a question, it's been posted before. It still "
        "counts fully — great for a second shot at one you missed."
    )),
    "teams_roles": ("👑 Team Roles & Leaving", (
        "<b>👑 TEAM ROLES</b>\n"
        "👑 Creator — approves requests, can dissolve\n"
        "🛡️ Admin — promoted by creator, can approve requests\n"
        "👤 Member — scores for the team\n\n"
        "If a creator leaves, leadership passes automatically to the "
        "longest-standing admin (or member)."
    )),
    "leaderboard_filters": ("📊 Leaderboard Filters", (
        "<b>📊 LEADERBOARD VIEWS</b>\n"
        "/leaderboard lets you switch between 🎒 Grade, 🏫 School, 🌆 City, "
        "and 🌍 Country views — tap the filter buttons at the bottom."
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
    rows.append([
        InlineKeyboardButton("👤 BACK TO PROFILE", callback_data="privacy_menu|0"),
        InlineKeyboardButton("🔙 CLOSE", callback_data="close_portal|0")
    ])
    return InlineKeyboardMarkup(rows)

def build_help_topic_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 BACK TO HELP", callback_data="help_menu|0")],
        [InlineKeyboardButton("👤 BACK TO PROFILE", callback_data="privacy_menu|0")]
    ])

# def build_leaderboard_text(scope: str, rows: list, profile: dict = None, label_override: str = None) -> str:
    #     scope_labels = {
    #         "grade": f"🎒 Grade {label_override or (profile.get('grade') if profile else '—')}",
    #         "country": f"🌍 {label_override}" if label_override else "🌍 Countries (Top)",
    #         "city": f"🌆 {label_override}" if label_override else "🌆 Cities (Top)",
    #         "school": "🏫 School Teams",
    #         "country_overall": "🌍 TOP COUNTRIES (Global)",
    #         "city_overall": "🌆 TOP CITIES (Global)",
    #     }
    #     lines = [f"<h2>🏆 {scope_labels.get(scope, 'Leaderboard')}</h2>"]

    #     if profile and scope == "grade" and not label_override:
    #         mastery = get_grade_mastery_title(profile.get("total_marks", 0))
    #         acc = int((profile['correct']/profile['total'])*100) if profile.get('total') else 0
    #         lines.append(
    #             f"<b>You:</b> {format_public_name(profile)} · {mastery} · "
    #             f"{profile.get('total_marks', 0)} marks · 🔥{profile.get('current_streak', 0)}d · 🎯{acc}%"
    #         )
    #         lines.append("<hr/>")

    #     if not rows:
    #         lines.append("<i>No scores yet — be the first!</i>")
    #         return "\n".join(lines)

    #     medals = ["🥇", "🥈", "🥉"]
    #     label_col = "Country" if scope == "country_overall" else "City" if scope == "city_overall" else "Scholar"
    #     table_rows = [f"<tr><td><b>#</b></td><td><b>{label_col}</b></td><td><b>Marks</b></td></tr>"]
    #     for i, r in enumerate(rows[:10]):
    #         rank = medals[i] if i < 3 else str(i + 1)
    #         if scope == "school":
    #             nm, score = f"#{r['alliance_tag']}", r['total_score']
    #         elif scope == "country_overall":
    #             nm, score = str(r.get('country') or '—'), r.get('total_score', 0)
    #         elif scope == "city_overall":
    #             nm, score = str(r.get('city') or '—'), r.get('total_score', 0)
    #         elif label_override and scope in ("city", "country"):
    #             nm, score = format_public_name(r), r.get('total_marks', 0)
    #         else:
    #             nm, score = format_public_name(r), r.get('total_score', r.get('total_marks', 0))
    #         table_rows.append(f"<tr><td>{rank}</td><td>{html.escape(nm)}</td><td>{score}</td></tr>")
    #     lines.append("<table>" + "".join(table_rows) + "</table>")
    #     return "\n".join(lines)

# def build_leaderboard_keyboard(scope: str, active_grade: int = None) -> InlineKeyboardMarkup:
    #     def _b(key, label):
    #         return InlineKeyboardButton(f"{'• ' if scope == key else ''}{label}", callback_data=f"lb_filter|{key}")
    #     rows = [
    #         [_b("grade", "🎒 GRADE"), _b("school", "🏫 SCHOOL")],
    #         [_b("school_branch", "🏢 BRANCHES"), _b("city", "🌆 CITY")],   # NEW: branch view
    #         [_b("country", "🌍 COUNTRY"), _b("city_overall", "🌆 TOP CITIES")],
    #         [_b("country_overall", "🌍 TOP COUNTRIES")],
    #         [InlineKeyboardButton("🌍 EXPLORE RANKINGS", callback_data="wr|world|all")],
    #         [InlineKeyboardButton("🗺️ EXPLORE COUNTRIES", callback_data="geo_country_list|0"),
    #          InlineKeyboardButton("🎒 GRADE RANKS", callback_data="geo_grade_list|0")],
    #     ]
    #     if scope == "grade":
    #         grade_row = [
    #             InlineKeyboardButton(f"{'• ' if active_grade == g else ''}{g}", callback_data=f"lb_grade|{g}")
    #             for g in (6, 8, 10, 12)
    #         ]
    #         rows.append(grade_row)
    #     rows.append([InlineKeyboardButton("👤 PROFILE", callback_data="privacy_menu|0"),
    #                  InlineKeyboardButton("🔙 CLOSE", callback_data="close_portal|0")])
    #     return InlineKeyboardMarkup(rows)

# def build_geo_picker_keyboard(items: list, scope: str) -> InlineKeyboardMarkup:
    #     """Buttons to pick a specific city/country to see its leaderboard, 2 per row."""
    #     rows, row = [], []
    #     for name in items[:20]:
    #         row.append(InlineKeyboardButton(name, callback_data=f"lb_{scope}_pick|{name}"))
    #         if len(row) == 2:
    #             rows.append(row); row = []
    #     if row:
    #         rows.append(row)
    #     rows.append([InlineKeyboardButton("🔙 LEADERBOARD", callback_data="menu_leaderboard|0")])
    #     return InlineKeyboardMarkup(rows)

def build_help_topic_text(key: str) -> str:
    topic = HELP_TOPICS.get(key)
    return topic[1] if topic else "⚠️ Topic not found."

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

def build_feedback_kanban_text(stats: dict, recent_by_status: dict) -> str:
    """Kanban-style board: one column per status, each showing its count and its most
    recently-updated items with a REF and a relative date, so admins can see where
    everything sits before drilling into a specific queue."""
    from src.config import FEEDBACK_STATUS_LABELS
    lines = ["<h2>🗂️ FEEDBACK KANBAN BOARD</h2>", f"<i>{stats.get('total', 0)} total submissions</i>", "<hr/>"]
    for status_key in ["open", "in_progress", "planned", "resolved", "wontfix"]:
        label = FEEDBACK_STATUS_LABELS.get(status_key, status_key)
        count = stats.get("by_status", {}).get(status_key, 0)
        lines.append(f"<h3>{label} ({count})</h3>")
        items = recent_by_status.get(status_key, [])
        if not items:
            lines.append("<i>Empty</i>")
        else:
            for fb in items:
                when = fb['updated_at'].strftime('%b %d') if fb.get('updated_at') else "—"
                snippet = html.escape(fb['message'][:45] + ("…" if len(fb['message']) > 45 else ""))
                lines.append(f" • <code>#{fb['id']}</code> {snippet} <i>({when})</i>")
    return "\n".join(lines)


def build_feedback_kanban_keyboard() -> InlineKeyboardMarkup:
    from src.config import FEEDBACK_STATUS_LABELS
    rows = []
    for status_key in ["open", "in_progress", "planned", "resolved", "wontfix"]:
        rows.append([InlineKeyboardButton(f"📂 {FEEDBACK_STATUS_LABELS.get(status_key, status_key)}", callback_data=f"fb_browse|all|{status_key}:0")])
    rows.append([InlineKeyboardButton("🔙 DASHBOARD", callback_data="admin_dashboard|0")])
    return InlineKeyboardMarkup(rows)


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

def build_location_suggestions_browse_list_text(items: list, kind: str, status: str, offset: int, total: int) -> str:
    kind_label = {"all": "All", "city": "🏙 Cities", "school": "🏫 Schools"}.get(kind, "All")
    status_label = {"all": "All", "pending": "📥 Pending", "approved": "✅ Approved", "rejected": "🚫 Rejected"}.get(status, status.capitalize())

    if not items:
        return (
            f"<h2>📍 LOCATION &amp; SCHOOL REQUESTS</h2>\n"
            f"<i>{kind_label} • {status_label}</i>\n<hr/>\n"
            f"<i>Nothing here right now.</i>"
        )

    lines = [f"<h2>📍 LOCATION &amp; SCHOOL REQUESTS</h2>\n<i>{kind_label} • {status_label} • {offset+1}-{offset+len(items)} of {total}</i>\n<hr/>\n"]
    status_icon = {"pending": "📥", "approved": "✅", "rejected": "🚫"}
    for ls in items:
        icon = "🏙" if ls['kind'] == "city" else "🏫"
        s_icon = status_icon.get(ls['status'], "•")
        who = format_public_name(ls)
        repeat = f" · asked {ls['request_count']}×" if ls.get('request_count', 1) > 1 else ""
        lines.append(f"{s_icon} {icon} <b>#{ls['id']}</b> {html.escape(ls['name'])}, {html.escape(str(ls.get('country') or ''))}\n   <i>by {who}{repeat}</i>")
    return "\n\n".join(lines)


def build_location_suggestion_item_text(ls: dict, thread: list = None, viewer_tz: str = "UTC") -> str:
    from src.geo import format_local_time
    icon = "🏙 City" if ls['kind'] == "city" else "🏫 School"
    status_labels = {"pending": "📥 Pending", "approved": "✅ Approved", "rejected": "🚫 Rejected"}
    who = format_public_name(ls)
    repeat = f"\n📈 Requested <b>{ls['request_count']}×</b>" if ls.get('request_count', 1) > 1 else ""
    thread_block = ""
    if thread:
        bubbles = []
        for m in thread:
            role_label = "🛠️ Admin" if m['sender_role'] == "admin" else "🧑 Student"
            ts = format_local_time(m.get('created_at'), viewer_tz) if m.get('created_at') else ""
            ts_line = f" <i>({ts})</i>" if ts else ""
            bubbles.append(f"<b>{role_label}:</b>{ts_line}\n{html.escape(m['message'])}")
        thread_block = "\n\n<blockquote expandable>" + "\n\n".join(bubbles) + "</blockquote>"
    return (
        f"<b>#{ls['id']} • {icon}</b>\n"
        f"📌 Status: <b>{status_labels.get(ls['status'], ls['status'])}</b>{repeat}\n"
        f"👤 {who} (<code>{ls['submitted_by']}</code>)\n\n"
        f"<blockquote><b>{html.escape(ls['name'])}</b>, {html.escape(str(ls.get('country') or ''))}</blockquote>"
        f"{thread_block}"
    )

def build_feedback_thread_text(fb: dict, thread: list, viewer_tz: str = "UTC") -> str:
    from src.config import FEEDBACK_CATEGORIES
    from src.geo import format_local_time
    cat_label = FEEDBACK_CATEGORIES.get(fb['category'], fb['category'])

    all_msgs = [{"role": "user", "text": fb['message'], "created_at": fb.get('created_at')}] + [
        {"role": m['sender_role'], "text": m['message'], "created_at": m.get('created_at')} for m in thread
    ]

    def _fmt_time(dt):
        return format_local_time(dt, viewer_tz) if dt else ""

    def bubble(role, text, created_at):
        safe = html.escape(text)
        ts_line = f"\n<i>{_fmt_time(created_at)}</i>" if created_at else ""
        if role == "admin":
            return f"➡️ <b>🛠️ Support</b>{ts_line}\n<blockquote>{safe}</blockquote>"
        return f"⬅️ <b>🧑 You</b>{ts_line}\n<blockquote>{safe}</blockquote>"

    if len(all_msgs) > 4:
        older, recent = all_msgs[:-3], all_msgs[-3:]
        older_block = "\n\n".join(bubble(m["role"], m["text"], m["created_at"]) for m in older)
        conversation = (
            f"<blockquote expandable><b>📜 Earlier in this conversation ({len(older)})</b>\n\n{older_block}</blockquote>\n\n"
            + "\n\n".join(bubble(m["role"], m["text"], m["created_at"]) for m in recent)
        )
    else:
        conversation = "\n\n".join(bubble(m["role"], m["text"], m["created_at"]) for m in all_msgs)

    return (
        f"<b>#{fb['id']} • {cat_label}</b>\n"
        f"{_status_progress_line(fb['status'])}\n"
        f"<hr/>\n\n"
        f"{conversation}"
    )


def build_welcome_message_text() -> str:
    """The channel's pinned welcome/intro card — marketing tone, not technical.
    Posted and pinned on demand via the admin CLI (option [W])."""
    return (
        "🎓 <b>Welcome — you just found something good.</b>\n"
        "<hr/>\n\n"
        "Hey! We're just getting started here, and honestly? We're glad you're one of the first "
        "ones in the room.\n\n"
        "This channel drops <b>bite-sized challenges</b> — math, science, language, logic, general "
        "knowledge, a bit of everything — right into your feed, day and night. No sign-up forms, "
        "no boring lectures. You just tap an answer and instantly see if you nailed it, with a full "
        "explanation waiting for you.\n\n"
        "<blockquote>"
        "🔥 Answer daily, build a streak, watch your rank climb\n"
        "⚔️ Jump into live tournaments where everyone competes at the same time\n"
        "🏫 Team up with friends or your school and climb the boards together\n"
        "🤝 Invite people you know — when they learn, you earn too\n"
        "🔐 Your name stays private unless you choose to show it off"
        "</blockquote>\n\n"
        "We built this because learning shouldn't feel like a chore, and a good challenge is way "
        "more fun with other people around. So here's the honest ask:\n\n"
        "👉 <b>Stick around. Answer one today. And if you like it — send it to someone.</b>\n"
        "That's genuinely how a channel like this grows — one person telling another it's worth "
        "their time.\n\n"
        "<i>Glad you're here. Let's get started. 🚀</i>"
    )

def build_org_history_text(org: dict, log_rows: list, viewer_tz: str = "UTC") -> str:
    from src.geo import format_local_time
    name = html.escape(org.get("org_name", "TEAM"))
    tag = org.get("org_tag", "")
    role_icon = {"creator": "👑", "admin": "🛡️", "member": "👤", "pending": "📥", "rejected": "🚫"}

    active = [r for r in log_rows if r['org_role'] not in ("pending", "rejected")]
    pending = [r for r in log_rows if r['org_role'] == "pending"]

    def _table(rows):
        body = ["<tr><td><b>Scholar</b></td><td><b>Role</b></td><td><b>Since</b></td></tr>"]
        for r in rows:
            icon = role_icon.get(r['org_role'], "•")
            when = format_local_time(r['joined_at'], viewer_tz, fmt="%b %d, %Y") if r.get('joined_at') else "—"
            body.append(f"<tr><td>{html.escape(format_public_name(r))}</td><td>{icon} {r['org_role'].title()}</td><td>{when}</td></tr>")
        return "<table>" + "".join(body) + "</table>"

    pending_block = f"<h3>📥 Pending Requests ({len(pending)})</h3>\n{_table(pending)}\n<hr/>\n" if pending else ""
    active_block = _table(active) if active else "<i>No members yet.</i>"

    return (
        f"<h2>👥 {name} — Members &amp; Requests</h2>\n"
        f"<code>#{tag}</code>\n<hr/>\n"
        f"{pending_block}"
        f"<h3>👤 Roster</h3>\n{active_block}\n<hr/>\n"
        f"<i>Only visible to team admins. 👑 Creator · 🛡️ Admin · 👤 Member</i>"
    )

def build_my_answers_subject_menu_text(summary: list) -> str:
    if not summary:
        return "📚 <b>MY ANSWERS</b>\n<hr/>\n<i>No questions in the bank yet.</i>"
    lines = ["📚 <b>MY ANSWERS — Pick a Subject</b>", "<hr/>"]
    for s in summary:
        subj = html.escape(str(s['subject']).title())
        lines.append(f" • <b>{subj}</b> — {s['answered_count']}/{s['total_count']} answered")
    return "\n".join(lines)


def build_my_answers_list_text(rows: list, subject: str, filter_mode: str, offset: int, total: int, sort_field: str = "topic", sort_dir: str = "asc") -> str:
    filt_label = {"all": "All", "answered": "✅ Answered", "unanswered": "⬜ Unanswered"}.get(filter_mode, "All")
    sort_label = {"topic": "Topic", "date": "Date Answered", "tags": "Tags", "difficulty": "Difficulty"}.get(sort_field, "Topic")
    arrow = "↓" if sort_dir == "desc" else "↑"
    header = (
        f"<h3>📚 {html.escape(subject.title())} — {filt_label}</h3>\n"
        f"<i>{offset+1}-{offset+len(rows)} of {total} · sorted by {sort_label} {arrow}</i>\n<hr/>\n"
    )
    if not rows:
        return header + "<i>Nothing here.</i>"

    table_rows = ["<tr><td><b>Ref</b></td><td><b>Question</b></td><td><b>Status</b></td><td><b>Date</b></td></tr>"]
    for r in rows:
        clean_q = lite_math(r.get('question') or '')
        q_preview = html.escape(clean_q[:40] + ("…" if len(clean_q) > 40 else ""))

        if r.get('is_correct') is not None:
            status = "🟩 Correct" if r['is_correct'] else "🟥 Wrong"
        elif r.get('message_id'):
            status = "⚫ Removed" if r.get('track_status') == 'deleted' else "⬜ Unanswered"
        else:
            status = "📅 Not posted"

        ref = f"REF {r['display_id']}" if r.get('display_id') else "—"
        date_str = r['answered_at'].strftime('%b %d, %Y') if r.get('answered_at') else "—"
        table_rows.append(f"<tr><td>{ref}</td><td>{q_preview}</td><td>{status}</td><td>{date_str}</td></tr>")

    return header + "<table>" + "".join(table_rows) + "</table>"


def build_my_answers_keyboard(rows: list, subject: str, filter_mode: str, offset: int, total: int, sort_field: str = "topic", sort_dir: str = "asc") -> InlineKeyboardMarkup:
    code = {"topic": "t", "date": "d", "tags": "g", "difficulty": "l"}
    dcode = {"asc": "a", "desc": "d"}
    sort_code, dir_code = code.get(sort_field, "t"), dcode.get(sort_dir, "a")

    kb = []
    for r in rows:
        if r.get('is_correct') is not None:
            icon = "🟩" if r['is_correct'] else "🟥"
        elif r.get('message_id'):
            icon = "⚫" if r.get('track_status') == 'deleted' else "⬜"
        else:
            icon = "📅"
        ref_lbl = f"REF {r['display_id']}" if r.get('display_id') else "Unpublished"
        kb.append([InlineKeyboardButton(
            f"{icon} {ref_lbl} — {r['topic'][:24]}",
            callback_data=f"my_ans_open|{r['q_id']}|{subject}|{filter_mode}|{offset}|{sort_code}|{dir_code}"
        )])

    kb.append([
        InlineKeyboardButton(("• " if filter_mode == "all" else "") + "All", callback_data=f"my_ans_subj|{subject}|all|0|{sort_code}|{dir_code}"),
        InlineKeyboardButton(("• " if filter_mode == "answered" else "") + "✅", callback_data=f"my_ans_subj|{subject}|answered|0|{sort_code}|{dir_code}"),
        InlineKeyboardButton(("• " if filter_mode == "unanswered" else "") + "⬜", callback_data=f"my_ans_subj|{subject}|unanswered|0|{sort_code}|{dir_code}"),
    ])

    def _sort_btn(field, code_letter, label):
        if sort_field == field:
            next_dir = "desc" if sort_dir == "asc" else "asc"
            arrow = "↓" if sort_dir == "desc" else "↑"
            return InlineKeyboardButton(f"• {label} {arrow}", callback_data=f"my_ans_subj|{subject}|{filter_mode}|0|{code_letter}|{dcode[next_dir]}")
        return InlineKeyboardButton(label, callback_data=f"my_ans_subj|{subject}|{filter_mode}|0|{code_letter}|a")

    kb.append([
        _sort_btn("topic", "t", "🔤 Topic"),
        _sort_btn("date", "d", "📅 Date"),
        _sort_btn("tags", "g", "🏷️ Tags"),
        _sort_btn("difficulty", "l", "📈 Level"),
    ])

    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("⬅️ PREV", callback_data=f"my_ans_subj|{subject}|{filter_mode}|{max(0, offset-8)}|{sort_code}|{dir_code}"))
    if offset + 8 < total:
        nav.append(InlineKeyboardButton("NEXT ➡️", callback_data=f"my_ans_subj|{subject}|{filter_mode}|{offset+8}|{sort_code}|{dir_code}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("🔙 SUBJECTS", callback_data="my_answers_menu|0")])
    return InlineKeyboardMarkup(kb)


def build_my_answers_subject_keyboard(summary: list) -> InlineKeyboardMarkup:
    rows = []
    for s in summary:
        subj = str(s['subject'])
        label = f"{subj.title()} ({s['answered_count']}/{s['total_count']})"
        rows.append([InlineKeyboardButton(label, callback_data=f"my_ans_subj|{subj}|all|0|t|a")])
    rows.append([InlineKeyboardButton("👤 BACK TO PROFILE", callback_data="privacy_menu|0")])
    return InlineKeyboardMarkup(rows)

def build_admin_questions_text(rows: list, subject: str, status_filter: str, offset: int, total: int, channel_username: str) -> str:
    subj_label = subject.title() if subject and subject != "all" else "All Subjects"
    stat_label = {"all": "All", "posted": "🟢 Posted", "unposted": "⚪ Unposted", "deleted": "⚫ Deleted"}.get(status_filter, "All")
    lines = [f"📚 <b>QUESTION BANK — {html.escape(subj_label)}</b>", f"<i>{stat_label} • {offset+1}-{offset+len(rows)} of {total}</i>", "<hr/>"]
    if not rows:
        lines.append("<i>No questions match this filter.</i>")
        return "\n".join(lines)
    for r in rows:
        if r.get('message_id') is None:
            status = "⚪ Unposted" if not r.get('scheduled_for') else f"📅 Scheduled {r['scheduled_for']}"
            link = ""
        elif r.get('track_status') == 'deleted':
            status = "⚫ Deleted"
            link = f" · <a href='https://t.me/{channel_username}/{r['message_id']}'>🔗 (dead link)</a>"
        else:
            status = "🟢 Live" if r.get('track_status') == 'active' else "🔵 Closed"
            link = f" · <a href='https://t.me/{channel_username}/{r['message_id']}'>🔗 Open</a>"

        times_shown = r.get('times_shown') or 0
        repeat_line = ""
        if times_shown > 1:
            first_str = r['first_shown_at'].strftime('%b %d, %Y') if r.get('first_shown_at') else "unknown"
            repeat_line = f"\n 🔁 Asked {times_shown}× — first on {first_str}"

        lines.append(
            f"<code>{r['q_id']}</code> [{html.escape(str(r['difficulty']))}] {html.escape(r['topic'])}\n"
            f" {status}{link} · 📊 {r['answer_count']} answered ({r['correct_count']} correct){repeat_line}"
        )
    return "\n\n".join(lines)


def build_admin_questions_keyboard(subject: str, status_filter: str, offset: int, total: int) -> InlineKeyboardMarkup:
    subj_key = subject or "all"
    status_row = [
        InlineKeyboardButton("All", callback_data=f"admin_questions|{subj_key}:all:0"),
        InlineKeyboardButton("🟢 Posted", callback_data=f"admin_questions|{subj_key}:posted:0"),
        InlineKeyboardButton("⚪ Unposted", callback_data=f"admin_questions|{subj_key}:unposted:0"),
        InlineKeyboardButton("⚫ Deleted", callback_data=f"admin_questions|{subj_key}:deleted:0"),
    ]
    rows = [status_row]
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("⬅️ PREV", callback_data=f"admin_questions|{subj_key}:{status_filter}:{max(0, offset-10)}"))
    if offset + 10 < total:
        nav.append(InlineKeyboardButton("NEXT ➡️", callback_data=f"admin_questions|{subj_key}:{status_filter}:{offset+10}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔙 DASHBOARD", callback_data="admin_dashboard|0")])
    return InlineKeyboardMarkup(rows)


# --- HIERARCHICAL DRILL-DOWN VIEWS: World -> Country -> City -> School ---

def _medal_or_num(i: int) -> str:
    medals = ["🥇", "🥈", "🥉"]
    return medals[i] if i < 3 else str(i + 1)


# def build_geo_country_list_text(rows: list, offset: int, total: int) -> str:
    #     lines = [f"<h2>🌍 WORLD RANKINGS — Countries</h2>", f"<i>{offset+1}-{offset+len(rows)} of {total}</i>", "<hr/>"]
    #     if not rows:
    #         lines.append("<i>No country data yet.</i>")
    #         return "\n".join(lines)
    #     table = ["<tr><td><b>#</b></td><td><b>Country</b></td><td><b>Marks</b></td><td><b>Students</b></td></tr>"]
    #     for i, r in enumerate(rows):
    #         table.append(f"<tr><td>{_medal_or_num(offset+i)}</td><td>{html.escape(str(r['country']))}</td><td>{r['total_score']}</td><td>{r['student_count']}</td></tr>")
    #     lines.append("<table>" + "".join(table) + "</table>")
    #     return "\n".join(lines)


def build_geo_country_list_keyboard(rows: list, offset: int, total: int) -> InlineKeyboardMarkup:
    kb = [[InlineKeyboardButton(f"🌍 {r['country']}", callback_data=f"geo_country_detail|{r['country']}")] for r in rows]
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("⬅️ PREV", callback_data=f"geo_country_list|{max(0, offset-15)}"))
    if offset + 15 < total:
        nav.append(InlineKeyboardButton("NEXT ➡️", callback_data=f"geo_country_list|{offset+15}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("👤 PROFILE", callback_data="privacy_menu|0"), InlineKeyboardButton("🔙 CLOSE", callback_data="close_portal|0")])
    return InlineKeyboardMarkup(kb)


# def build_geo_country_detail_text(country: str, detail: dict) -> str:
    #     s = detail["summary"]
    #     rank_line = f"🥇 World Rank: <b>#{s['world_rank']}</b>" if s.get('world_rank') else "<i>Not yet ranked</i>"
    #     lines = [
    #         f"<h2>🌍 {html.escape(country)}</h2>",
    #         f"{rank_line} · <b>{s.get('total_score', 0)}</b> total marks",
    #         "<hr/>", "<h3>🌆 Top Cities</h3>"
    #     ]
    #     if not detail["cities"]:
    #         lines.append("<i>No city data registered for this country yet.</i>")
    #     else:
    #         table = ["<tr><td><b>#</b></td><td><b>City</b></td><td><b>Marks</b></td></tr>"]
    #         for i, c in enumerate(detail["cities"]):
    #             table.append(f"<tr><td>{_medal_or_num(i)}</td><td>{html.escape(str(c['city']))}</td><td>{c['total_score']}</td></tr>")
    #         lines.append("<table>" + "".join(table) + "</table>")
    #     return "\n".join(lines)


def build_geo_country_detail_keyboard(country: str, detail: dict) -> InlineKeyboardMarkup:
    kb = [[InlineKeyboardButton(f"🌆 {c['city']}", callback_data=f"geo_city_detail|{c['city']}|{country}")] for c in detail["cities"]]
    kb.append([InlineKeyboardButton("🏫 ALL SCHOOLS HERE", callback_data=f"geo_school_list|all|{country}|0")])
    kb.append([InlineKeyboardButton("🔙 COUNTRIES", callback_data="geo_country_list|0")])
    return InlineKeyboardMarkup(kb)


# def build_geo_city_detail_text(city: str, country: str, detail: dict) -> str:
    #     s = detail["summary"]
    #     rank_bits = []
    #     if s.get("world_rank"):
    #         rank_bits.append(f"🌍 World #{s['world_rank']}")
    #     if s.get("country_rank"):
    #         rank_bits.append(f"🌆 Country #{s['country_rank']}")
    #     rank_line = " · ".join(rank_bits) if rank_bits else "<i>Not yet ranked</i>"
    #     lines = [
    #         f"<h2>🌆 {html.escape(city)}</h2>",
    #         f"<i>{html.escape(country or '')}</i>",
    #         f"{rank_line} · <b>{s.get('total_score', 0)}</b> total marks",
    #         "<hr/>", "<h3>🏫 Schools</h3>"
    #     ]
    #     if not detail["schools"]:
    #         lines.append("<i>No school teams registered in this city yet.</i>")
    #     else:
    #         table = ["<tr><td><b>#</b></td><td><b>School</b></td><td><b>Marks</b></td></tr>"]
    #         for i, sc in enumerate(detail["schools"]):
    #             table.append(f"<tr><td>{_medal_or_num(i)}</td><td>{html.escape(sc['org_name'])}</td><td>{sc['total_score']}</td></tr>")
    #         lines.append("<table>" + "".join(table) + "</table>")
    #     return "\n".join(lines)


def build_geo_city_detail_keyboard(city: str, country: str, detail: dict) -> InlineKeyboardMarkup:
    kb = [[InlineKeyboardButton(f"🏫 {sc['org_name']}", callback_data=f"view_org|{sc['org_id']}")] for sc in detail["schools"]]
    back_cb = f"geo_country_detail|{country}" if country else "geo_country_list|0"
    kb.append([InlineKeyboardButton("🔙 BACK", callback_data=back_cb)])
    return InlineKeyboardMarkup(kb)


# def build_geo_school_list_text(rows: list, city: str, country: str, offset: int, total: int) -> str:
#     scope = html.escape(city) if city and city != "all" else (html.escape(country) if country and country != "all" else "World")
#     lines = [f"<h2>🏫 SCHOOLS — {scope}</h2>", f"<i>{offset+1}-{offset+len(rows)} of {total} · A-Z</i>", "<hr/>"]
#     if not rows:
#         lines.append("<i>No schools found.</i>")
#     return "\n".join(lines)


def build_geo_school_list_keyboard(rows: list, city: str, country: str, offset: int, total: int) -> InlineKeyboardMarkup:
    kb = [[InlineKeyboardButton(f"🏫 {r['org_name']} — {r['total_score']} marks", callback_data=f"view_org|{r['org_id']}")] for r in rows]
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("⬅️ PREV", callback_data=f"geo_school_list|{city}|{country}|{max(0, offset-15)}"))
    if offset + 15 < total:
        nav.append(InlineKeyboardButton("NEXT ➡️", callback_data=f"geo_school_list|{city}|{country}|{offset+15}"))
    if nav:
        kb.append(nav)
    back_cb = f"geo_country_detail|{country}" if country and country != "all" else "geo_country_list|0"
    kb.append([InlineKeyboardButton("🔙 BACK", callback_data=back_cb)])
    return InlineKeyboardMarkup(kb)

# def build_geo_grade_list_text(rows: list) -> str:
    #     lines = ["<h2>🎒 WORLD RANKINGS — Grades</h2>", "<hr/>"]
    #     if not rows:
    #         lines.append("<i>No grade data yet.</i>")
    #         return "\n".join(lines)
    #     table = ["<tr><td><b>#</b></td><td><b>Grade</b></td><td><b>Marks</b></td><td><b>Students</b></td></tr>"]
    #     for i, r in enumerate(rows):
    #         table.append(f"<tr><td>{_medal_or_num(i)}</td><td>Grade {r['grade']}</td><td>{r['total_score']}</td><td>{r['student_count']}</td></tr>")
    #     lines.append("<table>" + "".join(table) + "</table>")
    #     return "\n".join(lines)


def build_geo_grade_list_keyboard(rows: list) -> InlineKeyboardMarkup:
    kb = [[InlineKeyboardButton(f"🎒 Grade {r['grade']}", callback_data=f"geo_grade_detail|{r['grade']}")] for r in rows]
    kb.append([InlineKeyboardButton("👤 PROFILE", callback_data="privacy_menu|0"), InlineKeyboardButton("🔙 CLOSE", callback_data="close_portal|0")])
    return InlineKeyboardMarkup(kb)


# def build_geo_grade_detail_text(grade: int, detail: dict) -> str:
    #     s = detail["summary"]
    #     rank_line = f"🥇 World Rank: <b>#{s['world_rank']}</b>" if s.get('world_rank') else "<i>Not yet ranked</i>"
    #     lines = [
    #         f"<h2>🎒 Grade {grade}</h2>",
    #         f"{rank_line} · <b>{s.get('total_score', 0)}</b> total marks (this grade, worldwide)",
    #         "<hr/>", "<h3>🌍 Top Countries AT this grade</h3>"
    #     ]
    #     if not detail["top_countries"]:
    #         lines.append("<i>No country data yet.</i>")
    #     else:
    #         table = ["<tr><td><b>#</b></td><td><b>Country</b></td><td><b>Marks</b></td></tr>"]
    #         for i, c in enumerate(detail["top_countries"]):
    #             table.append(f"<tr><td>{_medal_or_num(i)}</td><td>{html.escape(str(c['country']))}</td><td>{c['total_score']}</td></tr>")
    #         lines.append("<table>" + "".join(table) + "</table>")

    #     lines.append("<h3>🌆 Top Cities AT this grade</h3>")
    #     if not detail["top_cities"]:
    #         lines.append("<i>No city data yet.</i>")
    #     else:
    #         table = ["<tr><td><b>#</b></td><td><b>City</b></td><td><b>Marks</b></td></tr>"]
    #         for i, c in enumerate(detail["top_cities"]):
    #             table.append(f"<tr><td>{_medal_or_num(i)}</td><td>{html.escape(str(c['city']))}</td><td>{c['total_score']}</td></tr>")
    #         lines.append("<table>" + "".join(table) + "</table>")

    #     lines.append("<i>Rankings here compare only students in this grade — not each place's overall score.</i>")
    #     return "\n".join(lines)


def build_geo_grade_detail_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 GRADES", callback_data="geo_grade_list|0")]])

def build_organization_grade_breakdown_text(grade_rows: list) -> str:
    if not grade_rows:
        return ""
    lines = [
        "<h3>🎒 School Ranks by Grade</h3>",
        "<table><tr><td><b>Grade</b></td><td><b>Marks</b></td><td><b>City</b></td><td><b>Country</b></td><td><b>World</b></td></tr>"
    ]
    for r in grade_rows:
        lines.append(f"<tr><td>Grade {r['grade']}</td><td>{r['school_grade_score']}</td><td>#{r['city_rank']}</td><td>#{r['country_rank']}</td><td>#{r['world_rank']}</td></tr>")
    lines.append("</table>")
    return "\n".join(lines)

# def build_world_rank_text(scope: str, grade: str, matrix: dict, summary: dict = None) -> str:
    #     scope_labels = {"world": "🌍 WORLD", "country": "🌎 COUNTRY", "city": "🌆 CITY", "school": "🏫 SCHOOL"}
    #     lines = [f"<h2>{scope_labels.get(scope, 'World')} RANKINGS</h2>"]

    #     s = summary or {}
    #     lines.append(
    #         f"👥 {s.get('student_count', 0)} students · 🏫 {s.get('school_count', 0)} schools · "
    #         f"🏢 {s.get('team_count', 0)} teams · 🌆 {s.get('city_count', 0)} cities · "
    #         f"🌍 {s.get('country_count', 0)} countries"
    #     )
    #     lines.append(f"<b>{s.get('total_marks', 0)}</b> total marks · <b>{int(s.get('avg_marks', 0))}</b> average")
    #     lines.append(f"<i>{'All grades' if grade in (None, 'all') else f'Grade {grade}'}</i>")
    #     lines.append("<hr/>")

    #     cols = {
    #         "world": [("students", "Students"), ("teams", "Teams"), ("schools", "Schools"), ("cities", "Cities"), ("countries", "Countries")],
    #         "country": [("students", "Students"), ("teams", "Teams"), ("schools", "Schools"), ("cities", "Cities")],
    #         "city": [("students", "Students"), ("teams", "Teams"), ("schools", "Schools")],
    #         "school": [("students", "Students"), ("teams", "Teams")],
    #     }.get(scope, [("students", "Students")])

    #     medals = ["🥇", "🥈", "🥉"]
    #     header_row = "<tr><td><b>#</b></td>" + "".join(f"<td><b>{label}</b></td>" for _, label in cols) + "</tr>"

    #     # Always render 10 rows — skeleton dashes for any column/rank with no data yet.
    #     body_rows = []
    #     for i in range(10):
    #         rank = medals[i] if i < 3 else str(i + 1)
    #         cells = []
    #         for key, _ in cols:
    #             col_data = matrix.get(key, [])
    #             if i < len(col_data):
    #                 cells.append(f"<td>{html.escape(str(col_data[i]['name']))} ({col_data[i]['score']})</td>")
    #             else:
    #                 cells.append("<td>—</td>")
    #         body_rows.append(f"<tr><td>{rank}</td>{''.join(cells)}</tr>")

    #     lines.append("<table>" + header_row + "".join(body_rows) + "</table>")
    #     return "\n".join(lines)

# def build_world_rank_keyboard(scope: str, grade: str) -> InlineKeyboardMarkup:
    #     def _cb(s=None, g=None):
    #         return f"wr|{s or scope}|{g if g is not None else grade}"

    #     def _scope_btn(s, label):
    #         return InlineKeyboardButton(("• " if scope == s else "") + label, callback_data=_cb(s=s))

    #     rows = [
    #         [_scope_btn("world", "🌍 World"), _scope_btn("country", "🌎 Country")],
    #         [_scope_btn("city", "🌆 City"), _scope_btn("school", "🏫 School")],
    #     ]

    #     grade_vals = [("all", "All"), ("12", "12"), ("10", "10"), ("8", "8"), ("6", "6")]
    #     rows.append([InlineKeyboardButton(("• " if grade == v else "") + label, callback_data=_cb(g=v)) for v, label in grade_vals])

    #     rows.append([InlineKeyboardButton("👤 PROFILE", callback_data="privacy_menu|0"),
    #                  InlineKeyboardButton("🔙 CLOSE", callback_data="close_portal|0")])
    #     return InlineKeyboardMarkup(rows)


# def _filter_chip_line(scope, entity, grade, subject, difficulty, mode) -> str:
#     labels = {"world": "🌍 World", "country": f"🌎 {entity or 'Country'}", "city": f"🏙 {entity or 'City'}", "school": "🏫 School"}
#     parts = [labels.get(scope, scope), "📈 Average" if mode == "average" else "🏆 Total"]
#     if grade not in (None, "all"):
#         parts.append(f"🎓 G{grade}")
#     if subject not in (None, "all"):
#         parts.append(f"📚 {html.escape(subject.title())}")
#     if difficulty not in (None, "all"):
#         parts.append(f"⚡ {difficulty.title()}")
#     return " • ".join(parts)


def _filter_chip_line(scope, entity, grade, subject, difficulty, mode) -> str:
    labels = {"world": "🌍 World", "country": f"🌎 {entity or 'Country'}", "city": f"🏙 {entity or 'City'}", "school": "🏫 School"}
    parts = [f"📍 {labels.get(scope, scope)}", "📈 Average" if mode == "average" else "🏆 Total"]
    if grade not in (None, "all"):
        parts.append(f"🎓 G{grade}")
    if subject not in (None, "all"):
        parts.append(f"📚 {html.escape(subject.title())}")
    if difficulty not in (None, "all"):
        parts.append(f"⚡ {difficulty.title()}")
    return " • ".join(parts)


def build_leaderboard_text(scope, entity, grade, subject, difficulty, mode, matrix, summary) -> str:
    s = summary or {}
    titles = {"world": "🌍 WORLD LEADERBOARD", "country": f"🌎 {html.escape(str(entity or ''))}",
              "city": f"🏙 {html.escape(str(entity or ''))}", "school": "🏫 SCHOOL"}
    lines = [f"<h2>{titles.get(scope, 'LEADERBOARD')}</h2>"]

    rank_bits = []
    if scope == "country" and s.get("parent_ranks", {}).get("world"):
        rank_bits.append(f"🌍 World #{s['parent_ranks']['world']}")
    elif scope == "city" and s.get("parent_country"):
        rank_bits.append(f"🇨 {html.escape(s['parent_country'])}")
    elif scope == "school":
        if s.get("parent_city"):
            rank_bits.append(html.escape(s["parent_city"]))
        if s.get("parent_country"):
            rank_bits.append(html.escape(s["parent_country"]))
    if rank_bits:
        lines.append(" · ".join(rank_bits))

    lines.append(f"🏆 Cumulative: <b>{s.get('total_marks', 0):,}</b> · 📈 Average: <b>{int(s.get('avg_marks', 0))}</b>")

    stat_cols = {
        "world": [("student_count", " Students 👨‍🎓  |"), ("team_count", " Teams 👥|"), ("school_count", " Schools 🏫 |"), ("city_count", "🏙"), ("country_count", " Country 🌍 |")],
        "country": [("student_count", "Students 👨‍🎓 |"), ("team_count", " Teams 👥 |"), ("school_count", "Schools 🏫 |"), ("city_count", " City 🏙 |")],
        "city": [("student_count", "Students 👨‍🎓 | "), ("team_count", " Teams 👥 |"), ("school_count", "Schools 🏫 |")],
        "school": [("student_count", "Students 👨‍🎓 |"), ("team_count", " Teams 👥 |")],
    }.get(scope, [("student_count", "Students 👨‍🎓 |")])
    lines.append("  ".join(f"{icon} <b>{s.get(key, 0):,}</b>" for key, icon in stat_cols))
    lines.append("<hr/>")

    table_cols = {
        "world": [("students", "👨‍🎓 Students"), ("teams", "👥 Teams"), ("schools", "🏫 Schools"), ("cities", "🏙 Cities"), ("countries", "🌍 Countries")],
        "country": [("students", "👨‍🎓 Students"), ("teams", "👥 Teams"), ("schools", "🏫 Schools"), ("cities", "🏙 Cities")],
        "city": [("students", "👨‍🎓 Students"), ("teams", "👥 Teams"), ("schools", "🏫 Schools")],
        "school": [("students", "👨‍🎓 Students"), ("teams", "👥 Teams")],
    }.get(scope, [("students", "👨‍🎓 Students")])

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    header_row = "<tr><td></td>" + "".join(f"<td><b>{label}</b></td>" for _, label in table_cols) + "</tr>"
    body_rows = []
    for i in range(10):
        cells = []
        for key, _ in table_cols:
            col = matrix.get(key, [])
            cells.append(f"<td>{html.escape(str(col[i]['name']))} ({col[i]['score']})</td>" if i < len(col) else "<td>—</td>")
        body_rows.append(f"<tr><td>{medals[i]}</td>{''.join(cells)}</tr>")

    lines.append("<table>" + header_row + "".join(body_rows) + "</table>")
    lines.append(f"<i>{_filter_chip_line(scope, entity, grade, subject, difficulty, mode)}</i>")
    return "\n".join(lines)


def build_leaderboard_keyboard(scope, entity, grade, subject, difficulty, mode, edit="none", subjects_list=None, soff=0) -> InlineKeyboardMarkup:
    def _cb(s=None, ent=None, g=None, sub=None, d=None, m=None, e=None, so=None):
        return "|".join([
            "wr", s or scope, str(ent if ent is not None else (entity or "_")),
            g if g is not None else grade, sub if sub is not None else subject,
            d if d is not None else difficulty, m or mode, e if e is not None else edit,
            str(so if so is not None else soff)
        ])

    def _scope_btn(s, label):
        if s in ("country", "city", "school") and scope != s:
            return InlineKeyboardButton(label, callback_data=f"wrsel_ctry|nav_{s}|0")
        return InlineKeyboardButton(("• " if scope == s else "") + label, callback_data=_cb(s=s, ent="_"))

    rows = [
        [_scope_btn("world", "🌍 WORLD"), _scope_btn("country", "🌎 COUNTRY")],
        [_scope_btn("city", "🏙 CITY"), _scope_btn("school", "🏫 SCHOOL")],
        [InlineKeyboardButton(("• " if mode == "total" else "") + "🏆 TOTAL", callback_data=_cb(m="total")),
         InlineKeyboardButton(("• " if mode == "average" else "") + "📈 AVERAGE", callback_data=_cb(m="average"))],
        [InlineKeyboardButton(("• " if edit == "grade" else "") + "🎓 GRADE", callback_data=_cb(e="grade" if edit != "grade" else "none")),
         InlineKeyboardButton(("• " if edit == "subject" else "") + "📚 SUBJECT", callback_data=_cb(e="subject" if edit != "subject" else "none")),
         InlineKeyboardButton(("• " if edit == "difficulty" else "") + "⚡ DIFFICULTY", callback_data=_cb(e="difficulty" if edit != "difficulty" else "none"))],
    ]

    if edit == "grade":
        vals = [("all", "All"), ("6", "6"), ("8", "8"), ("10", "10"), ("12", "12")]
        rows.append([InlineKeyboardButton(("• " if grade == v else "") + label, callback_data=_cb(g=v)) for v, label in vals])
    elif edit == "difficulty":
        vals = [("all", "All"), ("easy", "Easy"), ("medium", "Medium"), ("hard", "Hard")]
        rows.append([InlineKeyboardButton(("• " if difficulty == v else "") + label, callback_data=_cb(d=v)) for v, label in vals])
    elif edit == "subject":
        subjects_list = subjects_list or []
        page = subjects_list[soff:soff + 8]
        rows.append([InlineKeyboardButton(("• " if subject == "all" else "") + "All", callback_data=_cb(sub="all"))])
        for i in range(0, len(page), 4):
            rows.append([InlineKeyboardButton(("• " if subject == sv else "") + sv.title()[:10], callback_data=_cb(sub=sv)) for sv in page[i:i+4]])
        nav = []
        if soff > 0:
            nav.append(InlineKeyboardButton("◀ Prev", callback_data=_cb(so=max(0, soff - 8))))
        if soff + 8 < len(subjects_list):
            nav.append(InlineKeyboardButton("Next ▶", callback_data=_cb(so=soff + 8)))
        if nav:
            rows.append(nav)

    if scope in ("country", "city", "school") and (not entity or entity == "_"):
        rows.append([InlineKeyboardButton(f"🔄 SWITCH {scope.upper()}", callback_data=f"wrsel_ctry|nav_{scope}|0")])

    rows.append([
        InlineKeyboardButton("⭐ FAVORITES", callback_data="wr_fav_menu|0"),
        InlineKeyboardButton("👤 PROFILE", callback_data="privacy_menu|0"),
    ])
    rows.append([
        InlineKeyboardButton("📢 CHANNEL", url=f"https://t.me/{CONFIG.get('channel', 'QuizOva').lstrip('@')}"),
        InlineKeyboardButton("❌ CLOSE", callback_data="close_portal|0"),
    ])
    return InlineKeyboardMarkup(rows)


def build_entity_picker_text(scope, parent_entity=None) -> str:
    label = {"country": "🌎 SELECT A COUNTRY", "city": f"🏙 SELECT A CITY — {html.escape(str(parent_entity or ''))}",
             "school": f"🏫 SELECT A SCHOOL — {html.escape(str(parent_entity or ''))}"}
    return f"<h2>{label.get(scope, 'SELECT')}</h2>\n<hr/>\nTap one below."


def build_entity_picker_keyboard(scope, parent_entity, items, offset, back_scope) -> InlineKeyboardMarkup:
    rows = []
    page = items[offset:offset + 10]
    for it in page:
        val = str(it.get("id")) if scope == "school" else it["name"]
        rows.append([InlineKeyboardButton(it["name"], callback_data=f"wr_pick_go|{scope}|{val}")])
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"wr_pick|{scope}|{parent_entity or '_'}|{max(0, offset-10)}"))
    if offset + 10 < len(items):
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"wr_pick|{scope}|{parent_entity or '_'}|{offset+10}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔙 BACK", callback_data=f"wr|{back_scope}|_|all|all|all|total|none|0")])
    return InlineKeyboardMarkup(rows)