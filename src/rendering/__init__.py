# src/rendering/__init__.py
from src.rendering.kroki_client import fetch_kroki_image, get_latex_url
from src.rendering.latex_templates import (
    escape_latex,
    build_figure_block,
    assemble_layout,
    build_widescreen_solution_latex,
    sanitize_tag_to_hashtag,
    create_explanation_assets,
    is_complex,
    has_real_diagram
)
from src.rendering.html_views import (
    build_closed_static_view,
    build_answered_view,
    build_keyboard,
    replace_code_with_italic,
    smart_truncate_html,
    generate_poll_hint,
    get_grade_mastery_title,
    build_interactive_keyboard
)

class UIFactory:
    WATERMARK = "@grade12EntranceExam"
    escape_latex = staticmethod(escape_latex)
    build_figure_block = staticmethod(build_figure_block)
    assemble_layout = staticmethod(assemble_layout)
    build_widescreen_solution_latex = staticmethod(build_widescreen_solution_latex)
    sanitize_tag_to_hashtag = staticmethod(sanitize_tag_to_hashtag)
    generate_poll_hint = staticmethod(generate_poll_hint)
    build_closed_static_view = staticmethod(build_closed_static_view)
    build_answered_view = staticmethod(build_answered_view)
    build_keyboard = staticmethod(build_keyboard)
    build_interactive_keyboard = staticmethod(build_interactive_keyboard)
    replace_code_with_italic = staticmethod(replace_code_with_italic)
    smart_truncate_html = staticmethod(smart_truncate_html)
    create_explanation_assets = staticmethod(create_explanation_assets)
    get_latex_url = staticmethod(get_latex_url)
    is_complex = staticmethod(is_complex)
    has_real_diagram = staticmethod(has_real_diagram)

    @classmethod
    def create_question_assets(cls, q, display_id):
        # Image layouts are ONLY compiled if the question contains an active diagram in its latex field
        has_tikz = cls.has_real_diagram(q)

        if has_tikz:
            question_block = cls.build_question_text_block(q, display_id)
            figure_block = cls.build_figure_block(q, add_strut=True)
            options_block = cls.build_options_block(q)
            img_url = cls.get_latex_url(cls.assemble_layout(cls.WATERMARK, question_block, figure_block, options_block))
        else:
            img_url = None

        from src.typography import beautify_markdown_math
        caption_q = f"<b>{beautify_markdown_math(q['question'])}</b>"

        from src.rendering.latex_templates import get_day_from_tags
        day_str = get_day_from_tags(q.get('tags', []))
        day_part = f" | 📅 <b>{day_str}</b>" if day_str else ""
        header = (f"📚 <b>{q.get('subject','').upper()} SHEET</b> | REF: <code>{display_id}</code> | 🔖 <b>Topic:</b> {q.get('topic','General')}{day_part} | 📢 <b>Channel:</b> <a href='https://t.me/grade12EntranceExam'>@grade12EntranceExam</a>\n━━━━━━━━━━━━━━━━━━━━━━━━\n")

        hashtag_list = [cls.sanitize_tag_to_hashtag(t) for t in q.get('tags', [])]
        final_caption = f"{header}{caption_q}\n\n{' '.join(hashtag_list)}"
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