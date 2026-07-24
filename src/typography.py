# src/typography.py
import re
import html

MATH_MAP = {
    r"\pi": "π", r"\theta": "θ", r"\alpha": "α", r"\beta": "β",
    r"\gamma": "γ", r"\Delta": "Δ", r"\sigma": "σ", r"\Omega": "Ω",
    r"\sqrt": "√", r"\infty": "∞", r"\pm": "±", r"\times": "×",
    r"\neq": "≠", r"\le": "≤", r"\ge": "≥", r"\rightarrow": "→",
    r"\approx": "≈", r"\cdot": "·", r"\in": "∈", r"\partial": "∂",
    r"\vec": "vec", r"\,": " ", r"\quad": "   ", r"\text": "",
    r"\bar": "bar", r"\hat": "hat", r"\nabla": "∇", r"\angle": "∠",
    r"\cos^{-1}": "cos⁻¹", r"\sin^{-1}": "sin⁻¹", r"\tan^{-1}": "tan⁻¹",
    r"\parallel": "∥", r"\perp": "⊥", r"\sum": "∑", r"\overline": "bar",
    r"\implies": "⇒", r"\iff": "⇔", r"\to": "→"
}

UNICODE_TO_LATEX = {
    "π": r"\pi", "θ": r"\theta", "α": r"\alpha", "β": r"\beta",
    "γ": r"\gamma", "Δ": r"\Delta", "σ": r"\sigma", "Ω": r"\Omega",
    "√": r"\sqrt", "∞": r"\infty", "²": r"^2", "³": r"^3",
    "₀": r"_{0}", "±": r"\pm", "×": r"\times", "≠": r"\neq",
    "≤": r"\le", "≥": r"\ge", "→": r"\rightarrow", "≈": r"\approx"
}

SUPERSCRIPTS = {
    '0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴', '5': '⁵', '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹',
    '+': '⁺', '-': '⁻', '=': '⁼', '(': '⁽', ')': '⁾', 'n': 'ⁿ', 'i': 'ⁱ', 'x': 'ˣ', 'y': 'ʸ', 'r': 'ʳ',
    'a': 'ᵃ', 'b': 'ᵇ', 'c': 'ᶜ', 'd': 'ᵈ', 'e': 'ᵉ', 'f': 'ᶠ', 'g': 'ᵍ', 'h': 'ʰ', 'j': 'ʲ', 'k': 'ᵏ',
    'l': 'ˡ', 'm': 'ᵐ', 'o': 'ᵒ', 'p': 'ᵖ', 's': 'ˢ', 't': 'ᵗ', 'u': 'ᵘ', 'v': 'ᵛ', 'w': 'ʷ', 'z': 'ᶻ'
}

SUBSCRIPTS = {
    '0': '₀', '1': '₁', '2': '₂', '3': '₃', '4': '₄', '5': '₅', '6': '₆', '7': '₇', '8': '₈', '9': '₉',
    '+': '₊', '-': '₋', '=': '₌', '(': '₍', ')': '₎', 'a': 'ₐ', 'e': 'ₑ', 'h': 'ₕ', 'i': 'ᵢ', 'j': 'ⱼ',
    'k': 'ₖ', 'l': 'ₗ', 'm': 'ₘ', 'n': 'ₙ', 'o': 'ₒ', 'p': 'ₚ', 'r': 'ᵣ', 's': 'ₛ', 't': 'ₜ', 'u': 'ᵤ',
    'v': 'ᵥ', 'x': 'ₓ'
}

def escape_plain_text(text: str) -> str:
    allowed_tags = [
        "b", "/b", "i", "/i", "u", "/u", "s", "/s", "tg-spoiler", "/tg-spoiler",
        "code", "/code", "pre", "/pre", "a", "/a", "blockquote", "/blockquote",
        "tg-math", "/tg-math", "tg-math-block", "/tg-math-block",
        "h1", "/h1", "h2", "/h2", "h3", "/h3", "h4", "/h4", "h5", "/h5", "h6", "/h6",
        "ul", "/ul", "ol", "/ol", "li", "/li", "table", "/table", "tr", "/tr", "td", "/td",
        "br", "hr", "mark", "/mark", "sub", "/sub", "sup", "/sup"
    ]
    parts = re.split(r'(</?[a-zA-Z1-6-]+(?:\s+[^>]*)?/?>)', text)
    for i in range(len(parts)):
        if i % 2 == 1:
            tag_match = re.match(r'</?([a-zA-Z1-6-]+)', parts[i])
            if tag_match:
                tag_name = tag_match.group(1).lower()
                if tag_name in allowed_tags or parts[i].startswith("<blockquote expandable"):
                    continue
            parts[i] = html.escape(parts[i])
        else:
            parts[i] = html.escape(parts[i])
    return "".join(parts)

