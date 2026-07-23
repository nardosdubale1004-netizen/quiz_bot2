# src/rendering/latex_templates.py
import re
from datetime import datetime
from src.typography import UNICODE_TO_LATEX

def is_complex(text):
    """Determines if a string contains advanced mathematical expressions or layout symbols."""
    if not text: return False
    triggers = [r"\begin", r"\frac", r"\int", r"\sum", r"\vec", r"\addplot", r"\\", r"\matrix", r"\cases", r"\sqrt{"]
    return any(t in str(text) for t in triggers)

def has_real_diagram(q) -> bool:
    """
    Determines if a question contains a genuine, non-trivial TikZ/axis drawing diagram.
    This guarantees math formulas use Telegram's native rich text rendering unless a graph is present.
    """
    if q.get("force_image") or q.get("force_latex", False):
        return True

    tikz = q.get("latex")
    if not tikz:
        return False
    tikz_clean = tikz.strip().replace(" ", "").replace("\n", "").replace("\r", "")
    if tikz_clean in ["", "\\begin{tikzpicture}\\end{tikzpicture}", "\\begin{tikzpicture}%\\end{tikzpicture}"]:
        return False

    drawing_triggers = [
        r"\draw", r"\fill", r"\node", r"\addplot", r"\path",
        r"\grid", r"\axis", r"\circle", r"\ellipse", r"\rectangle",
        r"tikzpicture", r"pgfplots"
    ]
    return any(trigger in tikz for trigger in drawing_triggers)

def escape_latex(text: str) -> str:
    if not text:
        return ""
    text = str(text).replace('\xa0', ' ').replace('\u00a0', ' ')
    for char in ['\xad', '\u00ad', '\u200b', '\u200c', '\u200d', '\u2060', '\ufeff']:
        text = text.replace(char, '')
    text = text.replace('\x0b', '\\v')
    text = re.sub(r'\t(heta|imes|an|ilde)', r'\\t\1', text)
    text = re.sub(r'\x07(lpha|pprox|cute)', r'\\a\1', text)
    text = re.sub(r'\x08(eta|egin|ar|old)', r'\\b\1', text)
    text = re.sub(r'\x0c(rac|orall)', r'\\f\1', text)

    parts = text.split('$')
    for i in range(len(parts)):
        if i % 2 == 0:
            for uni, lat in UNICODE_TO_LATEX.items():
                if uni in parts[i]:
                    parts[i] = parts[i].replace(uni, f"${lat}$")
        else:
            for uni, lat in UNICODE_TO_LATEX.items():
                parts[i] = parts[i].replace(uni, lat)
    text = '$'.join(parts).replace("$$", "")
    if text.count('$') % 2 != 0:
        text += '$'
    text = text.replace('\\%', '%').replace('%', '\\%')
    parts = text.split('$')
    for i in range(len(parts)):
        if i % 2 == 0:
            parts[i] = parts[i].replace('\\_', '_').replace('_', '\\_')
            parts[i] = parts[i].replace('\\&', '&').replace('&', '\\&')
            parts[i] = parts[i].replace('\\#', '#').replace('#', '\\#')
        else:
            parts[i] = parts[i].replace('\\%', '%').replace('%', '\\%')
    return '$'.join(parts)

def scale_tikz_block(tikz_code: str, scale_factor: float = 0.75) -> str:
    if not tikz_code:
        return ""
    return f"\\scalebox{{{scale_factor}}}{{\n{tikz_code.strip()}\n}}"

