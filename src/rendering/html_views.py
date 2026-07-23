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
    if accumulated.count('$') % 2 != 0: accumulated += '$'
    tag_pattern = re.compile(r'<(/?)(code|b|i|span|tg-spoiler|a)(?:\s+[^>]*?)?>')
    open_tags = []
    for match in tag_pattern.finditer(accumulated):
        if not match.group(1): open_tags.append(match.group(2))
        elif open_tags and open_tags[-1] == match.group(2): open_tags.pop()
    for tag in reversed(open_tags): accumulated += f'</{tag}>'
    return accumulated + "..."

def get_grade_mastery_title(marks: int) -> str:
    if marks == 0: return "🌱 Candidate (Practice Mode)"
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
    correct_idx = q['correct_option']
    letters = ["A", "B", "C", "D", "E"]
    correct_letter = letters[correct_idx] if correct_idx < len(letters) else "?"

    # 1. Continuation Layout
    if continuation:
        exp = q.get("poll_explanation", {})
        why = exp.get('why', 'No detailed solution available.')
        rule_text = exp.get('governing_principle') or exp.get('rule') or 'General Concept'

        explanation_part = (
            f"📝 <b>DETAILED SOLUTION:</b>\n"
            f"<b>Principle:</b>\n"
            f"<blockquote expandable>"
            f"🏛️ <b>{beautify_markdown_math(rule_text)}</b>"
            f"</blockquote>\n\n"
            f"<b>Step-by-Step Derivation:</b>\n"
            f"{beautify_markdown_math(why)}\n"
        )
        if exp.get('analogy'):
            explanation_part += f"\n💡 <b>Analogy:</b>\n{beautify_markdown_math(exp['analogy'])}\n"
        if exp.get('memory_tip'):
            explanation_part += f"\n🧠 <b>Memory Tip:</b>\n{beautify_markdown_math(exp['memory_tip'])}\n"

        analysis_list = ["🔍 <b>OPTION-BY-OPTION BREAKDOWN:</b>"]
        options_analysis = q.get('options_analysis', [])
        for i, o_text in enumerate(q.get('options', [])):
            let = letters[i] if i < len(letters) else str(i+1)
            is_opt_correct = (i == correct_idx)
            status_icon = "🟢" if is_opt_correct else "⚪"
            verdict_badge = "✅ <b>Correct.</b>" if is_opt_correct else "❌ <i>Incorrect.</i>"

            why_text = ""
            example_text = ""
            if i < len(options_analysis):
                why_text = options_analysis[i].get('why', '')
                example_text = options_analysis[i].get('example', '')

            cleaned_opt_val = lite_math(o_text)
            cleaned_why = beautify_markdown_math(why_text)

            analysis_line = f"• {status_icon} <b>Option {let} ({cleaned_opt_val}):</b> {verdict_badge} {cleaned_why}"
            if example_text:
                cleaned_example = beautify_markdown_math(example_text)
                analysis_line += f" (<i>e.g., {cleaned_example}</i>)"
            analysis_list.append(analysis_line)

        analysis_str = "\n".join(analysis_list)

        spoiler_content = f"🎯 <b>CORRECT OPTION: [{correct_letter}]</b>\n\n{explanation_part}\n{analysis_str}"
        connection_header = (
            f"📖 <b>DETAILED SOLUTION (CONTINUATION)</b> • REF <code>{display_id}</code>\n"
            f"<hr/>\n"
        )
        return f"{connection_header}🎯 <b>REVEAL SOLUTION DETAILS:</b>\n<tg-spoiler>{spoiler_content}</tg-spoiler>"

    # 2. Main Layout
    day_str = get_day_from_tags(q.get('tags', []))
    subject_str = q.get('subject', '').upper()
    topic_str = q.get('topic', 'General')

    header = (
        f"🎓 <b>{subject_str}</b> • REF <code>{display_id}</code>\n"
        f"📐 <b>{topic_str}</b> • 📅 {day_str}\n"
        f"<hr/>\n\n"
    )

    body = (
        f"<blockquote>"
        f"<b>PROBLEM PROPOSITION</b>\n"
        f"{beautify_markdown_math(q['question'])}"
        f"</blockquote>\n\n"
    )

    opts_list = ["📋 <b>OPTIONS:</b>"]
    for i, o in enumerate(q['options']):
        let = letters[i] if i < len(letters) else str(i+1)
        clean_opt = beautify_markdown_math(o)
        opts_list.append(f"• <b>{let})</b> {clean_opt}")
    opts_block = "\n".join(opts_list) + "\n\n"

    exp = q.get("poll_explanation", {})
    why = exp.get('why', 'No detailed solution available.')
    rule_text = exp.get('governing_principle') or exp.get('rule') or 'General Concept'

    if compact:
        truncated_why = smart_truncate_html(why, 300)
        spoiler_content = (
            f"🎯 <b>CORRECT OPTION: [{correct_letter}]</b>\n\n"
            f"📝 <b>SOLUTION SUMMARY:</b>\n"
            f"<blockquote expandable>"
            f"🏛️ <b>Principle:</b> {beautify_markdown_math(rule_text)}\n\n"
            f"<b>Explanation:</b> {beautify_markdown_math(truncated_why)}"
            f"</blockquote>\n"
        )
        footer_note = (
            "\n<hr/>\n"
            "📖 <i>The complete step-by-step derivation has been posted in the message below.</i>"
        )
    else:
        explanation_part = (
            f"📝 <b>DETAILED SOLUTION:</b>\n"
            f"<b>Principle:</b>\n"
            f"<blockquote expandable>"
            f"🏛️ <b>{beautify_markdown_math(rule_text)}</b>"
            f"</blockquote>\n\n"
            f"<b>Step-by-Step Derivation:</b>\n"
            f"{beautify_markdown_math(why)}\n\n"
        )
        if exp.get('analogy'):
            explanation_part += f"💡 <b>Analogy:</b>\n{beautify_markdown_math(exp['analogy'])}\n\n"
        if exp.get('memory_tip'):
            explanation_part += f"🧠 <b>Memory Tip:</b>\n{beautify_markdown_math(exp['memory_tip'])}\n\n"

        analysis_list = ["🔍 <b>OPTION-BY-OPTION BREAKDOWN:</b>"]
        options_analysis = q.get('options_analysis', [])
        for i, o_text in enumerate(q['options']):
            let = letters[i] if i < len(letters) else str(i+1)
            is_opt_correct = (i == correct_idx)
            status_icon = "🟢" if is_opt_correct else "⚪"
            verdict_badge = "✅ <b>Correct.</b>" if is_opt_correct else "❌ <i>Incorrect.</i>"

            why_text = ""
            example_text = ""
            if i < len(options_analysis):
                why_text = options_analysis[i].get('why', '')
                example_text = options_analysis[i].get('example', '')

            cleaned_opt_val = lite_math(o_text)
            cleaned_why = beautify_markdown_math(why_text)

            analysis_line = f"• {status_icon} <b>Option {let} ({cleaned_opt_val}):</b> {verdict_badge} {cleaned_why}"
            if example_text:
                cleaned_example = beautify_markdown_math(example_text)
                analysis_line += f" (<i>e.g., {cleaned_example}</i>)"
            analysis_list.append(analysis_line)

        analysis_str = "\n".join(analysis_list)

        spoiler_content = f"🎯 <b>CORRECT OPTION: [{correct_letter}]</b>\n\n{explanation_part}\n{analysis_str}"
        footer_note = ""

    hashtag_list = [sanitize_tag_to_hashtag(t) for t in q.get('tags', [])]
    footer = (
        f"\n\n<hr/>\n"
        f"📢 <b>Channel:</b> <a href='https://t.me/grade12EntranceExam'>@grade12EntranceExam</a>\n"
        f"{' '.join(hashtag_list)}{footer_note}"
    )

    return f"{header}{body}{opts_block}<hr/>\n🎯 <b>TAP TO REVEAL KEY ANSWER & SOLUTION:</b>\n<tg-spoiler>{spoiler_content}</tg-spoiler>{footer}"

