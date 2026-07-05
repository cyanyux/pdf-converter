"""Text post-processing shared by the OCR and doc-parse pipelines."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from .i18n import normalize_locale

# PaddleOCR-VL emits inline math/units as LaTeX (e.g. `$ ^{\circ} $C`, `$\pm0.3$`, `$ 8mm^{2} $`).
# In a .docx / .md that renders as literal source, so convert it to Unicode. pylatexenc (a
# transitive dep of paddleocr[doc2md]) does the structural parse — braces, \frac, \sqrt,
# environments, every delimiter form, unknown commands — WITHOUT the silent content drops and
# prefix collisions a hand-rolled command table has. We add a Unicode super/subscript pass on
# top, because pylatexenc renders `x^{2}` as ASCII `x^2` and this is a units document.
_SUP = str.maketrans("0123456789+-=()ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ")
_SUB = str.maketrans("0123456789+-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎")
_SUP_RE = re.compile(r"\^\{([^{}]*)\}|\^(-?\d+|.)")
_SUB_RE = re.compile(r"_\{([^{}]*)\}|_(-?\d+|.)")

# Only BALANCED math spans are converted. A lone `$` (currency, e.g. "$5"/"US$5.5") never
# matches, so it is left untouched rather than swallowed as an unterminated math-mode start —
# and, having no leftover delimiter, the result is idempotent under a second pass.
_MATH_SPAN = re.compile(
    r"\$\$.+?\$\$"  # display $$...$$
    r"|\$[^$\n]+?\$"  # inline $...$
    r"|\\\(.+?\\\)"  # \( ... \)
    r"|\\\[.+?\\\]",  # \[ ... \]
    re.DOTALL,
)


@lru_cache(maxsize=1)
def _latex_converter() -> Any:
    """pylatexenc LaTeX->text converter, built once (import is lazy like OpenCC)."""
    from pylatexenc.latex2text import (
        LatexNodes2Text,
        MacroTextSpec,
        get_default_latex_context_db,
    )

    ctx = get_default_latex_context_db()
    # In a units document `^{\circ}C` means degrees; the default renders \circ as the ring op ∘.
    ctx.add_context_category(
        "units-fixups",
        prepend=True,
        macros=[
            MacroTextSpec("circ", "\N{DEGREE SIGN}"),
            MacroTextSpec("degree", "\N{DEGREE SIGN}"),
            MacroTextSpec("times", "\N{MULTIPLICATION SIGN}"),
            MacroTextSpec("pm", "\N{PLUS-MINUS SIGN}"),
        ],
    )
    return LatexNodes2Text(latex_context=ctx, math_mode="text", strict_latex_spaces=False, keep_comments=False)


def _script(m: re.Match[str], table: dict[int, int], prefix: str) -> str:
    inner = m.group(1) if m.group(1) is not None else m.group(2)
    # Only lift to Unicode when EVERY char has a super/subscript form; otherwise keep a readable
    # `^`/`_` marker (e.g. `V_{max}` -> `V_max`) rather than dropping the script.
    if inner and all(ord(c) in table for c in inner):
        return inner.translate(table)
    return prefix + inner


def _is_verbatim_currency(span: str) -> bool:
    # `$` is ambiguous between a math delimiter and currency. An INLINE `$...$` span with no LaTeX
    # command (`\`) and no sub/superscript/grouping (`^` `_` `{` `}`) carries no math structure.
    # Two cases:
    #   * VL wraps bare operators/values in SPACE-PADDED delimiters ("$ ( $", "$ 1920*1080 $")
    #     when it styles a formula split around CJK words — that IS math markup; strip it (handled
    #     in _render_span), or the literal "$ ( $" ships in the docx/markdown.
    #   * Anything else is two currency markers mis-paired across ordinary text ("$5 to $10",
    #     "$5 至 US$" — the digit hugs the `$`), and converting would strip the `$` and fuse the
    #     amounts ("5 to10"). Leave it verbatim.
    # (`\(...\)`/`\[...\]` and display `$$...$$` are unambiguous math, so they are never verbatim.)
    if not (span.startswith("$") and not span.startswith("$$") and not re.search(r"[\\^_{}]", span)):
        return False
    inner = span[1:-1]
    # A space-padded structure-less span is VL formula markup, not currency -> not verbatim.
    return not (len(inner) >= 2 and inner[0].isspace() and inner[-1].isspace())


def _render_span(span: str) -> str:
    if _is_verbatim_currency(span):
        return span
    if span.startswith("$") and not span.startswith("$$") and not re.search(r"[\\^_{}]", span):
        # Structure-less but space-padded -> VL markup: strip the delimiters (see _is_verbatim_currency).
        return span[1:-1].strip()
    try:
        out: str = _latex_converter().latex_to_text(span)
    except Exception:
        return span  # never worse than the raw source
    out = _SUP_RE.sub(lambda mm: _script(mm, _SUP, "^"), out)
    out = _SUB_RE.sub(lambda mm: _script(mm, _SUB, "_"), out)
    out = out.replace("^\N{DEGREE SIGN}", "\N{DEGREE SIGN}")  # a raised degree ring IS just °
    return re.sub(r"\s+", " ", out).strip()


def latex_to_unicode(text: str) -> str:
    """Render VL's inline LaTeX math/units to Unicode so it doesn't ship as literal source.

    Scans left-to-right rather than a single re.sub so a mis-paired currency `$` cannot eat a
    following real formula's opening `$`. When a `$...$` candidate is judged currency and left
    verbatim, the scan resumes from just AFTER its OPENING `$` (not its close), so the formula's
    delimiter gets a fresh chance to pair. Example: "cost $5, area $x^{2}$" — the "$5, area $"
    inline candidate is structure-less currency, so instead of consuming it (which would leave
    the real formula's `$` unpaired and ship "x^{2}" literally), we advance one char and re-match
    "$x^{2}$" as the math span it is.
    """
    if "$" not in text and "\\(" not in text and "\\[" not in text:
        return text
    out: list[str] = []
    pos = 0
    n = len(text)
    while pos < n:
        m = _MATH_SPAN.search(text, pos)
        if m is None:
            out.append(text[pos:])
            break
        span = m.group(0)
        # An inline `$...$` candidate that is verbatim currency must not swallow the region: emit
        # everything up to and INCLUDING its opening `$`, then resume the scan one char in so a
        # later real formula's `$` can pair. Display `$$`, `\(...\)`, `\[...\]` never take this path.
        if span.startswith("$") and not span.startswith("$$") and _is_verbatim_currency(span):
            out.append(text[pos : m.start() + 1])
            pos = m.start() + 1
            continue
        out.append(text[pos : m.start()])
        out.append(_render_span(span))
        pos = m.end()
    return "".join(out)


# PaddleOCR-VL sometimes emits a stray space between two Han characters (e.g. "總務科 辦公室",
# "中 華 民 國") — a recognition artifact, since written Chinese has no inter-character spacing.
# The lookbehind/lookahead both require a Han ideograph, so this can NEVER touch a CJK<->Latin or
# CJK<->digit boundary ("Onvif 攝影機", "24 VDC", "±5%"), a full-width "| " markdown table pipe,
# or anything but a space/tab wedged strictly between two Han glyphs.
_CJK_SPACE = re.compile(r"(?<=[㐀-鿿豈-﫿])[ \t]+(?=[㐀-鿿豈-﫿])")


def strip_cjk_spaces(text: str) -> str:
    """Drop stray spaces VL inserts between two Han characters (never CJK<->Latin/digit)."""
    return _CJK_SPACE.sub("", text)


def strip_cjk_spaces_across(texts: list[str]) -> list[str] | None:
    """strip_cjk_spaces over the CONCATENATION of adjacent runs, mapped back per run.

    A Han<->Han space that sits at a run BOUNDARY ("…繳交 " | "維護…" — PaddleX splits a
    paragraph into a <w:t> per style change) is invisible to the per-run function. The regex
    only DELETES characters, so finding its match spans in the joined string and dropping those
    global indices from whichever run they live in is an exact remap. Returns None if unchanged.
    """
    joined = "".join(texts)
    dead: set[int] = set()
    for m in _CJK_SPACE.finditer(joined):
        dead.update(range(m.start(), m.end()))
    if not dead:
        return None
    out: list[str] = []
    pos = 0
    for t in texts:
        out.append("".join(c for j, c in enumerate(t, start=pos) if j not in dead))
        pos += len(t)
    return out


@lru_cache(maxsize=1)
def _s2tw() -> Any:
    """OpenCC Simplified->Traditional (Taiwan variants), built once.

    Phrase-aware and idempotent, so context-dependent characters convert correctly
    (`范圍`->`範圍` but the surname `范仲淹` and `批准` are left alone) — unlike a static
    one-to-one table, which had to skip ~170 ambiguous characters and left them Simplified.
    's2tw' (not 's2twp') keeps it character-level: it fixes the script and normalizes to
    Taiwan-standard glyphs (峰/裡/為) without localizing vocabulary or changing word lengths.
    """
    import opencc

    return opencc.OpenCC("s2tw")


def fix_ocr_text(text: str, locale: str | None = None) -> str:
    """Correct mis-recognized Simplified characters to Traditional (Taiwan).

    Only applied for zh-TW (the default). zh-CN and en are returned as-is, so
    Simplified-Chinese users keep native output.
    """
    if not text or normalize_locale(locale) != "zh-TW":
        return text
    converted: str = _s2tw().convert(text)
    return converted