def build_figure_block(q, add_strut=False):
    if not q.get("latex"):
        return None
    tikz = q["latex"].strip()
    tikz = re.sub(r'\n\s*\n', '\n', tikz)

    tikz = re.sub(r'\\makebox\s*(?:\[[^\]]*\])*\s*\{%?\s*(.*?)\s*%?\}', r'\1', tikz, flags=re.DOTALL)
    tikz = re.sub(r'\\mbox\s*\{%?\s*(.*?)\s*%?\}', r'\1', tikz, flags=re.DOTALL)
    tikz = re.sub(r'\\hbox\s*\{%?\s*(.*?)\s*%?\}', r'\1', tikz, flags=re.DOTALL)

    pattern = re.compile(r"(?:\\begin\{center\}\s*)?(?:\\vspace\{[^{}]+\}\s*)?(\\begin\{(?:tikzpicture|axis)\}.*?\\end\{(?:tikzpicture|axis)\})(?:\s*\\vspace\{[^{}]+\})?(?:\s*\\end{{center}})?", re.DOTALL)
    match = pattern.search(tikz)
    if match:
        tikz = match.group(1).strip()
    if "\\begin{tikzpicture}" not in tikz and "\\begin{axis}" not in tikz:
        tikz = "\\begin{tikzpicture}%\n" + tikz + "%\n\\end{tikzpicture}"
    elif "\\begin{axis}" in tikz and "\\begin{tikzpicture}" not in tikz:
        tikz = "\\begin{tikzpicture}%\n" + tikz + "%\n\\end{tikzpicture}"

    tikz = re.sub(
        r'\\path\s*(?:\[.*?\])?\s*\(\[xshift=[^)]+\]current\s+bounding\s+box\.[^)]+\)\s*--\s*\(\[xshift=[^)]+\]current\s+bounding\s+box\.[^)]+\);',
        '',
        tikz,
        flags=re.IGNORECASE
    )

    tikz = tikz.replace("node[above] {$(1,2,2)$}", "node[above left=2pt] {$(1,2,2)$}")
    tikz = tikz.replace("node[above] {(1,2,2)}", "node[above left=2pt] {(1,2,2)}")

    return re.sub(r'\n\s*\n', '\n', tikz)

def assemble_layout(watermark: str, question_block: str, figure_block: str, options_block: str, display_id: str = None) -> str:
    content_width_cm = 15.0
    latex_blocks = []
    if question_block:
        latex_blocks.append(f"\\begin{{minipage}}{{{content_width_cm}cm}}\n{question_block}\n\\end{{minipage}}")
    if figure_block:
        latex_blocks.append(f"\\par\\noindent\\centering\\leavevmode\\par\n{figure_block}\n\\par")
    if options_block:
        latex_blocks.append(f"\\begin{{minipage}}{{{content_width_cm}cm}}\n{options_block}\n\\end{{minipage}}")

    escaped_watermark = watermark.replace("_", "\\_").replace("&", "\\&").replace("%", "\\%")

    if display_id:
         footer_text = f"\\begin{{tabular}}{{@{{}}c@{{}}}} Q.REF: {display_id} \\enskip $\\bullet$ \\enskip \\telegramicon \\enskip {escaped_watermark} \\end{{tabular}}"
    else:
         footer_text = f"\\begin{{tabular}}{{@{{}}c@{{}}}} \\telegramicon \\enskip {escaped_watermark} \\end{{tabular}}"

    footer_latex = (
        f"\\begin{{minipage}}{{{content_width_cm}cm}}\n"
        f"\\vspace{{0.5em}}\n"
        f"\\noindent\\hrulefill \\par\n"
        f"\\vspace{{0.8em}}\n"
        f"\\centering \\color{{gray}} \\bfseries\\scriptsize {footer_text}\n"
        f"\\end{{minipage}}"
    )
    latex_blocks.append(footer_latex)
    body_content = "\n\\par\\vspace{1.5em}\n".join(latex_blocks)

    watermark_tikz = (
        f"\\begin{{tikzpicture}}[overlay]\n"
        f"  \\foreach \\y in {{0, -10, -20, -30, -40, -50, -60, -70, -80, -90, -100}} {{\n"
        f"    \\node[opacity=0.03, color=gray, rotate=25, scale=3.5, font=\\sffamily\\bfseries] at (8.25, \\y) {{{escaped_watermark}}};\n"
        f"  }}\n"
        f"\\end{{tikzpicture}}"
    )

    template = """\\documentclass[12pt]{article}
\\usepackage[utf8]{inputenc}
\\usepackage{mathpazo}
\\usepackage{amsmath, amssymb, pgfplots, enumitem, xcolor, adjustbox}
\\usepackage[paperwidth=18.5cm, paperheight=120cm, left=1.0cm, right=1.0cm, top=1.0cm, bottom=1.0cm]{geometry}
\\usepackage[active, tightpage]{preview}
\\setlength{\\PreviewBorder}{25pt}
\\pgfplotsset{compat=1.18, premium_style/.style={axis lines=middle, grid=both, grid style={line width=.3pt, draw=gray!20, dashed}, tick label style={font=\\small}, label style={font=\\small}, every axis line/.append style={-Stealth, line width=1pt, draw=black!80}, every tick/.append style={line width=0.6pt, draw=black!80}, samples=50}}
\\usetikzlibrary{arrows.meta, calc, patterns}
\\binoppenalty=10000
\\relpenalty=10000
\\sloppy
\\newcommand{\\telegramicon}{\\scalebox{0.9}{\\color{blue!70!cyan}$\\blacktriangleright$}}
\\begin{document}
\\begin{preview}
\\begin{minipage}{16.5cm}
\\pagecolor{white}
\\centering
\\noindent\\rule{16.5cm}{0pt}\\par
__WATERMARK_TIKZ__
__BODY_CONTENT__
\\par\\prevdepth=0pt
\\end{minipage}
\\end{preview}
\\end{document}"""
    return template.replace("__BODY_CONTENT__", body_content).replace("__WATERMARK_TIKZ__", watermark_tikz)

