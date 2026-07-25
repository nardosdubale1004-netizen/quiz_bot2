# src/rendering/__init__.py
from src.rendering.kroki_client import fetch_kroki_image, get_latex_url
from src.rendering.latex_templates import (
    escape_latex,
    build_figure_block,
    scale_tikz_block,
    assemble_layout,
    assemble_diagram_only_layout,
    build_widescreen_solution_latex,
    sanitize_tag_to_hashtag,
    create_explanation_assets,
    is_complex,
    has_real_diagram,
    has_explanation_diagram
)
from src.rendering.html_views import (
    build_closed_static_view,
    build_answered_view,
    build_keyboard,
    replace_code_with_italic,
    smart_truncate_html,
    generate_poll_hint,
    get_grade_mastery_title,
    build_interactive_keyboard,
    build_answered_keyboard
)

class UIFactory:
    WATERMARK = "@grade12EntranceExam"
    escape_latex = staticmethod(escape_latex)
    build_figure_block = staticmethod(build_figure_block)
    scale_tikz_block = staticmethod(scale_tikz_block)
    assemble_layout = staticmethod(assemble_layout)
    assemble_diagram_only_layout = staticmethod(assemble_diagram_only_layout)
    build_widescreen_solution_latex = staticmethod(build_widescreen_solution_latex)
    sanitize_tag_to_hashtag = staticmethod(sanitize_tag_to_hashtag)
    generate_poll_hint = staticmethod(generate_poll_hint)
    build_closed_static_view = staticmethod(build_closed_static_view)
    build_answered_view = staticmethod(build_answered_view)
    build_keyboard = staticmethod(build_keyboard)
    build_interactive_keyboard = staticmethod(build_interactive_keyboard)
    build_answered_keyboard = staticmethod(build_answered_keyboard)
    replace_code_with_italic = staticmethod(replace_code_with_italic)
    smart_truncate_html = staticmethod(smart_truncate_html)
    create_explanation_assets = staticmethod(create_explanation_assets)
    get_latex_url = staticmethod(get_latex_url)
    is_complex = staticmethod(is_complex)
    has_real_diagram = staticmethod(has_real_diagram)
    has_explanation_diagram = staticmethod(has_explanation_diagram)

    @classmethod
    def create_question_assets(cls, q, display_id):
        has_tikz = cls.has_real_diagram(q)
        figure_block = cls.build_figure_block(q, add_strut=False) if has_tikz else None

        if has_tikz and not figure_block:
            has_tikz = False

        if has_tikz:
            img_url = cls.get_latex_url(cls.assemble_diagram_only_layout(cls.WATERMARK, display_id, figure_block))
        else:
            img_url = None

        from src.typography import beautify_markdown_math
        from src.rendering.latex_templates import get_day_from_tags
        from src.rendering.rich_helpers import convert_to_legacy_html

        day_str = get_day_from_tags(q.get('tags', []))
        subject = beautify_markdown_math(q.get('subject','').upper())
        topic = beautify_markdown_math(q.get('topic','General'))
        
        header = (
            f"🎓 <b>{subject}</b> • REF <code>{display_id}</code>\n"
            f"📐 <b>{topic}</b> • 📅 {day_str}\n"
            f"<hr/>\n"
        )

        hashtag_list = [cls.sanitize_tag_to_hashtag(t) for t in q.get('tags', [])]
        footer = (
            f"\n<hr/>\n"
            f"📢 <b>Channel:</b> <a href='https://t.me/grade12EntranceExam'>@grade12EntranceExam</a>\n"
            f"{' '.join(hashtag_list)}"
        )

        # Truncation logic to protect against the Telegram 1024 caption limit
        raw_question = beautify_markdown_math(q['question'])
        caption_q_template = "\n<blockquote><b>PROBLEM PROPOSITION</b>\n{}</blockquote>\n"
        if has_tikz:
            caption_q_template += '\n<p><img src="tg://photo?id=quiz_diagram"/></p>'

        full_caption_text = f"{header}{caption_q_template.format(raw_question)}{footer}"
        legacy_html_len = len(convert_to_legacy_html(full_caption_text))

        # Check against Telegram 1024-character photo caption limits (allowing safe padding room)
        if legacy_html_len > 1010:
            excess = legacy_html_len - 1010
            allowed_question_len = max(100, len(raw_question) - excess)
            truncated_question = cls.smart_truncate_html(raw_question, allowed_question_len)
            caption_q = caption_q_template.format(truncated_question)
        else:
            caption_q = caption_q_template.format(raw_question)

        final_caption = f"{header}{caption_q}{footer}"
        return img_url, final_caption