def convert_superscripts(text):
    def repl(match):
        val = match.group(1) or match.group(2)
        return "".join(SUPERSCRIPTS.get(c, c) for c in val)
    text = re.sub(r'\^\{([^}]+)\}', repl, text)
    text = re.sub(r'\^([a-zA-Z0-9+-])', repl, text)
    return text

def convert_subscripts(text):
    def repl(match):
        val = match.group(1) or match.group(2)
        return "".join(SUBSCRIPTS.get(c, c) for c in val)
    text = re.sub(r'_\{([^}]+)\}', repl, text)
    text = re.sub(r'_([a-zA-Z0-9+-])', repl, text)
    return text

def clean_latex_to_unicode(text):
    if not text:
        return ""
    text = str(text)
    text = re.sub(r'</?tg-math(?:-block)?>', '', text)
    text = text.replace(r"\par", "\n").replace(r"\quad", "   ").replace(r"\,", " ")
    text = text.replace(r"\left", "").replace(r"\right", "")
    text = text.replace(r"^\circ", "°").replace(r"\circ", "°").replace(r"^circ", "°")

    text = re.sub(r'\\int_\{([^}]+)\}\^\{([^}]+)\}', r'∫(limits \1 to \2) ', text)
    text = re.sub(r'\\int_([a-zA-Z0-9+-])\^([a-zA-Z0-9+-])', r'∫(limits \1 to \2) ', text)
    text = text.replace(r"\int", "∫")

    def frac_repl(match):
        num, denom = match.group(1), match.group(2)
        if len(num) == 1 and len(denom) == 1:
            return f"{num}/{denom}"
        return f"({num})/({denom})"

    text = re.sub(r'\\frac\{([^{}]+)\}\{([^{}]+)\}', frac_repl, text)
    text = re.sub(r'\\sqrt\{([^{}]+)\}', r'√(\1)', text)

    text = convert_superscripts(text)
    text = convert_subscripts(text)

    for latex_sym, unicode_sym in MATH_MAP.items():
        text = text.replace(latex_sym, unicode_sym)

    text = text.replace("\\", "")
    return re.sub(r'[ \t]+', ' ', text).strip()

def lite_math(text):
    if not text:
        return ""
    return clean_latex_to_unicode(text.replace("$", ""))

def sanitize_latex_for_telegram_math(expr: str) -> str:
    if not expr:
        return ""

    expr = expr.strip().replace("\n", " ").replace("\r", "")
    expr = expr.replace(r"\allowbreak", "")
    expr = expr.replace(r"\par", " ")
    expr = expr.replace(r"\,", " ")
    expr = expr.replace(r"\quad", "   ")
    expr = expr.replace(r"\qquad", "   ")
    expr = expr.replace(r"\\", ", ")
    expr = expr.replace(r"\left", "").replace(r"\right", "")
    expr = expr.replace(r"\|", "||")

    expr = expr.replace(r"^\circ", "°").replace(r"\circ", "°")

    expr = expr.replace(r"\mathbb{R}", "ℝ").replace(r"\mathbb{N}", "ℕ")
    expr = expr.replace(r"\mathbb{Z}", "ℤ").replace(r"\mathbb{C}", "ℂ")
    expr = expr.replace(r"\mathbb{Q}", "ℚ")

    for _ in range(3):
        expr = re.sub(r'\\math[a-zA-Z]*\s*\{([^}]+)\}', r'\1', expr)
        expr = re.sub(r'\\text[a-zA-Z]*\s*\{([^}]+)\}', r'\1', expr)
        expr = re.sub(r'\\vec\s*\{([^}]+)\}', r'\1', expr)
        expr = re.sub(r'\\overline\s*\{([^}]+)\}', r'\1', expr)
        expr = re.sub(r'\\hat\s*\{([^}]+)\}', r'\1', expr)
        expr = re.sub(r'\\bar\s*\{([^}]+)\}', r'\1', expr)

    # Clean fallback for any unformatted standard mathematical matrices
    def convert_matrices(match):
        content = match.group(1).strip()
        content = content.replace("&", ", ").replace("\\\\", "; ").replace("\n", " ")
        content = re.sub(r'\s+', ' ', content)
        return f"[{content}]"

    expr = re.sub(r'\\begin\{(?:pmatrix|matrix|bmatrix|cases)\}(.*?)\\end\{(?:pmatrix|matrix|bmatrix|cases)\}', convert_matrices, expr, flags=re.DOTALL)
    return re.sub(r'\s+', ' ', expr).strip()