def assemble_diagram_only_layout(watermark: str, display_id: str, figure_block: str) -> str:
    escaped_watermark = watermark.replace("_", "\\_").replace("&", "\\&").replace("%", "\\%")

    template = """\\documentclass[12pt]{article}
\\usepackage[utf8]{inputenc}
\\usepackage{mathpazo}
\\usepackage{amsmath, amssymb, pgfplots, enumitem, xcolor, adjustbox, varwidth}
\\usepackage[paperwidth=18.5cm, paperheight=120cm, left=1.0cm, right=1.0cm, top=1.0cm, bottom=1.0cm]{geometry}
\\usepackage[active, tightpage]{preview}
\\setlength{\\PreviewBorder}{25pt}
\\pgfplotsset{compat=1.18, premium_style/.style={axis lines=middle, grid=both, grid style={line width=.3pt, draw=gray!20, dashed}, tick label style={font=\\small}, label style={font=\\small}, every axis line/.append style={-Stealth, line width=1pt, draw=black!80}, every tick/.append style={line width=0.6pt, draw=black!80}, samples=50}}
\\usetikzlibrary{arrows.meta, calc, patterns}
\\binoppenalty=10000
\\relpenalty=10000
\\sloppy
\\newcommand{\\telegramicon}{\\scalebox{0.9}{\\color{blue!70!cyan}$\\blacktriangleright$}}
\\begin{document}
\\begin{preview}
\\pagecolor{white}
\\centering
\\begin{minipage}{13.0cm}
  \\centering
  __FIGURE_BLOCK__\\par
  \\vspace{1.4em}
  {\\color{black!70}\\bfseries\\scriptsize
    \\begin{tabular}{@{}c@{}}
      Q.REF: __DISPLAY_ID__ \\enskip $\\bullet$ \\enskip \\telegramicon \\enskip __WATERMARK__
    \\end{tabular}
  }
\\end{minipage}
\\end{preview}
\\end{document}"""
    return (template.replace("__FIGURE_BLOCK__", figure_block)
                    .replace("__DISPLAY_ID__", str(display_id))
                    .replace("__WATERMARK__", escaped_watermark))

