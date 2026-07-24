# src/rendering/html_views.py
import html
import re
from src.config import CONFIG
from src.typography import clean_latex_to_unicode, lite_math, beautify_markdown_math
from src.rendering.latex_templates import get_day_from_tags, sanitize_tag_to_hashtag, is_complex
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

def replace_code_with_italic(text: str) -> str:
    return text.replace("<code>", "<i>").replace("</code>", "</i>") if text else ""

def smart_truncate_html(text: str, max_len: int) -> str:
    if not text or len(text) <= max_len:
        return text or ""
    sentences = re.split(r'(?<=[.!?])\s+', text)
    accumulated = ""
    for sentence in sentences:
        if len(accumulated) + len(sentence) + 4 > max_len:
            break
        accumulated += (sentence + " ")
    accumulated = accumulated.strip() or text[:max_len - 3].strip()
    if accumulated.count('$') % 2 != 0:
        accumulated += '$'
    tag_pattern = re.compile(r'<(/?)(code|b|i|span|tg-spoiler|a)(?:\s+[^>]*?)?>')
    open_tags = []
    for match in tag_pattern.finditer(accumulated):
        if not match.group(1):
            open_tags.append(match.group(2))
        elif open_tags and open_tags[-1] == match.group(2):
            open_tags.pop()
    for tag in reversed(open_tags):
        accumulated += f'</{tag}>'
    return accumulated + "..."

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

    exp = q.get("poll_explanation", {})
    why = exp.get('why', 'No detailed explanation provided.')
    rule_text = exp.get('governing_principle') or exp.get('rule') or 'General Concept'

    general_principle = (
        f"<blockquote>\n"
        f"<b>🏛️ GENERAL PRINCIPLE</b><br/>\n"
        f"<i>{beautify_markdown_math(rule_text)}</i>\n"
        f"</blockquote>"
    )

    step_by_step_parts = [
        f"<blockquote expandable>\n"
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
        f"<blockquote expandable>\n"
        f"<b>🔍 OPTION BREAKDOWN</b>\n"
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
            analysis_line += f"\n  {beautify_markdown_math(example_text)}"
        breakdown_parts.append(analysis_line)
    breakdown_parts.append("</blockquote>")
    breakdown_block = "\n".join(breakdown_parts)

    general_principle = replace_code_with_italic(general_principle)
    step_by_step = replace_code_with_italic(step_by_step)
    breakdown_block = replace_code_with_italic(breakdown_block)

    if continuation:
        spoiler_content = f"🎯 <b>CORRECT OPTION: [{correct_letter}]</b>\n\n{general_principle}\n{step_by_step}\n{breakdown_block}"
        connection_header = f"<b>📖 DETAILED SOLUTION (CONTINUATION) • REF <code>{display_id}</code></b>\n<hr/>"
        return f"{connection_header}🎯 <b>REVEAL SOLUTION DETAILS:</b>\n<tg-spoiler>{spoiler_content}</tg-spoiler>"

    subject_text = beautify_markdown_math(q.get('subject','').upper())
    topic_text = beautify_markdown_math(q.get('topic','General'))
    header = (
        f"🎓 <b>{subject_text}</b> • REF <code>{display_id}</code>\n"
        f"📐 <b>{topic_text}</b> • 📅 {day_str}\n<hr/>"
    )

    body = (
        f"<blockquote>"
        f"<b>PROBLEM PROPOSITION</b><br/>"
        f"{beautify_markdown_math(q['question'])}"
        f"</blockquote>"
    )

    if compact:
        opts_block = ""
        spoiler_content = f"🎯 <b>CORRECT OPTION: [{correct_letter}]</b>"
        footer_note = ""
    else:
        opts_list = ["📋 <b>OPTIONS</b>", "<ul>"]
        for i, o in enumerate(q['options']):
            opts_list.append(f"  <li><b>{chr(65+i)})</b> {beautify_markdown_math(o)}</li>")
        opts_list.append("</ul>")
        opts_block = "\n" + "\n".join(opts_list)
        spoiler_content = f"🎯 <b>CORRECT OPTION: [{correct_letter}]</b>\n\n{general_principle}\n{step_by_step}\n{breakdown_block}"
        footer_note = ""

    spoiler_content = replace_code_with_italic(spoiler_content)
    hashtag_list = [sanitize_tag_to_hashtag(t) for t in q.get('tags', [])]

    footer = (
        f"\n<hr/>\n"
        f"📢 <b>Channel:</b> <a href='https://t.me/grade12EntranceExam'>@grade12EntranceExam</a>\n"
        f"{' '.join(hashtag_list)}{footer_note}"
    )

    components = [header, body]
    if opts_block:
        components.append(opts_block)
    components.append(f"<hr/>\n🎯 <b>TAP TO REVEAL KEY ANSWER & SOLUTION:</b>\n<tg-spoiler>{spoiler_content}</tg-spoiler>")
    components.append(footer)

    return "\n".join(components)

def build_answered_view(q, display_id: str, user_idx: int, show_derivation=False, show_perf=False, mode="compact", compact=None, perf_card=None, continuation=False) -> str:
    # Defensively support legacy configuration states for backward compatibility
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
    day_str = get_day_from_tags(q.get('tags', []))

    exp = q.get("poll_explanation", {})
    why = exp.get('why', 'No step-by-step derivation available.')
    rule_text = exp.get('governing_principle') or exp.get('rule') or 'General Concept'

    general_principle = (
        f"<blockquote>\n"
        f"<b>🏛️ GENERAL PRINCIPLE</b><br/>\n"
        f"<i>{beautify_markdown_math(rule_text)}</i>\n"
        f"</blockquote>"
    )

    step_by_step_parts = [
        f"<blockquote expandable>\n"
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
        f"<blockquote expandable>\n"
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

    # Render score card section dynamically if turned on
    score_segment = ""
    if show_perf and perf_card:
        if not perf_card['first_try']:
            marks_notice = "⚠️ <i>Practice Mode: Score locked. No marks awarded.</i>"
        elif perf_card['is_bonus_winner']:
            marks_notice = "⚡ <b>EARLY BIRD BONUS!</b> You solved this first! <b>(+10 Marks)</b>"
        elif perf_card['marks_awarded'] > 0:
            marks_notice = "🟩 <b>CORRECT!</b> Standard score awarded. <b>(+2 Marks)</b>"
        else:
            marks_notice = "🟥 <b>INCORRECT.</b> No marks awarded. <b>(+0 Marks)</b>"

        mastery = get_grade_mastery_title(perf_card['total_marks'])
        next_rank_info = get_next_rank_info(perf_card['total_marks'])

        score_segment = (
            f"<hr/>\n"
            f"📊 <b>STUDY PERFORMANCE CARD</b>\n"
            f"<p>{marks_notice}</p>\n"
            f"<table>\n"
            f"  <tr>\n"
            f"    <td>🎒 <b>Academic Level:</b></td>\n"
            f"    <td>Grade {perf_card.get('grade', 12)}</td>\n"
            f"  </tr>\n"
            f"  <tr>\n"
            f"    <td>📝 <b>Practice Score:</b></td>\n"
            f"    <td><b>{perf_card['total_marks']} Marks</b></td>\n"
            f"  </tr>\n"
            f"  <tr>\n"
            f"    <td>🏆 <b>Mastery Level:</b></td>\n"
            f"    <td><b>{mastery}</b></td>\n"
            f"  </tr>\n"
            f"  <tr>\n"
            f"    <td>🎯 <b>Accuracy Rate:</b></td>\n"
            f"    <td><b>{perf_card['accuracy']}%</b> ({perf_card['correct']} of {perf_card['total']})</td>\n"
            f"  </tr>\n"
            f"</table>\n"
            f"💡 <i>Target: {next_rank_info}</i>\n"
        )

    # --- Standalone Followup Continuation Responses (Unified and Combined) ---
    if continuation:
        parts = []
        if show_derivation:
            parts.append(f"<b>📖 DERIVATION DETAILS:</b>\n{general_principle}\n{step_by_step}\n{breakdown_block}")
        if show_perf and score_segment:
            parts.append(score_segment)
            
        connection_header = f"<b>📝 DETAILED EXPLANATION SHEET • REF <code>{display_id}</code></b>\n<hr/>"
        return f"{connection_header}\n" + "\n\n".join(parts)

    subject_text = beautify_markdown_math(q.get('subject','').upper())
    topic_text = beautify_markdown_math(q.get('topic','General'))
    header = (
        f"🎓 <b>{subject_text}</b> • REF <code>{display_id}</code>\n"
        f"📐 <b>{topic_text}</b> • 📅 {day_str}\n<hr/>"
    )

    body = (
        f"<blockquote>"
        f"<b>PROBLEM PROPOSITION</b><br/>"
        f"{beautify_markdown_math(q['question'])}"
        f"</blockquote>"
    )

    user_val = q['options'][user_idx] if user_idx < len(q['options']) else "Unknown"
    correct_val = q['options'][correct_idx]

    # Displays selection letters alongside text values
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
    footer = (
        f"\n<hr/>\n"
        f"📢 <b>Channel:</b> <a href='https://t.me/grade12EntranceExam'>@grade12EntranceExam</a>\n"
        f"{' '.join(hashtag_list)}"
    )

    components = [header, body]
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

def build_answered_keyboard(d_id: str, user_selection: int, show_derivation: bool, show_perf: bool, is_photo=False) -> InlineKeyboardMarkup:
    """Generates a composite, binary state-driven keyboard mapping the exact on/off layout combination."""
    prefix = "toggle_photo" if is_photo else "toggle"
    buttons = []
    
    # Derivation toggle button
    if show_derivation:
        buttons.append([InlineKeyboardButton("↩️ HIDE SOLUTION DETAILS", callback_data=f"{prefix}|{d_id}|{user_selection}|0|{1 if show_perf else 0}")])
    else:
        buttons.append([InlineKeyboardButton("📖 REVEAL COMPLETE DERIVATION", callback_data=f"{prefix}|{d_id}|{user_selection}|1|{1 if show_perf else 0}")])
        
    # Performance card toggle button
    if show_perf:
        buttons.append([InlineKeyboardButton("↩️ HIDE PERFORMANCE CARD", callback_data=f"{prefix}|{d_id}|{user_selection}|{1 if show_derivation else 0}|0")])
    else:
        buttons.append([InlineKeyboardButton("📊 VIEW PERFORMANCE CARD", callback_data=f"{prefix}|{d_id}|{user_selection}|{1 if show_derivation else 0}|1")])
        
    buttons.append([InlineKeyboardButton("📣 RETURN TO CHANNEL", url="https://t.me/grade12EntranceExam")])
    return InlineKeyboardMarkup(buttons)

def build_keyboard(q, display_id: str) -> InlineKeyboardMarkup:
    letters = ["𝗔", "𝗕", "𝗖", "𝗗", "𝗘"]
    bot_user = CONFIG.get("bot_username", "EthiopiaEntranceExamBot")
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