def convert_latex_matrix_to_unicode(text: str) -> str:
    """
    Converts LaTeX matrices (pmatrix, bmatrix, matrix, cases) to gorgeous,
    perfectly-aligned 2D Unicode plain-text arrays for standard messages.
    """
    pattern = re.compile(r'\\begin\{(pmatrix|bmatrix|matrix|cases)\}(.*?)\\end\{\1\}', re.DOTALL)
    
    def repl(match):
        env_type = match.group(1)
        content = match.group(2).strip()
        
        rows_raw = re.split(r'\\\\|\\cr', content)
        rows = []
        for r in rows_raw:
            r = r.strip()
            if not r and len(rows_raw) > 1:
                continue
            cols = [c.strip() for c in r.split('&')]
            rows.append(cols)
            
        if not rows:
            return ""
            
        num_cols = max(len(r) for r in rows)
        col_widths = [0] * num_cols
        for r in rows:
            for c_idx, cell in enumerate(r):
                clean_cell = lite_math(cell)
                col_widths[c_idx] = max(col_widths[c_idx], len(clean_cell))
                
        formatted_rows = []
        for r in rows:
            row_cells = []
            for c_idx in range(num_cols):
                cell_val = r[c_idx] if c_idx < len(r) else ""
                clean_cell = lite_math(cell_val)
                padded = clean_cell.center(col_widths[c_idx])
                row_cells.append(padded)
            formatted_rows.append("  ".join(row_cells))
            
        n_rows = len(formatted_rows)
        result_lines = []
        
        if env_type == "pmatrix":
            if n_rows == 1:
                result_lines.append(f"({formatted_rows[0]})")
            elif n_rows == 2:
                result_lines.append(f"⎛ {formatted_rows[0]} ⎞")
                result_lines.append(f"⎝ {formatted_rows[1]} ⎠")
            else:
                result_lines.append(f"⎛ {formatted_rows[0]} ⎞")
                for i in range(1, n_rows - 1):
                    result_lines.append(f"⎜ {formatted_rows[i]} ⎟")
                result_lines.append(f"⎝ {formatted_rows[-1]} ⎠")
                
        elif env_type == "bmatrix":
            if n_rows == 1:
                result_lines.append(f"[{formatted_rows[0]}]")
            elif n_rows == 2:
                result_lines.append(f"⎡ {formatted_rows[0]} ⎤")
                result_lines.append(f"⎣ {formatted_rows[1]} ⎦")
            else:
                result_lines.append(f"⎡ {formatted_rows[0]} ⎤")
                for i in range(1, n_rows - 1):
                    result_lines.append(f"⎢ {formatted_rows[i]} ⎥")
                result_lines.append(f"⎣ {formatted_rows[-1]} ⎦")
                
        elif env_type == "cases":
            if n_rows == 1:
                result_lines.append(f"⎧ {formatted_rows[0]}")
            elif n_rows == 2:
                result_lines.append(f"⎧ {formatted_rows[0]}")
                result_lines.append(f"⎩ {formatted_rows[1]}")
            else:
                result_lines.append(f"⎧ {formatted_rows[0]}")
                for i in range(1, n_rows - 1):
                    result_lines.append(f"⎨ {formatted_rows[i]}")
                result_lines.append(f"⎩ {formatted_rows[-1]}")
        else:
            result_lines = formatted_rows
            
        return "\n" + "\n".join(result_lines) + "\n"

    return pattern.sub(repl, text)