def build_answered_view(q, display_id: str, user_idx: int, compact=False, perf_card=None, continuation=False) -> str:
    correct_idx = q['correct_option']
    letters = ["A", "B", "C", "D", "E"]
    user_letter = letters[user_idx] if user_idx < len(letters) else "?"
    is_correct_user = (user_idx == correct_idx)
    user_status = "🟩 CORRECT" if is_correct_user else "🟥 INCORRECT"
    correct_letter = letters[correct_idx]

    # 1. Continuation Layout
    if continuation:
        exp = q.get("poll_explanation", {})
        why = exp.get('why', 'No detailed solution available.')
        rule_text = exp.get('governing_principle') or exp.get('rule') or 'General Concept'
        
        explanation_part = (
            f"📝 <b>DETAILED SOLUTION:</b>\n"
            f"<b>Principle:</b>\n"
            f"<blockquote expandable>"
            f"🏛️ <b>{beautify_markdown_math(rule_text)}</b>"
            f"</blockquote>\n\n"
            f"<b>Step-by-Step Derivation:</b>\n"
            f"{beautify_markdown_math(why)}\n"
        )
        if exp.get('analogy'):
            explanation_part += f"\n💡 <b>Analogy:</b>\n{beautify_markdown_math(exp['analogy'])}\n"
        if exp.get('memory_tip'):
            explanation_part += f"\n🧠 <b>Memory Tip:</b>\n{beautify_markdown_math(exp['memory_tip'])}\n"

        analysis_list = ["🔍 <b>OPTION-BY-OPTION BREAKDOWN:</b>"]
        options_analysis = q.get('options_analysis', [])
        for i, o_text in enumerate(q.get('options', [])):
            let = letters[i] if i < len(letters) else str(i+1)
            is_opt_correct = (i == correct_idx)
            status_icon = "🟢" if is_opt_correct else "⚪"
            verdict_badge = "✅ <b>Correct.</b>" if is_opt_correct else "❌ <i>Incorrect.</i>"

            why_text = ""
            example_text = ""
            if i < len(options_analysis):
                why_text = options_analysis[i].get('why', '')
                example_text = options_analysis[i].get('example', '')

            cleaned_opt_val = lite_math(o_text)
            cleaned_why = beautify_markdown_math(why_text)

            analysis_line = f"• {status_icon} <b>Option {let} ({cleaned_opt_val}):</b> {verdict_badge} {cleaned_why}"
            if example_text:
                cleaned_example = beautify_markdown_math(example_text)
                analysis_line += f" (<i>e.g., {cleaned_example}</i>)"
            analysis_list.append(analysis_line)

        analysis_str = "\n".join(analysis_list)

        connection_header = (
            f"📖 <b>DETAILED DERIVATION (CONTINUATION)</b> • REF <code>{display_id}</code>\n"
            f"<hr/>\n"
        )
        return f"{connection_header}{explanation_part}\n{analysis_str}"

    # 2. Main Layout with Sleek Header
    day_str = get_day_from_tags(q.get('tags', []))
    subject_str = q.get('subject', '').upper()
    topic_str = q.get('topic', 'General')

    header = (
        f"🎓 <b>{subject_str}</b> • REF <code>{display_id}</code>\n"
        f"📐 <b>{topic_str}</b> • 📅 {day_str}\n"
        f"<hr/>\n\n"
    )

    body = (
        f"<blockquote>"
        f"<b>PROBLEM PROPOSITION</b>\n"
        f"{beautify_markdown_math(q['question'])}"
        f"</blockquote>\n\n"
    )

    opts_list = ["📋 <b>OPTIONS:</b>"]
    for i, o in enumerate(q['options']):
        let = letters[i] if i < len(letters) else str(i+1)
        clean_opt = beautify_markdown_math(o)
        opts_list.append(f"• <b>{let})</b> {clean_opt}")
    opts_block = "\n".join(opts_list) + "\n\n"

    status_block = (
        f"<hr/>\n"
        f"🎯 <b>Your Selection:</b> {user_letter} ({user_status})\n"
        f"⭐ <b>Correct Option:</b> <b>[{correct_letter}]</b>\n\n"
    )

    exp = q.get("poll_explanation", {})
    why = exp.get('why', 'No detailed solution available.')
    rule_text = exp.get('governing_principle') or exp.get('rule') or 'General Concept'

    if compact:
        truncated_why = smart_truncate_html(why, 300)
        explanation_block = (
            f"📝 <b>DETAILED SOLUTION:</b>\n"
            f"<b>Principle:</b>\n"
            f"<blockquote expandable>"
            f"🏛️ <b>{beautify_markdown_math(rule_text)}</b>\n\n"
            f"<b>Explanation Summary:</b>\n"
            f"{beautify_markdown_math(truncated_why)}"
            f"</blockquote>\n"
        )
        analysis_block = ""
        footer_note = (
            "\n<hr/>\n"
            "📖 <i>The complete step-by-step derivation has been posted in the message below.</i>"
        )
    else:
        explanation_block = (
            f"📝 <b>DETAILED SOLUTION:</b>\n"
            f"<b>Principle:</b>\n"
            f"<blockquote expandable>"
            f"🏛️ <b>{beautify_markdown_math(rule_text)}</b>"
            f"</blockquote>\n\n"
            f"<b>Step-by-Step Derivation:</b>\n"
            f"{beautify_markdown_math(why)}\n\n"
        )
        if exp.get('analogy'):
            explanation_block += f"💡 <b>Analogy:</b>\n{beautify_markdown_math(exp['analogy'])}\n\n"
        if exp.get('memory_tip'):
            explanation_block += f"🧠 <b>Memory Tip:</b>\n{beautify_markdown_math(exp['memory_tip'])}\n\n"

        analysis_list = ["🔍 <b>OPTION-BY-OPTION BREAKDOWN:</b>"]
        options_analysis = q.get('options_analysis', [])
        for i, o_text in enumerate(q['options']):
            let = letters[i] if i < len(letters) else str(i+1)
            is_opt_correct = (i == correct_idx)
            status_icon = "🟢" if is_opt_correct else "⚪"
            verdict_badge = "✅ <b>Correct.</b>" if is_opt_correct else "❌ <i>Incorrect.</i>"

            why_text = ""
            example_text = ""
            if i < len(options_analysis):
                why_text = options_analysis[i].get('why', '')
                example_text = options_analysis[i].get('example', '')

            cleaned_opt_val = lite_math(o_text)
            cleaned_why = beautify_markdown_math(why_text)

            analysis_line = f"• {status_icon} <b>Option {let} ({cleaned_opt_val}):</b> {verdict_badge} {cleaned_why}"
            if example_text:
                cleaned_example = beautify_markdown_math(example_text)
                analysis_line += f" (<i>e.g., {cleaned_example}</i>)"
            analysis_list.append(analysis_line)

        analysis_block = "\n".join(analysis_list) + "\n"
        footer_note = ""

    score_segment = ""
    if perf_card:
        if not perf_card.get('first_try', True):
            marks_notice = "⚠️ <i>Practice Mode: Answer modified. No marks awarded.</i>"
        elif perf_card.get('is_bonus_winner'):
            marks_notice = "⚡ <b>EARLY BIRD BONUS!</b> You solved this first! <b>(+10 Marks)</b>"
        elif perf_card.get('marks_awarded', 0) > 0:
            marks_notice = "✅ <b>CORRECT ANSWER!</b> Standard score awarded. <b>(+2 Marks)</b>"
        else:
            marks_notice = "❌ <b>INCORRECT ANSWER.</b> No marks awarded. <b>(+0 Marks)</b>"

        mastery = get_grade_mastery_title(perf_card.get('total_marks', 0))
        next_rank_info = get_next_rank_info(perf_card.get('total_marks', 0))

        score_segment = (
            f"<hr/>\n"
            f"<h3>📊 STUDY PERFORMANCE CARD</h3>\n"
            f"{marks_notice}\n\n"
            f"• 🎒 <b>Academic Level:</b> Grade {perf_card.get('grade', 12)}\n"
            f"• 📝 <b>Practice Score:</b> {perf_card.get('total_marks', 0)} Marks\n"
            f"• 🏆 <b>Mastery Level:</b> {mastery}\n"
            f"• 🎯 <b>Accuracy Rate:</b> {perf_card.get('accuracy', 0)}% ({perf_card.get('correct', 0)} of {perf_card.get('total', 0)} solved correctly)\n\n"
            f"💡 <b>Next Target:</b> <i>{next_rank_info}</i>\n"
        )

    hashtag_list = [sanitize_tag_to_hashtag(t) for t in q.get('tags', [])]
    footer = (
        f"\n\n<hr/>\n"
        f"📢 <b>Channel:</b> <a href='https://t.me/grade12EntranceExam'>@grade12EntranceExam</a>\n"
        f"{' '.join(hashtag_list)}{footer_note}"
    )

    return f"{header}{body}{opts_block}{status_block}{explanation_block}{analysis_block}{score_segment}{footer}"

def build_keyboard(q, display_id: str) -> InlineKeyboardMarkup:
    """Creates inline keyboard button rows that always include the letter and the parsed option text."""
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
    """Creates callback buttons that always include the letter and the parsed option text."""
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
        if len(combined) <= 195: return combined
    for sentence in re.split(r'(?<=[.!?])\s+', clean_why):
        if len(sentence) <= 195 and any(sym in sentence for sym in ["=", "√", "∫", "π", "θ", "°"]):
            return sentence
    return f"Apply {clean_rule[:100]}."[:195] if clean_rule else "Check Premium UI for derivations."[:195]