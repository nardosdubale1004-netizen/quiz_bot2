# src/typography.py
import re
import html

# Map common LaTeX elements to safe Unicode characters for non-math fallback views
MATH_MAP = {
    r"\pi": "π", r"\theta": "θ", r"\alpha": "α", r"\beta": "β",
    r"\gamma": "γ", r"\Delta": "Δ", r"\sigma": "σ", r"\Omega": "Ω",
    r"\sqrt": "√", r"\infty": "∞", r"\pm": "±", r"\times": "×",
    r"\neq": "≠", r"\le": "≤", r"\ge": "≥", r"\rightarrow": "→",
    r"\approx": "≈", r"\cdot": "·", r"\in": "∈", r"\partial": "∂",
    r"\vec": "", r"\,": " ", r"\quad": "   ", r"\text": "",
    r"\bar": "", r"\hat": "", r"\nabla": "∇", r"\angle": "∠",
    r"\cos^{-1}": "cos⁻¹", r"\sin^{-1}": "sin⁻¹", r"\tan^{-1}": "tan⁻¹",
    r"\parallel": "∥", r"\perp": "⊥", r"\sum": "∑", r"\overline": "",
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

def replace_fractions(text):
    """
    Recursively replaces LaTeX fractions with readable unicode equivalents, 
    accounting for nested braces and functions.
    """
    iterations = 0
    while iterations < 100:
        iterations += 1
        match = re.search(r'\\frac\s*\{', text)
        if not match:
            break
        start_idx = match.start()
        
        # Balance check numerator
        num_start = start_idx + len("\\frac{") - 1
        brace_count = 0
        num_end = -1
        for idx in range(num_start, len(text)):
            if text[idx] == '{':
                brace_count += 1
            elif text[idx] == '}':
                brace_count -= 1
                if brace_count == 0:
                    num_end = idx
                    break
        if num_end == -1:
            break
            
        num_content = text[num_start+1:num_end]
        
        # Balance check denominator
        denom_search_start = num_end + 1
        denom_match = re.match(r'\s*\{', text[denom_search_start:])
        if not denom_match:
            break
            
        denom_start = denom_search_start + denom_match.end() - 1
        brace_count = 0
        denom_end = -1
        for idx in range(denom_start, len(text)):
            if text[idx] == '{':
                brace_count += 1
            elif text[idx] == '}':
                brace_count -= 1
                if brace_count == 0:
                    denom_end = idx
                    break
        if denom_end == -1:
            break
            
        denom_content = text[denom_start+1:denom_end]
        
        # Recursively parse internal fractions
        num_content = replace_fractions(num_content)
        denom_content = replace_fractions(denom_content)
        
        if len(num_content) == 1 and len(denom_content) == 1:
            replacement = f"{num_content}/{denom_content}"
        else:
            num_str = num_content if (num_content.startswith('(') and num_content.endswith(')')) or len(num_content) == 1 else f"({num_content})"
            denom_str = denom_content if (denom_content.startswith('(') and denom_content.endswith(')')) or len(denom_content) == 1 else f"({denom_content})"
            replacement = f"{num_str}/{denom_str}"
            
        text = text[:start_idx] + replacement + text[denom_end+1:]
    return text

def replace_sqrts(text):
    """
    Recursively replaces LaTeX square roots with unicode symbols.
    """
    iterations = 0
    while iterations < 100:
        iterations += 1
        match = re.search(r'\\sqrt\s*\{', text)
        if not match:
            break
        start_idx = match.start()
        
        start_brace = start_idx + len("\\sqrt{") - 1
        brace_count = 0
        end_brace = -1
        for idx in range(start_brace, len(text)):
            if text[idx] == '{':
                brace_count += 1
            elif text[idx] == '}':
                brace_count -= 1
                if brace_count == 0:
                    end_brace = idx
                    break
        if end_brace == -1:
            break
            
        content = text[start_brace+1:end_brace]
        content = replace_sqrts(content)
        content = replace_fractions(content)
        
        replacement = f"√({content})"
        text = text[:start_idx] + replacement + text[end_brace+1:]
    return text

def clean_latex_to_unicode(text):
    """
    Cleans LaTeX commands and converts formulas into plain unicode representations
    for clean display in UI buttons and alternative views.
    """
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

    # Resolve complex square roots and fractions recursively
    text = replace_sqrts(text)
    text = replace_fractions(text)

    # Convert simple inline exponents and subscripts
    text = convert_superscripts(text)
    text = convert_subscripts(text)

    # Standard cleanups of remaining variables and commands
    for latex_sym, unicode_sym in MATH_MAP.items():
        text = text.replace(latex_sym, unicode_sym)

    text = text.replace("\\", "")
    text = text.replace("{", "").replace("}", "")  # Remove leftover grouping brackets for fallback text view
    return re.sub(r'[ \t]+', ' ', text).strip()

def lite_math(text):
    if not text:
        return ""
    return clean_latex_to_unicode(text.replace("$", ""))

def sanitize_latex_for_telegram_math(expr: str) -> str:
    """
    Standardizes LaTeX expressions to adhere strictly to Telegram's native
    mathematical parser (KaTeX) without stripping standard formatting commands
    like \vec, \text, \quad, etc., which are fully supported in Telegram Premium 10.x+.
    """
    if not expr:
        return ""

    # Simply trim whitespace and normalize newlines to single spaces.
    # We do NOT strip formatting commands as they are natively rendered by Telegram.
    expr = expr.strip().replace("\n", " ").replace("\r", "")
    return expr

def auto_wrap_math_expressions(text: str) -> str:
    """
    Safely wraps plain-text math expressions in '$' delimiters.
    Uses highly secure patterns to remain immune to catastrophic backtracking.
    """
    if not text:
        return ""

    # Tokenize input to protect existing math environments and HTML blocks
    pattern = re.compile(
        r'(<tg-math-block>.*?</tg-math-block>|'
        r'<tg-math>.*?</tg-math>|'
        r'<pre>.*?</pre>|'
        r'<code>.*?</code>|'
        r'</?[a-zA-Z1-6-]+(?:\s+[^>]*)?/?>|'
        r'\$\$.*?\訊|'
        r'\$.*?\$|'
        r'\\\[.*?\\\]|'
        r'\\\(.*?\\\))',
        re.DOTALL
    )

    parts = pattern.split(text)

    for i in range(len(parts)):
        if i % 2 == 0:  # segment is plain text
            segment = parts[i]
            if not segment.strip():
                continue

            # Pattern 1: Contiguous LaTeX commands with backslashes (linear complexity)
            segment = re.sub(
                r'(\\([a-zA-Z0-9]+|begin|end)(?:\{[^{}]*\}|\[[^\]]*\]|[a-zA-Z0-9()+\-*/^=_<>,.\\\s&])*)',
                lambda m: f"${m.group(1).strip()}$" if m.group(1).strip() else m.group(0),
                segment
            )

            # Pattern 2: Mathematical equations and assignments containing equals/inequalities
            segment = re.sub(
                r'((?:[a-zA-Z0-9_+*/^()\-]+(?:\s+[a-zA-Z0-9_+*/^()\-]+)*)\s*(?:=|<=|>=|<|>|\\neq|\\approx|\\le|\\ge)\s*(?:[a-zA-Z0-9_+*/^()\-]+(?:\s+[a-zA-Z0-9_+*/^()\-]+)*))',
                lambda m: f"${m.group(1).strip()}$" if not m.group(1).strip().startswith('$') else m.group(0),
                segment
            )

            # Pattern 3: Standard variables with exponents or parentheses
            segment = re.sub(
                r'\b([a-zA-Z]\^[0-9a-zA-Z+-]+|[a-zA-Z]_[0-9a-zA-Z+-]+|\b[a-zA-Z]\([a-zA-Z0-9+-]\))\b',
                r'$\1$',
                segment
            )

            # Normalize potential double wraps
            segment = segment.replace('$$', '$')
            parts[i] = segment

    return "".join(parts)

def beautify_markdown_math(text):
    if not text:
        return ""

    text = str(text)

    # Safely wraps unformatted math structures (including matrices)
    text = auto_wrap_math_expressions(text)

    # Normalize LaTeX math delimiters to standard $ and $$ first
    text = text.replace(r'\[', '$$').replace(r'\]', '$$')
    text = text.replace(r'\(', '$').replace(r'\)', '$')

    # Replace literal escaped newlines with actual newlines where safe
    text = re.sub(r'\\+n(?!eq|ode|earrow|abla|eg|um|otin|ew|orm|exists|subset|i\b|ormalsize|umber)', '\n', text)
    text = text.replace("<br>", "\n").replace("<br/>", "\n").replace("\r", "")

    def clean_plain_text_latex_formatting(s):
        s = re.sub(r'\\vspace\{[^}]*\}', '\n', s)
        s = re.sub(r'\\hspace\{[^}]*\}', ' ', s)
        s = s.replace(r"\par", "\n")
        s = re.sub(r'\\noindent\b', '', s)
        s = re.sub(r'\\leavevmode\b', '', s)
        s = s.replace(r"\,", " ")
        s = s.replace(r"\quad", "   ")
        return s

    def step_repl(match):
        step_num = match.group(1)
        emojis = {"1": "1️⃣", "2": "2️⃣", "3": "3️⃣", "4": "4️⃣", "5": "5️⃣", "6": "6️⃣", "7": "7️⃣", "8": "8️⃣", "9": "9️⃣"}
        emoji = emojis.get(step_num, "▪️")
        return f"\n<b>{emoji} Step {step_num}:</b>\n  "

    text = re.sub(r'(?i)\bStep\s*(\d+)[:.-]?\s*', step_repl, text)

    # Tokenize by all forms of inline and block math tags to protect math syntax
    math_pattern = re.compile(
        r'(<tg-math-block>.*?</tg-math-block>|'
        r'<tg-math>.*?</tg-math>|'
        r'\$\$.*?\$\$|'
        r'\$.*?\$)',
        re.DOTALL
    )

    parts = math_pattern.split(text)
    for i in range(len(parts)):
        if i % 2 == 1:
            # Math Segment (odd index) - Protect formatting macros
            segment = parts[i]
            if segment.startswith("<tg-math-block>"):
                raw_formula = segment[len("<tg-math-block>") : -len("</tg-math-block>")]
                is_block = True
            elif segment.startswith("<tg-math>"):
                raw_formula = segment[len("<tg-math>") : -len("</tg-math>")]
                is_block = False
            elif segment.startswith("$$"):
                raw_formula = segment[2:-2]
                is_block = True
            elif segment.startswith("$"):
                raw_formula = segment[1:-1]
                is_block = False
            else:
                raw_formula = segment
                is_block = False

            sanitized = sanitize_latex_for_telegram_math(raw_formula)
            if is_block:
                parts[i] = f"\n<tg-math-block>{sanitized}</tg-math-block>\n"
            else:
                parts[i] = f"<tg-math>{sanitized}</tg-math>"
        else:
            # Plain Text Segment (even index) - Clean prose commands
            cleaned = clean_plain_text_latex_formatting(parts[i])
            parts[i] = escape_plain_text(cleaned)

    result = "".join(parts)
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result.strip()