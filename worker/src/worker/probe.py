"""Per-page classification + backend routing for a source PDF (pymupdf, CPU-only).

A page is "digital" when its extractable text layer carries real content
(>= DIGITAL_MIN_CHARS non-whitespace characters); otherwise it is "raster" —
a scanned/image page with no usable text layer. This drives two decisions the
supervisor makes at claim time WITHOUT loading any model:

  - route_markdown: born-digital PDFs get faithful markdown from Docling (the
    text layer is ground truth); scanned PDFs go through rasterize + PaddleOCR-VL.
  - is_already_searchable: a PDF whose every page already has a text layer needs
    no OCR at all — the searchable-PDF request short-circuits to a plain copy.

Import-light on purpose: only pymupdf, which the worker already depends on. No
paddle / torch / docling here, so the supervisor and the unit tests can call it.
"""

from __future__ import annotations

from typing import Any

import pymupdf as fitz

# A page needs at least this many non-whitespace characters in its text layer to count as
# "digital". Chosen to clear stray artifacts (a lone page number, a scanner watermark string)
# while admitting any page with a real paragraph of extractable text.
DIGITAL_MIN_CHARS = 50

# Fraction of pages that must be digital for the whole PDF to route to Docling. A handful of
# scanned inserts in an otherwise born-digital document would lose their content under Docling
# (do_ocr=False), so we additionally require every non-digital page to be genuinely empty of
# text (see route_markdown) — a mixed doc with content-bearing raster pages falls back to VL.
DIGITAL_RATIO = 0.9

# Max tolerated ratio of U+FFFD replacement chars to total text chars before we treat the text
# layer as mojibake (a broken /ToUnicode CMap) and refuse the already-searchable short-circuit.
# A text layer full of � is unusable — better to fall through to real OCR than ship garbage.
MOJIBAKE_RATIO = 0.05


def _page_text_stats(page: fitz.Page) -> tuple[int, int]:
    """(non-whitespace chars, U+FFFD replacement chars) in a page's extractable text layer."""
    text = page.get_text("text") or ""
    chars = sum(1 for ch in text if not ch.isspace())
    fffd = text.count("�")
    return chars, fffd


def classify_pages(pdf: str) -> dict[str, Any]:
    """Per-page digital/raster classification for a PDF.

    Returns counts plus per-page detail: {total, digital_pages, raster_pages, fffd,
    pages: [{index, chars, fffd, digital}, ...]}. `fffd` is the document-wide count of U+FFFD
    replacement chars (a broken /ToUnicode CMap yields these). An empty or encrypted PDF yields
    total=0 (the caller's own pipeline surfaces the real error message).
    """
    pages: list[dict[str, Any]] = []
    doc = fitz.open(pdf)
    try:
        if doc.is_encrypted:
            return {"total": 0, "digital_pages": 0, "raster_pages": 0, "fffd": 0, "pages": []}
        for i in range(len(doc)):
            chars, fffd = _page_text_stats(doc[i])
            pages.append({"index": i, "chars": chars, "fffd": fffd, "digital": chars >= DIGITAL_MIN_CHARS})
    finally:
        doc.close()
    digital_pages = sum(1 for p in pages if p["digital"])
    return {
        "total": len(pages),
        "digital_pages": digital_pages,
        "raster_pages": len(pages) - digital_pages,
        "fffd": sum(p["fffd"] for p in pages),
        "pages": pages,
    }


def route_markdown(pdf: str) -> str:
    """Route a markdown job: 'docling' (digital, text-faithful) or 'vl' (rasterize + OCR).

    Docling iff the PDF is overwhelmingly digital (digital_pages/total >= DIGITAL_RATIO)
    AND every non-digital page is genuinely empty (< DIGITAL_MIN_CHARS chars) — a raster page
    carrying no text content can't lose anything under do_ocr=False. Any content-bearing page
    below the digital bar, an empty PDF, or a low digital ratio falls back to VL, whose
    rasterize+OCR pass reads image-only pages.
    """
    info = classify_pages(pdf)
    total = info["total"]
    if total == 0:
        return "vl"
    ratio = info["digital_pages"] / total
    every_raster_empty = all(p["digital"] or p["chars"] < DIGITAL_MIN_CHARS for p in info["pages"])
    if ratio >= DIGITAL_RATIO and every_raster_empty:
        return "docling"
    return "vl"


def already_searchable(info: dict[str, Any]) -> bool:
    """Pure predicate over a classify_pages result: is this PDF already fully searchable?

    True iff EVERY page already has a real text layer (all pages digital) AND the text layer is
    not mojibake. A searchable-PDF request for such an input needs no OCR: the run.py short-circuit
    copies the original upload as the result. Any raster page (or an empty PDF) returns False so
    the normal PP-OCRv6 pass runs.

    Mojibake guard: a document whose ratio of U+FFFD replacement chars to text chars exceeds
    MOJIBAKE_RATIO has a broken /ToUnicode CMap — its text layer is unusable garbage, so we return
    False and fall through to real OCR. Erring toward OCR is the safe direction.
    """
    total = info["total"]
    if total == 0 or info["raster_pages"] != 0:
        return False
    text_chars = sum(p["chars"] for p in info["pages"])
    mojibake = text_chars > 0 and info["fffd"] / text_chars > MOJIBAKE_RATIO
    return not mojibake


def is_already_searchable(pdf: str) -> bool:
    """Thin wrapper: classify the PDF once, then apply the already_searchable predicate."""
    return already_searchable(classify_pages(pdf))