def build_widescreen_solution_latex(q, display_id, watermark: str, day_str: str) -> str:
    exp = q.get("poll_explanation", {})
    subject_escaped = escape_latex(q.get('subject', 'GENERAL').upper())
    topic_escaped = escape_latex(q.get('topic', 'General'))

    raw_rule = (exp.get('governing_principle') or exp.get('rule') or 'Concept').replace('\r\n', '\n').strip()
    rule_escaped = " \\\\ \n".join([escape_latex(line.strip()) for line in raw_rule.split('\n') if line.strip()])

    raw_why = exp.get('why', 'No detailed derivation available.').replace('\r\n', '\n').strip()
    why_escaped = " \\\\ \n".join([escape_latex(line.strip()) for line in raw_why.split('\n') if line.strip()])

    exp_figure_block = build_figure_block(q, add_strut=False)
    diagram_block = f"\\par\\noindent\\centering\\leavevmode\\par\n{exp_figure_block}\n\\par\\vspace{{2.0em}}\n" if exp_figure_block else ""
    question_escaped = escape_latex(q.get('question', ''))

    escaped_watermark = watermark.replace("_", "\\_").replace("&", "\\&").replace("%", "\\%")
    watermark_tikz = (
        f"\\begin{{tikzpicture}}[overlay]\n"
        f"  \\foreach \\y in {{0, -10, -20, -30, -40, -50, -60, -70, -80, -90, -100}} {{\n"
        f"    \\node[opacity=0.03, color=gray, rotate=25, scale=3.5, font=\\sffamily\\bfseries] at (8.25, \\y) {{{escaped_watermark}}};\n"
        f"  }}\n"
        f"\\end{{tikzpicture}}"
    )

    template = """\\documentclass[12pt]{article}
\\usepackage[utf8]{inputenc}
\\usepackage{mathpazo}
\\usepackage{amsmath, amssymb, pgfplots, enumitem, xcolor, adjustbox}
\\usepackage[paperwidth=18.5cm, paperheight=120cm, left=1.0cm, right=1.0cm, top=1.0cm, bottom=1.0cm]{geometry}
\\usepackage[active, tightpage]{preview}
\\setlength{\\PreviewBorder}{25pt}
\\pgfplotsset{compat=1.18, premium_style/.style={axis lines=middle, grid=both, grid style={line width=.3pt, draw=gray!20, dashed}, tick label style={font=\\small}, label style={font=\\small}, every axis line/.append style={-Stealth, line width=1pt, draw=black!80}, every tick/.append style={line width=0.6pt, draw=black!80}, samples=50}}
\\usetikzlibrary{arrows.meta, calc, patterns}
\\binoppenalty=10000
\\relpenalty=10000
\\openup 1em
\\newcommand{\\telegramicon}{\\scalebox{0.9}{\\color{blue!70!cyan}$\\blacktriangleright$}}
\\begin{document}
\\begin{preview}
\\begin{minipage}{16.5cm}
\\pagecolor{white}
\\centering
\\noindent\\rule{16.5cm}{0pt}\\par
__WATERMARK_TIKZ__
\\begin{minipage}{15.0cm}
    \\flushleft
    {\\noindent \\large \\textbf{SOLUTION SHEET: REF __DISPLAY_ID__}} \\par
    \\vspace{0.3em}
    {\\noindent \\small \\color{gray} Subject: __SUBJECT__ \\quad $\\bullet$ \\quad Topic: __TOPIC__} \\par
    \\vspace{0.8em}
    \\noindent\\hrulefill \\par
\\end{minipage}
\\par\\vspace{2.0em}
__DIAGRAM_BLOCK__
\\begin{minipage}{15.0cm}
    \\flushleft
    {\\noindent \\large \\textbf{PROBLEM CANVAS}} \\par
    \\vspace{0.4em}
    \\noindent\\hrulefill \\par
    \\vspace{1.0em}
    {\\noindent \\small \\textbf{Question Details:} \\\\ __QUESTION__} \\par
    \\vspace{1.5em}
    \\begin{adjustbox}{minipage=14.2cm, margin=0.8ex, bgcolor=gray!5, frame=0.3pt}
        {\\noindent \\small \\textbf{Governing Principle \\& Formulation:}} \\\\
        \\vspace{0.4em}
        __RULE__
    \\end{adjustbox} \\par
\\end{minipage}
\\par\\vspace{2.5em}
\\begin{minipage}{15.0cm}
    \\flushleft
    {\\noindent \\large \\textbf{DERIVATION \\& STEPS}} \\par
    \\vspace{0.4em}
    \\noindent\\hrulefill \\par
    \\vspace{1.0em}
    {\\noindent \\small \\textbf{Step-by-Step Calculation:} \\\\ __WHY__} \\par
\\end{minipage}
\\par\\vspace{2.5em}
\\begin{minipage}{15.0cm}
    \\noindent\\hrulefill \\par
    \\vspace{0.8em}
    \\centering {\\color{gray}\\bfseries\\scriptsize
        \\begin{tabular}{@{}c@{}}
            Q.REF: __DISPLAY_ID__ \\enskip $\\bullet$ \\enskip \\telegramicon \\enskip __WATERMARK__
        \\end{tabular}
    }
\\end{minipage}
\\par\\prevdepth=0pt
\\end{minipage}
\\end{preview}
\\end{document}"""
    return (template.replace("__DIAGRAM_BLOCK__", diagram_block).replace("__WATERMARK_TIKZ__", watermark_tikz)
                .replace("__DISPLAY_ID__", str(display_id)).replace("__SUBJECT__", subject_escaped)
                .replace("__TOPIC__", topic_escaped).replace("__QUESTION__", question_escaped)
                .replace("__RULE__", rule_escaped).replace("__WHY__", why_escaped))

