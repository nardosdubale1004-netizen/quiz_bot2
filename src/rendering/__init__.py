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
from src.config import CONFIG

class UIFactoryMeta(type):
    @property
    def WATERMARK(cls):
        # Dynamically pulls from environment or config.json
        return CONFIG.get("channel") or "@QuizOva"

class UIFactory(metaclass=UIFactoryMeta):
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
        caption_q = (
            f"<blockquote>"
            f"<b>PROBLEM PROPOSITION</b>\n"
            f"{beautify_markdown_math(q['question'])}"
            f"</blockquote>"
        )
        if has_tikz:
            caption_q += '\n<p><img src="tg://photo?id=quiz_diagram"/></p>'

        # 🔁 Repeat-question badge. times_shown/first_shown_at ride along on `q` already
        # (refresh_database() SELECT *'s the questions table), so this needs no extra query.
        # times_shown here reflects sends BEFORE this one — >0 means it's been seen before.
        repeat_badge = ""
        times_shown = q.get("times_shown") or 0
        if times_shown > 0:
            repeat_badge = (
                f"\n<blockquote>🔁 <b>Repeat question</b> — good chance for another try.</blockquote>"
            )

        hashtag_list = [cls.sanitize_tag_to_hashtag(t) for t in q.get('tags', [])]
        channel_name = cls.WATERMARK
        channel_username = channel_name.lstrip('@')

        bot_username = CONFIG.get("bot_username")
        dm_link = f" · <a href='https://t.me/{bot_username}?start=view_{display_id}'>💬</a>" if bot_username else ""

        footer = (
            f"\n<hr/>\n"
            f"<b>REF <code>{display_id}</code></b> │ <a href='https://t.me/{channel_username}'>{channel_name}</a>{dm_link}\n"
            f"{' '.join(hashtag_list)}"
        )
        final_caption = f"{caption_q}{repeat_badge}{footer}"
        return img_url, final_caption