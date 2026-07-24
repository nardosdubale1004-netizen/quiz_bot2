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
    # Allowed tags are kept unescaped so Telegram renders them as entities
    allowed_tags = [
        "b", "/b", "i", "/i", "u", "/u", "s", "/s", "tg-spoiler", "/tg-spoiler",
        "code", "/code", "pre", "/pre", "a", "/a", "blockquote", "/blockquote",
        "tg-math", "/tg-math", "tg-math-block", "/tg-math-block",
        "h1", "/h1", "h2", "/h2", "h3", "/h3", "h4", "/h4", "h5", "/h5", "h6", "/h6",
        "ul", "/ul", "ol", "/ol", "li", "/li", "table", "/table", "tr", "/tr", "td", "/td",
        "br", "hr", "mark", "/mark", "sub", "/sub", "sup", "/sup"  # Fixed: corrected /superscript to /sup
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
    # Strip Telegram advance rich math tags for plain-text fallback (e.g. native polls)
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

def beautify_markdown_math(text):
    if not text:
        return ""

    text = str(text)
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

    # --- LATEX DELIMITER NORMALIZATION BRIDGE ---
    # Automatically map standard LaTeX bracket block styles to Markdown dollar style
    result = result.replace(r'\[', '$$').replace(r'\]', '$$')
    result = result.replace(r'\(', '$').replace(r'\)', '$')

    # Convert dollar block/inline math delimiters to native Telegram rich tags
    parts_block = result.split('$$')
    for i in range(len(parts_block)):
        if i % 2 == 1:
            parts_block[i] = f"\n<tg-math-block>{parts_block[i].strip()}</tg-math-block>\n"
        else:
            parts_inline = parts_block[i].split('$')
            for j in range(len(parts_inline)):
                if j % 2 == 1:
                    parts_inline[j] = f"<tg-math>{parts_inline[j].strip()}</tg-math>"
                else:
                    parts_inline[j] = escape_plain_text(parts_inline[j])
            parts_block[i] = "".join(parts_inline)

    result = "".join(parts_block)
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result.strip()