def get_day_from_tags(tags=None):
    now = datetime.now()
    if tags and isinstance(tags, list):
        iso_pattern = re.compile(r'^(\d{4})[-/](\d{2})[-/](\d{2})$')
        common_pattern = re.compile(r'^(\d{2})[-/](\d{2})[-/](\d{4})$')
        for tag in tags:
            tag = str(tag).strip()
            iso_match = iso_pattern.match(tag)
            if iso_match:
                try:
                    year, month, day = map(int, iso_match.groups())
                    return _format_datetime_day(datetime(year, month, day))
                except ValueError: pass
            common_match = common_pattern.match(tag)
            if common_match:
                try:
                    day, month, year = common_match.match(tag).groups()
                    return _format_datetime_day(datetime(year, month, day))
                except ValueError: pass
    return _format_datetime_day(now)

def _format_datetime_day(dt):
    try: return dt.strftime("%B %-d, %Y")
    except ValueError: return dt.strftime("%B %d, %Y").replace(" 0", " ")

def sanitize_tag_to_hashtag(tag):
    tag = str(tag).strip()
    iso_pattern = re.compile(r'^(\d{4})[-/](\d{2})[-/](\d{2})$')
    common_pattern = re.compile(r'^(\d{2})[-/](\d{2})[-/](\d{4})$')
    if iso_pattern.match(tag):
        tag = f"date_{tag.replace('-', '_').replace('/', '_')}"
    elif common_pattern.match(tag):
        day, month, year = common_pattern.match(tag).groups()
        tag = f"date_{year}_{month}_{day}"
    else:
        tag = tag.replace("-", "_").replace(" ", "_")
        tag = re.sub(r'[^\w_]', '', tag)
    return f"#{tag}"

def create_explanation_assets(q, user_idx, display_id):
    has_tikz = has_real_diagram(q)

    latex_code = None
    if has_tikz:
        figure_block = build_figure_block(q, add_strut=False)
        if figure_block:
            latex_code = assemble_diagram_only_layout("@grade12EntranceExam", display_id, figure_block)
        else:
            has_tikz = False

    from src.rendering.html_views import build_answered_view
    caption_html = build_answered_view(q, str(display_id), user_idx, compact=has_tikz)
    return latex_code, caption_html