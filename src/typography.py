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
    """Escapes raw XML special characters that are not part of supported formatting tags."""
    allowed_tags = [
        "b", "/b", "i", "/i", "u", "/u", "s", "/s", "tg-spoiler", "/tg-spoiler",
        "code", "/code", "pre", "/pre", "a", "/a", "blockquote", "/blockquote",
        "tg-math", "/tg-math", "tg-math-block", "/tg-math-block",
        "h1", "/h1", "h2", "/h2", "h3", "/h3", "h4", "/h4", "h5", "/h5", "h6", "/h6",
        "ul", "/ul", "ol", "/ol", "li", "/li", "table", "/table", "tr", "/tr", "td", "/td",
        "br", "hr", "mark", "/mark", "sub", "/sub", "sup", "/superscript"
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

    # 1. Clean spacing and paragraph noise
    text = text.replace(r"\par", "\n").replace(r"\quad", "   ").replace(r"\,", " ")
    text = text.replace(r"\left", "").replace(r"\right", "")
    text = text.replace(r"\noindent", "").replace(r"\leavevmode", "").replace("oindent", "")
    text = text.replace(r"\allowbreak", "").replace(r"\displaystyle", "")

    # 2. Degrees
    text = text.replace(r"^\circ", "°").replace(r"\circ", "°").replace(r"^circ", "°")

    # 3. Integrals
    text = re.sub(r'\\int_\{([^}]+)\}\^\{([^}]+)\}', r'∫_{\1}^{\2} ', text)
    text = re.sub(r'\\int_([a-zA-Z0-9+-])\^([a-zA-Z0-9+-])', r'∫_{\1}^{\2} ', text)
    text = text.replace(r"\int", "∫")

    # 4. Fractions (\frac{a}{b})
    for _ in range(3):
        new_text = re.sub(r'\\frac\{([^{}]+)\}\{([^{}]+)\}', lambda m: f"({m.group(1)})/({m.group(2)})" if (len(m.group(1)) > 1 or len(m.group(2)) > 1) and not m.group(1).isdigit() else f"{m.group(1)}/{m.group(2)}", text)
        if new_text == text:
            break
        text = new_text

    # 5. Square roots (\sqrt{a})
    for _ in range(3):
        new_text = re.sub(r'\\sqrt\[([^\]]+)\]\{([^{}]+)\}', r'\1√(\2)', text)
        new_text = re.sub(r'\\sqrt\{([^{}]+)\}', lambda m: f"√{m.group(1)}" if len(m.group(1)) <= 2 else f"√({m.group(1)})", new_text)
        if new_text == text:
            break
        text = new_text

    # 6. Vector / Accents
    text = re.sub(r'\\vec\{([^{}]+)\}', r'\1⃗', text)
    text = re.sub(r'\\bar\{([^{}]+)\}', r'\1̄', text)
    text = re.sub(r'\\hat\{([^{}]+)\}', r'\1̂', text)
    text = re.sub(r'\\overline\{([^{}]+)\}', r'\1̄', text)
    text = re.sub(r'\\mathbf\{([^{}]+)\}', r'\1', text)
    text = re.sub(r'\\mathrm\{([^{}]+)\}', r'\1', text)
    text = re.sub(r'\\text\{([^{}]+)\}', r'\1', text)

    # 7. Superscripts & Subscripts
    text = convert_superscripts(text)
    text = convert_subscripts(text)

    # 8. Symbol replacements
    for latex_sym, unicode_sym in MATH_MAP.items():
        text = text.replace(latex_sym, unicode_sym)

    # 9. Clean remaining unparsed backslashes
    text = re.sub(r'\\([a-zA-Z]+)', r'\1', text)
    text = text.replace("\\", "")

    return re.sub(r'[ \t]+', ' ', text).strip()

def lite_math(text):
    if not text:
        return ""
    return clean_latex_to_unicode(text.replace("$", ""))

def beautify_markdown_math(text):
    if not text:
        return ""

    text = str(text)
    # Normalize line breaks
    text = text.replace("\\\\n", "\n").replace("\\n", "\n").replace(r"\n", "\n")
    text = text.replace("<br>", "\n").replace("<br/>", "\n").replace("\r", "")

    # Strip LaTeX spacing
    text = re.sub(r'\\vspace\{[^}]*\}', '\n', text)
    text = re.sub(r'\\hspace\{[^}]*\}', ' ', text)
    text = text.replace(r"\par", "\n").replace(r"\noindent", "").replace(r"\leavevmode", "").replace("oindent", "")

    # Format Step headers nicely
    def step_repl(match):
        step_num = match.group(1)
        emojis = {"1": "1️⃣", "2": "2️⃣", "3": "3️⃣", "4": "4️⃣", "5": "5️⃣", "6": "6️⃣", "7": "7️⃣", "8": "8️⃣", "9": "9️⃣"}
        emoji = emojis.get(step_num, "▪️")
        return f"\n\n<b>{emoji} Step {step_num}:</b> "

    text = re.sub(r'(?i)\bStep\s*(\d+)[:.-]?\s*', step_repl, text)

    # Process explicit math blocks $$...$$ and inline $...$
    parts_block = text.split('$$')
    for i in range(len(parts_block)):
        if i % 2 == 1:
            cleaned = clean_latex_to_unicode(parts_block[i].strip())
            parts_block[i] = f"<tg-math-block>{cleaned}</tg-math-block>"
        else:
            parts_inline = parts_block[i].split('$')
            for j in range(len(parts_inline)):
                if j % 2 == 1:
                    cleaned = clean_latex_to_unicode(parts_inline[j].strip())
                    parts_inline[j] = f"<tg-math>{cleaned}</tg-math>"
                else:
                    lines = parts_inline[j].split('\n')
                    formatted_lines = []
                    for line in lines:
                        line_str = line.strip()
                        # Auto-detect standalone equation/math line
                        is_math_line = False
                        if line_str and not line_str.startswith("<") and not any(line_str.startswith(f"{e} Step") for e in ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]):
                            if any(cmd in line_str for cmd in ["\\frac", "\\sqrt", "\\vec", "\\int", "\\sum", "^", "_", "°"]) or (("=" in line_str or "±" in line_str or "⇒" in line_str) and any(c.isdigit() or c in "xyzabrkhuv" for c in line_str)):
                                words = line_str.split()
                                if len(words) <= 12 and not any(w.lower() in ["group", "complete", "identify", "substitute", "find", "solve", "calculate", "where", "the", "this", "value", "square", "terms", "both", "sides", "equation", "adding", "together", "right", "side", "squared", "radius"] for w in words):
                                    is_math_line = True

                        if is_math_line:
                            cleaned = clean_latex_to_unicode(line_str)
                            if cleaned:
                                formatted_lines.append(f"<tg-math-block>{cleaned}</tg-math-block>")
                            else:
                                formatted_lines.append(escape_plain_text(line))
                        else:
                            def math_inline_repl(m):
                                math_found = m.group(0)
                                cleaned = clean_latex_to_unicode(math_found)
                                return f"<tg-math>{cleaned}</tg-math>" if cleaned else math_found

                            line_proc = re.sub(r'\\(?:frac|sqrt|vec)\{[^{}]+\}(?:\{[^{}]+\})?|(?:(?:\([a-zA-Z0-9\^_\+\-\*/\s]+\)|[a-zA-Z0-9\^_\+\-\*/]+)\s*=\s*[a-zA-Z0-9\(\)\{\}\[\]\+\-\*/\^=_√πθ\s]+)', math_inline_repl, line)
                            if "\\" in line_proc or "^" in line_proc or "_" in line_proc:
                                line_proc = clean_latex_to_unicode(line_proc)

                            formatted_lines.append(escape_plain_text(line_proc))

                    parts_inline[j] = "\n".join(formatted_lines)
            parts_block[i] = "".join(parts_inline)

    result = "".join(parts_block)
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result.strip()