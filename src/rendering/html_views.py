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
    """Classifies user proficiency level into realistic study ranks."""
    if marks == 0: return "🌱 Candidate (Practice Mode)"
    if marks < 50: return "🛡️ Bronze Scholar"
    if marks < 150: return "⚔️ Silver Elite"
    if marks < 500: return "👑 Gold Master"
    if marks < 1200: return "💎 Platinum Grandmaster"
    return "🌌 Legend"

def get_next_rank_info(marks: int) -> str:
    """Calculates how many marks are needed for the next rank level."""
    if marks == 0: return "Solve 1 question to unlock <b>Bronze Scholar</b> rank!"
    if marks < 50: return f"Earn <b>{50 - marks} Marks</b> to unlock <b>Silver Elite</b>"
    if marks < 150: return f"Earn <b>{150 - marks} Marks</b> to unlock <b>Gold Master</b>"
    if marks < 500: return f"Earn <b>{500 - marks} Marks</b> to unlock <b>Platinum Grandmaster</b>"
    if marks < 1200: return f"Earn <b>{1200 - marks} Marks</b> to unlock <b>Legend</b>"
    return "Maximum Mastery Level Reached! 🌌"

def indent_text(text: str, spaces: int = 6) -> str:
    """Aligns subsequent lines with strict nested indentation to match the parent block."""
    if not text:
        return ""
    indent = " " * spaces
    return "\n".join(indent + line if line.strip() else "" for line in text.split("\n"))