def auto_wrap_math_expressions(text: str) -> str:
    """
    Intelligently scans the plain text parts of a string and wraps any mathematical 
    equations, variables, or expressions in '$' delimiters, without touching existing HTML tags.
    """
    if not text:
        return ""

    # Protect existing mathematical blocks, tags, and formatting blocks
    pattern = re.compile(
        r'(<tg-math-block>.*?</tg-math-block>|'
        r'<tg-math>.*?</tg-math>|'
        r'<pre>.*?</pre>|'
        r'<code>.*?</code>|'
        r'</?[a-zA-Z1-6-]+(?:\s+[^>]*)?/?>|'
        r'\$\$.*?\$\$|'
        r'\$.*?\$|'
        r'\\\[.*?\\\]|'
        r'\\\(.*?\\\))',
        re.DOTALL
    )
    
    parts = pattern.split(text)
    
    for i in range(len(parts)):
        # Every even index represents plain text outside tags/blocks
        if i % 2 == 0:
            segment = parts[i]
            if not segment.strip():
                continue
            
            # Pattern A: Contiguous LaTeX commands with backslashes
            # e.g., \lim_{x\to2}\frac{(x-2)(x+2)}{x-2} = \lim_{x\to2}(x+2)
            segment = re.sub(
                r'((?:\\[a-zA-Z]+|\d|[a-zA-Z]|[+\-*/^=<>(){}\[\]_.,\\ \t]|\\neq|\\approx|\\le|\\ge|\\to)+)',
                lambda m: f"${m.group(1).strip()}$" if '\\' in m.group(1) else m.group(1),
                segment
            )
            
            # Pattern B: Standard math equations & inequalities
            # e.g., "x^2 - 4 = (x-2)(x+2)" or "2 + 0 = 2 \neq 4"
            segment = re.sub(
                r'((?:[a-zA-Z0-9_+*/^()-]+\s*)+(?:=|<=|>=|<|>|\\neq|\\approx|\\le|\\ge)\s*(?:[a-zA-Z0-9_+*/^()-]+\s*)+)',
                lambda m: f"${m.group(1).strip()}$" if not m.group(1).strip().startswith('$') else m.group(1),
                segment
            )
            
            # Pattern C: Standard individual variable expressions
            # e.g., "x^2", "a^2", "f(x)"
            segment = re.sub(
                r'\b([a-zA-Z]\^[0-9a-zA-Z+-]+|\b[a-zA-Z]\([a-zA-Z0-9+-]\))\b',
                r'$\1$',
                segment
            )
            
            # Clean consecutive delimiters from replacements
            segment = segment.replace('$$', '$')
            parts[i] = segment

    return "".join(parts)

def beautify_markdown_math(text):
    if not text:
        return ""

    text = str(text)
    
    # First convert standard LaTeX matrices to elegant 2D plain-text Unicode alignments
    text = convert_latex_matrix_to_unicode(text)
    
    # Parse and wrap remaining unformatted plain-text equations safely
    text = auto_wrap_math_expressions(text)
    
    text = text.replace("\\\\n", "\n").replace("\\n", "\n").replace(r"\n", "\n")
    text = text.replace("<br>", "\n").replace("<br/>", "\n").replace("\r", "")

    text = re.sub(r'\\vspace\{[^}]*\}', '\n', text)
    text = re.sub(r'\\hspace\{[^}]*\}', ' ', text)
    text = text.replace(r"\par", "\n")
    text = re.sub(r'\\noindent\b', '', text)
    text = re.sub(r'\\leavevmode\b', '', text)
    text = text.replace(r"\,", " ")
    text = text.replace(r"\quad", "   ")

    def step_repl(match):
        step_num = match.group(1)
        emojis = {"1": "1️⃣", "2": "2️⃣", "3": "3️⃣", "4": "4️⃣", "5": "5️⃣", "6": "6️⃣", "7": "7️⃣", "8": "8️⃣", "9": "9️⃣"}
        emoji = emojis.get(step_num, "▪️")
        return f"\n<b>{emoji} Step {step_num}:</b>\n  "

    result = re.sub(r'(?i)\bStep\s*(\d+)[:.-]?\s*', step_repl, text)

    result = result.replace(r'\[', '$$').replace(r'\]', '$$')
    result = result.replace(r'\(', '$').replace(r'\)', '$')

    parts_block = result.split('$$')
    for i in range(len(parts_block)):
        if i % 2 == 1:
            sanitized_formula = sanitize_latex_for_telegram_math(parts_block[i])
            parts_block[i] = f"\n<tg-math-block>{sanitized_formula}</tg-math-block>\n"
        else:
            parts_inline = parts_block[i].split('$')
            for j in range(len(parts_inline)):
                if j % 2 == 1:
                    sanitized_formula = sanitize_latex_for_telegram_math(parts_inline[j])
                    parts_inline[j] = f"<tg-math>{sanitized_formula}</tg-math>"
                else:
                    parts_inline[j] = escape_plain_text(parts_inline[j])
            parts_block[i] = "".join(parts_inline)

    result = "".join(parts_block)

    def sanitize_tg_math_tag(match):
        formula = match.group(1)
        return f"<tg-math>{sanitize_latex_for_telegram_math(formula)}</tg-math>"

    def sanitize_tg_math_block_tag(match):
        formula = match.group(1)
        return f"<tg-math-block>{sanitize_latex_for_telegram_math(formula)}</tg-math-block>"

    result = re.sub(r'<tg-math>(.*?)</tg-math>', sanitize_tg_math_tag, result, flags=re.DOTALL)
    result = re.sub(r'<tg-math-block>(.*?)</tg-math-block>', sanitize_tg_math_block_tag, result, flags=re.DOTALL)

    result = re.sub(r'\n{3,}', '\n\n', result)
    return result.strip()