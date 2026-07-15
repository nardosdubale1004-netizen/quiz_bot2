from src.rendering.kroki_client import fetch_kroki_image, get_latex_url
from src.rendering.latex_templates import (
    escape_latex,
    build_figure_block,
    assemble_layout,
    build_widescreen_solution_latex,
    sanitize_tag_to_hashtag,
    create_explanation_assets
)
from src.rendering.html_views import (
    build_closed_static_view,
    build_answered_view,
    build_keyboard,
    replace_code_with_italic,
    smart_truncate_html,
    generate_poll_hint,
    get_grade_mastery_title
)

class UIFactory:
    escape_latex = staticmethod(escape_latex)
    build_figure_block = staticmethod(build_figure_block)
    assemble_layout = staticmethod(assemble_layout)
    build_widescreen_solution_latex = staticmethod(build_widescreen_solution_latex)
    sanitize_tag_to_hashtag = staticmethod(sanitize_tag_to_hashtag)
    generate_poll_hint = staticmethod(generate_poll_hint)
    build_closed_static_view = staticmethod(build_closed_static_view)
    build_answered_view = staticmethod(build_answered_view)
    build_keyboard = staticmethod(build_keyboard)
    replace_code_with_italic = staticmethod(replace_code_with_italic)
    smart_truncate_html = staticmethod(smart_truncate_html)
    create_explanation_assets = staticmethod(create_explanation_assets)
    get_latex_url = staticmethod(get_latex_url)

    @staticmethod
    def is_complex(text):
        if not text: return False
        triggers = [r"\begin", r"\frac", r"\int", r"\sum", r"\vec", r"\addplot", r"\\", r"\matrix", r"\cases", r"\sqrt{"]
        return any(t in str(text) for t in triggers)

    @classmethod
    def create_question_assets(cls, q, display_id):
        is_q_complex = cls.is_complex(q.get('question', ''))
        is_o_complex = any(cls.is_complex(o) for o in q.get('options', []))
        has_tikz = bool(q.get("latex"))
        question_block = cls.build_question_text_block(q, display_id) if (is_q_complex or has_tikz) else None
        figure_block = cls.build_figure_block(q, add_strut=True) if has_tikz else None
        options_block = cls.build_options_block(q) if is_o_complex else None

        from src.typography import lite_math
        import html
        caption_q = f"<b>{html.escape(lite_math(q['question']))}</b>"
        
        from src.rendering.latex_templates import get_day_from_tags
        day_str = get_day_from_tags(q.get('tags', []))
        day_part = f" | 📅 <b>{day_str}</b>" if day_str else ""
        header = (f"📚 <b>{q.get('subject','').upper()} SHEET</b> | REF: <code>{display_id}</code>\n"
                  f"🔖 <b>Topic:</b> {q.get('topic','General')}{day_part}\n"
                  f"📢 <b>Channel:</b> <a href='https://t.me/grade12EntranceExam'>@grade12EntranceExam</a>\n"
                  f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n")
        
        hashtag_list = [cls.sanitize_tag_to_hashtag(t) for t in q.get('tags', [])]
        final_caption = f"{header}{caption_q}\n\n{' '.join(hashtag_list)}"
        img_url = cls.get_latex_url(cls.assemble_layout(question_block, figure_block, options_block)) if (question_block or figure_block or options_block) else None
        return img_url, final_caption, None

    @classmethod
    def build_question_text_block(cls, q, display_id):
        from src.rendering.latex_templates import get_day_from_tags
        subject = cls.escape_latex(q.get('subject', '').upper())
        topic = cls.escape_latex(q.get('topic', 'General'))
        day_str = get_day_from_tags(q.get('tags', []))
        safe_q = cls.escape_latex(q['question'])
        meta_elements = [f"Subject: {subject}", f"Topic: {topic}"]
        if day_str: meta_elements.append(day_str)
        meta_line = " \\quad $\\bullet$ \\quad ".join(meta_elements)
        return (
            f"{{\\noindent \\large \\textbf{{QUESTION SHEET: REF {display_id}}}}} \\par\n"
            f"\\vspace{{0.3em}}\n"
            f"{{\\noindent \\small \\color{{gray}} {meta_line}}} \\par\n"
            f"\\vspace{{0.8em}}\n"
            f"\\noindent\\hrulefill \\par\n"
            f"\\vspace{{1.1em}}\n"
            f"{{\\noindent {safe_q}}} \\par"
        )

    @classmethod
    def build_options_block(cls, q):
        opts_latex = "\\textbf{Options:} \\par \\vspace{0.5em} \n\\begin{enumerate}[label=\\bfseries\\Alph*), leftmargin=3em]\n"
        for o in q['options']:
            opts_latex += f"\\item {cls.escape_latex(o)}\n"
        opts_latex += "\\end{enumerate}"
        return opts_latex