def build_closed_static_view(q, display_id: str, compact=False, continuation=False) -> str:
    """Generates the final plain-text static fallback view for closed quizzes."""
    correct_letter = chr(65 + q['correct_option'])
    
    # 1. Threaded Continuation Layout
    if continuation:
        exp = q.get("poll_explanation", {})
        why = exp.get('why', 'N/A')
        rule_text = exp.get('governing_principle') or exp.get('rule') or 'General Concept'
        
        explanation_part = (f"📝 <b>DETAILED SOLUTION:</b>\n"
                            f"   ▪️ <b>Principle:</b>\n{indent_text(beautify_markdown_math(rule_text))}\n\n"
                            f"   ▪️ <b>Explanation:</b>\n{indent_text(beautify_markdown_math(why))}\n")
        if exp.get('analogy'): explanation_part += f"   ▪️ <b>Analogy:</b>\n{indent_text(beautify_markdown_math(exp['analogy']))}\n\n"
        if exp.get('memory_tip'): explanation_part += f"   ▪️ <b>Memory Tip:</b>\n{indent_text(beautify_markdown_math(exp['memory_tip']))}\n"
        
        analysis_list = []
        options_analysis = q.get('options_analysis', [])
        for i, o_text in enumerate(q['options']):
            letter = chr(65 + i)
            analysis_line = f"   {'✅' if letter == correct_letter else '❌'} <b>{letter}:</b> {beautify_markdown_math(options_analysis[i].get('why', '')) if i < len(options_analysis) else ''}"
            if i < len(options_analysis) and options_analysis[i].get('example'):
                analysis_line += f" (<i>e.g., {beautify_markdown_math(options_analysis[i]['example'])}</i>)"
            analysis_list.append(analysis_line)
        
        analysis_str = "\n".join(analysis_list)
        analysis_block = "\n🔍 <b>OPTION BREAKDOWN:</b>\n" + analysis_str
        
        spoiler_content = f"🎯 <b>CORRECT OPTION: [{correct_letter}]</b>\n\n{explanation_part}{analysis_block}"
        spoiler_content = replace_code_with_italic(spoiler_content)
        
        connection_header = (
            f"📖 <b>DETAILED SOLUTION (CONTINUATION)</b> | REF: <code>{display_id}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        )
        return f"{connection_header}🎯 <b>REVEAL SOLUTION DETAILS:</b>\n<tg-spoiler>{spoiler_content}</tg-spoiler>"

    # 2. Base layouts
    day_str = get_day_from_tags(q.get('tags', []))
    day_part = f" | 📅 <b>{day_str}</b>" if day_str else ""
    header = (f"📚 <b>{q.get('subject','').upper()} STUDY SHEET</b> | REF: <code>{display_id}</code>\n"
              f"🔖 <b>Topic:</b> {q.get('topic','General')}{day_part}\n"
              f"📢 <b>Channel:</b> <a href='https://t.me/grade12EntranceExam'>@grade12EntranceExam</a>\n"
              f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n")
    body = f"{beautify_markdown_math(q['question'])}\n\n"
    opts_list = [f"   <b>{chr(65+i)})</b> {beautify_markdown_math(o)}" for i, o in enumerate(q['options'])]
    opts_block = "📋 <b>OPTIONS:</b>\n" + "\n".join(opts_list) + "\n\n"
    exp = q.get("poll_explanation", {})
    why = exp.get('why', 'N/A')
    rule_text = exp.get('governing_principle') or exp.get('rule') or 'General Concept'

    if compact:
        truncated_why = smart_truncate_html(why, 300)
        spoiler_content = (
            f"🎯 <b>CORRECT OPTION: [{correct_letter}]</b>\n\n"
            f"📝 <b>SOLUTION SUMMARY:</b>\n"
            f"   ▪️ <b>Principle:</b>\n{indent_text(beautify_markdown_math(rule_text))}\n\n"
            f"   ▪️ <b>Explanation:</b>\n{indent_text(beautify_markdown_math(truncated_why))}"
        )
        footer_note = (
            "\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📖 <i>The complete step-by-step derivation has been posted in the message below.</i>"
        )
    else:
        explanation_part = (f"📝 <b>DETAILED SOLUTION:</b>\n"
                            f"   ▪️ <b>Principle:</b>\n{indent_text(beautify_markdown_math(rule_text))}\n\n"
                            f"   ▪️ <b>Explanation:</b>\n{indent_text(beautify_markdown_math(why))}\n")
        if exp.get('analogy'): explanation_part += f"   ▪️ <b>Analogy:</b>\n{indent_text(beautify_markdown_math(exp['analogy']))}\n\n"
        if exp.get('memory_tip'): explanation_part += f"   ▪️ <b>Memory Tip:</b>\n{indent_text(beautify_markdown_math(exp['memory_tip']))}\n"
        analysis_list = []
        options_analysis = q.get('options_analysis', [])
        for i, o_text in enumerate(q['options']):
            letter = chr(65 + i)
            analysis_line = f"   {'✅' if letter == correct_letter else '❌'} <b>{letter}:</b> {beautify_markdown_math(options_analysis[i].get('why', '')) if i < len(options_analysis) else ''}"
            if i < len(options_analysis) and options_analysis[i].get('example'):
                analysis_line += f" (<i>e.g., {beautify_markdown_math(options_analysis[i]['example'])}</i>)"
            analysis_list.append(analysis_line)
        
        analysis_str = "\n".join(analysis_list)
        spoiler_content = f"🎯 <b>CORRECT OPTION: [{correct_letter}]</b>\n\n{explanation_part}🔍 <b>OPTION BREAKDOWN:</b>\n{analysis_str}"
        footer_note = ""

    spoiler_content = replace_code_with_italic(spoiler_content)
    full_text = f"{header}{body}{opts_block}━━━━━━━━━━━━━━━━━━━━━━━━\n🎯 <b>TAP TO REVEAL KEY ANSWER & SOLUTION:</b>\n<tg-spoiler>{spoiler_content}</tg-spoiler>{footer_note}"

    if compact and len(full_text) > 1022:
        final_why = smart_truncate_html(truncated_why, max(50, len(truncated_why) - (len(full_text) - 1010)))
        spoiler_content = replace_code_with_italic(f"🎯 <b>CORRECT OPTION: [{correct_letter}]</b>\n\n📝 <b>SOLUTION SUMMARY:</b>\n{indent_text(beautify_markdown_math(final_why))}")
        full_text = f"{header}{body}{opts_block}━━━━━━━━━━━━━━━━━━━━━━━━\n🎯 <b>TAP TO REVEAL KEY ANSWER & SOLUTION:</b>\n<tg-spoiler>{spoiler_content}</tg-spoiler>{footer_note}"
    return full_text

def build_answered_view(q, display_id: str, user_idx: int, compact=False, perf_card=None, continuation=False) -> str:
    """Generates a complete unified view of the question, options, user result, and detailed solutions."""
    correct_idx = q['correct_option']
    letters = ["A", "B", "C", "D", "E"]
    user_letter = letters[user_idx] if user_idx < len(letters) else "?"
    user_status = "🟩 CORRECT" if user_idx == correct_idx else "🟥 INCORRECT"
    correct_letter = letters[correct_idx]

    # 1. Threaded Continuation Layout
    if continuation:
        exp = q.get("poll_explanation", {})
        why = exp.get('why', 'N/A')
        rule_text = exp.get('governing_principle') or exp.get('rule') or 'General Concept'
        explanation_part = (f"📝 <b>DETAILED SOLUTION:</b>\n"
                            f"   ▪️ <b>Principle:</b>\n{indent_text(beautify_markdown_math(rule_text))}\n\n"
                            f"   ▪️ <b>Explanation:</b>\n{indent_text(beautify_markdown_math(why))}\n")
        if exp.get('analogy'): explanation_part += f"   ▪️ <b>Analogy:</b>\n{indent_text(beautify_markdown_math(exp['analogy']))}\n\n"
        if exp.get('memory_tip'): explanation_part += f"   ▪️ <b>Memory Tip:</b>\n{indent_text(beautify_markdown_math(exp['memory_tip']))}\n"
        explanation_part += "\n"

        analysis_list = []
        options_analysis = q.get('options_analysis', [])
        for i, o_text in enumerate(q['options']):
            let = chr(65 + i)
            is_correct = (let == correct_letter)
            status_icon = "🟢" if is_correct else "⚪"

            why_text = ""
            example_text = ""
            if i < len(options_analysis):
                why_text = options_analysis[i].get('why', '')
                example_text = options_analysis[i].get('example', '')

            analysis_line = f"   {status_icon} <b>{let}:</b> {beautify_markdown_math(why_text)}"
            if example_text:
                analysis_line += f" (<i>e.g., {beautify_markdown_math(example_text)}</i>)"
            analysis_list.append(analysis_line)
        
        analysis_str = "\n".join(analysis_list)
        analysis_block = "🔍 <b>OPTION BREAKDOWN:</b>\n" + analysis_str + "\n"

        explanation_part = replace_code_with_italic(explanation_part)
        analysis_block = replace_code_with_italic(analysis_block)

        connection_header = (
            f"📖 <b>DETAILED DERIVATION (CONTINUATION)</b> | REF: <code>{display_id}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        )
        return f"{connection_header}{explanation_part}{analysis_block}"

    # 2. Base layouts
    day_str = get_day_from_tags(q.get('tags', []))
    day_part = f" | 📅 <b>{day_str}</b>" if day_str else ""
    header = (f"📚 <b>{q.get('subject','').upper()} STUDY SHEET</b> | REF: <code>{display_id}</code>\n"
              f"🔖 <b>Topic:</b> {q.get('topic','General')}{day_part}\n"
              f"📢 <b>Channel:</b> <a href='https://t.me/grade12EntranceExam'>@grade12EntranceExam</a>\n"
              f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n")
    body = f"{beautify_markdown_math(q['question'])}\n\n"
    opts_list = [f"   <b>{chr(65+i)})</b> {beautify_markdown_math(o)}" for i, o in enumerate(q['options'])]
    opts_block = "📋 <b>OPTIONS:</b>\n" + "\n".join(opts_list) + "\n\n"

    status_block = f"━━━━━━━━━━━━━━━━━━━━━━━━\n🎯 <b>Your Selection:</b> {user_letter} ({user_status})\n⭐ <b>Correct Option:</b> <b>[{correct_letter}]</b>\n\n"
    exp = q.get("poll_explanation", {})
    why = exp.get('why', 'N/A')
    rule_text = exp.get('governing_principle') or exp.get('rule') or 'General Concept'

    if compact:
        truncated_why = smart_truncate_html(why, 300)
        explanation_block = (
            f"📝 <b>DETAILED SOLUTION:</b>\n"
            f"   ▪️ <b>Principle:</b>\n{indent_text(beautify_markdown_math(rule_text))}\n\n"
            f"   ▪️ <b>Explanation:</b>\n{indent_text(beautify_markdown_math(truncated_why))}\n"
        )
        analysis_block = ""
        footer_note = (
            "\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📖 <i>The complete step-by-step derivation has been posted in the message below.</i>"
        )
    else:
        explanation_block = (
            f"📝 <b>DETAILED SOLUTION:</b>\n"
            f"   ▪️ <b>Principle:</b>\n{indent_text(beautify_markdown_math(rule_text))}\n\n"
            f"   ▪️ <b>Explanation:</b>\n{indent_text(beautify_markdown_math(why))}\n"
        )
        if exp.get('analogy'): explanation_block += f"   ▪️ <b>Analogy:</b>\n{indent_text(beautify_markdown_math(exp['analogy']))}\n\n"
        if exp.get('memory_tip'): explanation_block += f"   ▪️ <b>Memory Tip:</b>\n{indent_text(beautify_markdown_math(exp['memory_tip']))}\n"
        explanation_block += "\n"
        analysis_list = []
        options_analysis = q.get('options_analysis', [])
        for i, o_text in enumerate(q['options']):
            let = chr(65 + i)
            is_correct = (let == correct_letter)
            status_icon = "🟢" if is_correct else "⚪"

            why_text = ""
            example_text = ""
            if i < len(options_analysis):
                why_text = options_analysis[i].get('why', '')
                example_text = options_analysis[i].get('example', '')

            analysis_line = f"   {status_icon} <b>{let}:</b> {beautify_markdown_math(why_text)}"
            if example_text:
                analysis_line += f" (<i>e.g., {beautify_markdown_math(example_text)}</i>)"
            analysis_list.append(analysis_line)
        
        analysis_str = "\n".join(analysis_list)
        analysis_block = "🔍 <b>OPTION BREAKDOWN:</b>\n" + analysis_str + "\n"
        footer_note = ""

    explanation_block = replace_code_with_italic(explanation_block)
    analysis_block = replace_code_with_italic(analysis_block)

    # 4. Compile dynamic performance scorecard if analytics are loaded
    score_segment = ""
    if perf_card:
        if not perf_card['first_try']:
            marks_notice = "⚠️ <i>Practice Mode: Answer modified. No marks awarded.</i>\n"
        elif perf_card['is_bonus_winner']:
            marks_notice = "⚡ <b>EARLY BIRD BONUS!</b> You solved this first! <b>(+10 Marks)</b>\n"
        elif perf_card['marks_awarded'] > 0:
            marks_notice = "✅ <b>CORRECT!</b> Standard score awarded. <b>(+2 Marks)</b>\n"
        else:
            marks_notice = "❌ <b>INCORRECT.</b> No marks awarded. <b>(+0 Marks)</b>\n"

        mastery = get_grade_mastery_title(perf_card['total_marks'])
        next_rank_info = get_next_rank_info(perf_card['total_marks'])
        
        score_segment = (
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>STUDY PERFORMANCE CARD:</b>\n"
            f"{marks_notice}\n"
            f"├─ Level: <b>Grade {perf_card.get('grade', 12)}</b>\n"
            f"├─ Cumulative Score: <b>{perf_card['total_marks']} Marks</b>\n"
            f"├─ Mastery Rank: <b>{mastery}</b>\n"
            f"├─ Accuracy: <b>{perf_card['accuracy']}%</b> ({perf_card['correct']}/{perf_card['total']} correct)\n"
            f"└─ Target: {next_rank_info}\n"
        )

    return f"{header}{body}{opts_block}{status_block}{explanation_block}{analysis_block}{score_segment}{footer_note}"

def build_keyboard(q, display_id: str) -> InlineKeyboardMarkup:
    from src.rendering import UIFactory
    letters = ["𝗔", "𝗕", "𝗖", "𝗗", "𝗘"]
    is_o_complex = any(is_complex(o) for o in q['options'])
    
    bot_user = CONFIG.get("bot_username", "EthiopiaEntranceExamBot")
    buttons = []
    for i, opt in enumerate(q['options']):
        label = letters[i] if is_o_complex else f"{letters[i]} │ {lite_math(opt)}"
        url = f"https://t.me/{bot_user}?start=ans_{display_id}_{i}"
        buttons.append([InlineKeyboardButton(label, url=url)])
        
    return InlineKeyboardMarkup(buttons)

def build_interactive_keyboard(q, display_id: str) -> InlineKeyboardMarkup:
    """Creates the interactive options keyboard (callback buttons) sent only inside private PM chats."""
    from src.rendering import UIFactory
    letters = ["𝗔", "𝗕", "𝗖", "𝗗", "𝗘"]
    is_o_complex = any(is_complex(o) for o in q['options'])
    buttons = [[InlineKeyboardButton(letters[i] if is_o_complex else f"{letters[i]} │ {lite_math(opt)}", callback_data=f"ans|{display_id}|{i}")] for i, opt in enumerate(q['options'])]
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