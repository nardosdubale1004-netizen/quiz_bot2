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

def build_closed_static_view(q, display_id: str, compact=False, continuation=False) -> str:
    correct_letter = chr(65 + q['correct_option'])
    day_str = get_day_from_tags(q.get('tags', []))

    hashtag_list = [sanitize_tag_to_hashtag(t) for t in q.get('tags', [])]
    channel_name = CONFIG.get("channel", "@QuizOva")
    channel_username = channel_name.lstrip('@')

    footer = (
        f"\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>REF <code>{display_id}</code></b> │ <a href='https://t.me/{channel_username}'>{channel_name}</a>\n"
        f"{' '.join(hashtag_list)}"
    )

    from src.rendering.latex_templates import has_real_diagram
    has_tikz = has_real_diagram(q)

    if compact:
        raw_question = beautify_markdown_math(q['question'])
        body_plain = f"<b>PROBLEM PROPOSITION</b>\n{raw_question}"

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
            body_plain = f"<b>PROBLEM PROPOSITION</b>\n{truncated_question}"

            components = [body_plain, opts_block, spoiler_block, footer]
            caption_text = "\n\n".join(components)

        return caption_text

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

    general_principle = (
        f"🏛️ <b>GENERAL PRINCIPLE:</b>\n"
        f"<i>{beautify_markdown_math(rule_text)}</i>\n"
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
    breakdown_parts = [
        f"🔍 <b>OPTION BREAKDOWN:</b>\n"
    ]
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

    spoiler_content = f"🎯 <b>CORRECT OPTION: [{correct_letter}]</b>\n\n{general_principle}\n{step_by_step}\n{breakdown_block}"
    spoiler_content = spoiler_content.replace("<tg-math-block>", "<tg-math>").replace("</tg-math-block>", "</tg-math>")
    spoiler_content = replace_code_with_italic(spoiler_content)

    if continuation:
        connection_header = f"<b>📖 DETAILED EXPLANATION SHEET • REF <code>{display_id}</code></b>\n<hr/>"
        components = [
            connection_header,
            body,
            opts_block,
            f"<hr/>\n🎯 <b>TAP TO REVEAL KEY ANSWER & SOLUTION:</b>\n<tg-spoiler>{spoiler_content.strip()}</tg-spoiler>",
            footer
        ]
        return "\n\n".join(components)

    components = [
        body,
        opts_block,
        f"<hr/>\n🎯 <b>TAP TO REVEAL KEY ANSWER & SOLUTION:</b>\n<tg-spoiler>{spoiler_content.strip()}</tg-spoiler>",
        footer
    ]
    return "\n\n".join(components)

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
        f"<b>🏛️ GENERAL PRINCIPLE</b><br/>"
        f"<i>{beautify_markdown_math(rule_text)}</i>"
        f"</blockquote>"
    )

    step_by_step_parts = [
        f"<blockquote expandable>"
        f"<b>🔢 STEP-BY-STEP DERIVATION</b>\n"
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
        f"<blockquote expandable>"
        f"<b>🔍 OPTION BREAKDOWN</b>\n"
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
        is_bonus = perf_card.get('is_bonus_winner', False) or (orig_marks == 10)

        status_prefix = "⚠️ <b>Score Locked (Previously Answered):</b><br/>" if is_repeat else ""

        if is_bonus:
            marks_notice = f"{status_prefix}⚡ <b>EARLY BIRD BONUS!</b> You solved this first! <b>(+10 Marks)</b>"
        elif orig_marks > 0:
            marks_notice = f"{status_prefix}🟩 <b>CORRECT!</b> Standard score awarded. <b>(+2 Marks)</b>"
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


def build_profile_card_text(profile: dict, roster: list = None) -> str:
    """
    Builds a beautifully simple student profile dossier card
    incorporating Telegram's advanced <blockquote expandable> layout.
    """
    real_name = format_public_name(profile)
    grade = profile.get("grade") or "Not selected"
    marks = profile.get("total_marks", 0)
    streak = profile.get("current_streak", 0)
    total = profile.get("total", 0)
    correct = profile.get("correct", 0)
    accuracy = int((correct / total) * 100) if total > 0 else 0
    mastery = get_grade_mastery_title(marks)
    next_rank = get_next_rank_info(marks)
    
    # Simple and clear visibility configurations
    if profile.get("public_consent_granted"):
        consent_status = "🟢 PUBLIC\n<i>(Your actual Telegram name is displayed on weekly leaderboards)</i>"
    else:
        consent_status = "🕵️ PRIVATE\n<i>(Your real name is completely hidden. You appear anonymously as an ID)</i>"
        
    nickname_label = profile.get("nickname") or "<i>None set (using default masked id)</i>"
    
    org_tag = profile.get("org_tag")
    org_name = profile.get("org_name")
    org_role = profile.get("org_role")

    # Simple visual indicator progress bar
    filled = min(10, max(0, int(accuracy / 10)))
    progress = f"<code>[{'■' * filled}{' ' * (10 - filled)}]</code>"

    text = (
        f"👤 <b>YOUR ACADEMIC DOSSIER</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👋 <b>Welcome!</b> Here is your live study profile and settings:\n\n"
        f"• <b>Custom score Name:</b> {nickname_label}\n"
        f"• <b>Academic Level:</b> <code>Grade {grade}</code>\n"
        f"• <b>Scoreboard Visibility:</b> {consent_status}\n\n"
        f"📊 <b>YOUR PRACTICE STATS:</b>\n"
        f"<blockquote expandable>"
        f"🏆 <b>Practice Score:</b> {marks} Marks\n"
        f"🎖️ <b>Mastery Level:</b> {mastery}\n"
        f"🔥 <b>Active Streak:</b> {streak} Days\n"
        f"🎯 <b>Accuracy Rate:</b> {progress} <b>{accuracy}%</b> ({correct}/{total} solved)\n\n"
        f"<i>💡 Rank Progress: {next_rank}</i>"
        f"</blockquote>\n\n"
    )

    if org_tag:
        text += (
            f"🏫 <b>YOUR SCHOOL ALLIANCE:</b>\n"
            f"• <b>Institution:</b> {org_name}\n"
            f"• <b>Domain Code:</b> <code>#{org_tag}</code> (Role: {org_role.capitalize()})\n\n"
        )
        if roster:
            total_marks = sum(r.get("total_marks", 0) for r in roster)
            text += f"👥 <b>ALLIANCE MEMBERS ({len(roster)}):</b>\n"
            text += f"<blockquote expandable>\n"
            text += f"🏆 <b>Total Alliance Score:</b> {total_marks} Marks\n\n"
            
            medals = ["🥇", "🥈", "🥉", "▫️", "▫️", "▫️", "▫️", "▫️", "▫️", "▫️"]
            for idx, r in enumerate(roster[:10]):
                user_label = format_public_name(r)
                role_icon = " 👑" if r.get("org_role") == "creator" else ""
                text += f" {medals[idx]} <code>{user_label}</code> — <b>{r['total_marks']} Marks</b>{role_icon}\n"
            text += f"</blockquote>\n"
    else:
        text += (
            f"🏫 <b>SCHOOL TEAM ALLIANCE:</b>\n"
            f"<i>You are not linked to any school or study club team yet. Join or establish one using the buttons below to compare and aggregate scores collectively!</i>\n"
        )

    text += "\n━━━━━━━━━━━━━━━━━━━━━━━"
    return text


def build_organization_card_text(org: dict, roster: list) -> str:
    """
    Builds a beautiful, simple, and expandable card displaying details and 
    member rosters for a specific school team.
    """
    name = org.get("org_name", "UNKNOWN").upper()
    tag = org.get("org_tag", "UNKNOWN")
    org_type = org.get("org_type", "School")
    
    total_score = sum(r.get("total_marks", 0) for r in roster)
    avg_score = int(total_score / len(roster)) if roster else 0
    
    roster_lines = []
    medals = ["🥇", "🥈", "🥉", "▫️", "▫️", "▫️", "▫️", "▫️", "▫️", "▫️"]
    for idx, r in enumerate(roster[:10]):
        formatted_name = format_public_name(r)
        role_marker = " 👑" if r.get("org_role") == "creator" else " 🛡️" if r.get("org_role") == "admin" else ""
        roster_lines.append(f" {medals[idx]} <code>{formatted_name}</code> — <b>{r['total_marks']} Marks</b>{role_marker}")
        
    roster_block = "\n".join(roster_lines) if roster_lines else "<i>No active scholars registered.</i>"

    text = (
        f"🏫 <b>SCHOOL TEAM: {name}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"• <b>Domain Code:</b> <code>#{tag}</code>\n"
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