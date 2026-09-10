"""Model output is untrusted text. Render a small Markdown subset, sanitise
everything else, and keep LaTeX intact on the way through.

The LaTeX part matters more than it looks. Markdown eats maths: the underscores
in `z_{\\ell s}` become emphasis, `*` becomes a bullet, and backslashes get
swallowed, so by the time it reaches the browser the formula is already broken
and no client-side renderer can recover it. So maths is lifted out first,
replaced by inert placeholders, and put back after sanitising — as escaped text
inside a span that the client hands to KaTeX.
"""

import re

import bleach
import markdown as md

ALLOWED_TAGS = [
    "p", "br", "strong", "em", "code", "pre", "blockquote",
    "ul", "ol", "li", "h3", "h4", "h5", "hr", "table", "thead",
    "tbody", "tr", "th", "td", "a",
]
ALLOWED_ATTRS = {"a": ["href", "title", "rel", "target"]}

# Alphanumeric so Markdown leaves it completely alone.
PLACEHOLDER = "zzmathzz{}zz"
PLACEHOLDER_RE = re.compile(r"zzmathzz(\d+)zz")

# Ordered: longest delimiters first, so $$ is not read as two $.
MATH_PATTERNS = [
    (re.compile(r"\\\[(.+?)\\\]", re.S), True),    # \[ ... \]
    (re.compile(r"\$\$(.+?)\$\$", re.S), True),    # $$ ... $$
    (re.compile(r"\\\((.+?)\\\)", re.S), False),   # \( ... \)
    (re.compile(r"(?<![\w$])\$([^$\n]+?)\$(?![\w$])"), False),  # $ ... $
]

# Code must be left alone: a dollar sign in a shell snippet is not maths.
CODE_RE = re.compile(r"(```.*?```|~~~.*?~~~|`[^`\n]+`)", re.S)

CURRENCY_RE = re.compile(r"^\s*[\d.,]+\s*$")


def _looks_like_money(tex):
    """`$4.50` is not a formula. Require something mathematical in there."""
    return CURRENCY_RE.match(tex) is not None


def _extract_math(text, store):
    """Replace maths with placeholders, outside code spans only."""
    parts = CODE_RE.split(text)
    for index, part in enumerate(parts):
        if index % 2:  # odd chunks are the code spans themselves
            continue
        for pattern, display in MATH_PATTERNS:
            def swap(match, display=display):
                tex = match.group(1).strip()
                if not tex or (not display and _looks_like_money(tex)):
                    return match.group(0)
                store.append((tex, display))
                return PLACEHOLDER.format(len(store) - 1)

            part = pattern.sub(swap, part)
        parts[index] = part
    return "".join(parts)


def _restore_math(html, store):
    def swap(match):
        try:
            tex, display = store[int(match.group(1))]
        except (ValueError, IndexError):
            return match.group(0)
        # The TeX is escaped: KaTeX receives it as text, never as markup.
        return (
            '<span class="math" data-display="{}">{}</span>'.format(
                "1" if display else "0",
                bleach.clean(tex, tags=[], strip=True),
            )
        )

    return PLACEHOLDER_RE.sub(swap, html)


def render(text: str) -> str:
    store = []
    protected = _extract_math(text or "", store)

    html = md.markdown(
        protected,
        extensions=["fenced_code", "tables", "sane_lists", "nl2br"],
        output_format="html",
    )
    clean = bleach.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, strip=True)
    linked = bleach.linkify(clean, callbacks=[_safe_link])
    return _restore_math(linked, store)


def _safe_link(attrs, new=False):
    attrs[(None, "rel")] = "nofollow noopener noreferrer"
    attrs[(None, "target")] = "_blank"
    return attrs
