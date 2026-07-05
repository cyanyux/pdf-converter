"""Unit tests for probe routing rules and the already-searchable predicate.

Builds tiny PDFs with pymupdf from a page spec: a "digital" page carries a real text layer; a
"raster" page is image-only (0 extractable chars); "raster:N" is an image page carrying N stray
chars (< the digital threshold, e.g. a scanned page-number/watermark). Docling is never loaded.
"""

from pathlib import Path
from typing import Any

import pymupdf as fitz

from worker import probe


def _add_digital(doc: fitz.Document) -> None:
    """Append a digital page whose text layer has well over DIGITAL_MIN_CHARS non-ws chars."""
    page = doc.new_page()
    page.insert_text((72, 72), "PDFConverterDigitalText" * 8, fontsize=8)


def _add_raster(doc: fitz.Document, stray_chars: int = 0) -> None:
    """Append an image-only page, optionally with `stray_chars` of stray text (< threshold)."""
    page = doc.new_page()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 60, 60), False)
    pix.clear_with(220)
    page.insert_image(fitz.Rect(72, 72, 172, 172), pixmap=pix)
    if stray_chars:
        page.insert_text((72, 200), "X" * stray_chars, fontsize=8)


# A minimal, valid 0-page PDF. pymupdf refuses to save a doc with no pages, so an empty-spec
# fixture is written from these raw bytes instead (it still opens cleanly with len(doc)==0).
_EMPTY_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    b"2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
    b"trailer\n<< /Size 3 /Root 1 0 R >>\n%%EOF\n"
)


def _make_pdf(tmp_path: Path, name: str, spec: list[str]) -> str:
    """Build a PDF from a page spec: 'digital', 'raster', or 'raster:N' (N stray chars)."""
    if not spec:
        path = tmp_path / name
        path.write_bytes(_EMPTY_PDF)
        return str(path)
    doc = fitz.open()
    for kind in spec:
        if kind == "digital":
            _add_digital(doc)
        elif kind.startswith("raster"):
            _, _, n = kind.partition(":")
            _add_raster(doc, int(n) if n else 0)
        else:
            raise ValueError(kind)
    path = tmp_path / name
    doc.save(path)
    doc.close()
    return str(path)


def test_classify_pure_digital(tmp_path: Path) -> None:
    pdf = _make_pdf(tmp_path, "digital.pdf", ["digital", "digital"])
    info = probe.classify_pages(pdf)
    assert info["total"] == 2
    assert info["digital_pages"] == 2
    assert info["raster_pages"] == 0
    assert all(p["digital"] for p in info["pages"])


def test_classify_pure_raster(tmp_path: Path) -> None:
    pdf = _make_pdf(tmp_path, "raster.pdf", ["raster", "raster"])
    info = probe.classify_pages(pdf)
    assert info["total"] == 2
    assert info["digital_pages"] == 0
    assert info["raster_pages"] == 2


def test_route_markdown_digital_to_docling(tmp_path: Path) -> None:
    pdf = _make_pdf(tmp_path, "d.pdf", ["digital", "digital", "digital"])
    assert probe.route_markdown(pdf) == "docling"


def test_route_markdown_scanned_to_vl(tmp_path: Path) -> None:
    pdf = _make_pdf(tmp_path, "s.pdf", ["raster", "raster"])
    assert probe.route_markdown(pdf) == "vl"


def test_route_markdown_mixed_below_ratio_to_vl(tmp_path: Path) -> None:
    # 2 digital + 2 empty-raster: ratio 0.5 < 0.9 -> VL (the scanned pages need OCR).
    pdf = _make_pdf(tmp_path, "m.pdf", ["digital", "digital", "raster", "raster"])
    assert probe.route_markdown(pdf) == "vl"


def test_route_markdown_mostly_digital_with_empty_raster_to_docling(tmp_path: Path) -> None:
    # 9 digital + 1 truly-empty raster: ratio 0.9 AND the raster page carries no text -> docling
    # (do_ocr=False loses nothing on a content-free page).
    pdf = _make_pdf(tmp_path, "md.pdf", ["digital"] * 9 + ["raster"])
    assert probe.route_markdown(pdf) == "docling"


def test_route_markdown_low_ratio_with_partial_text_to_vl(tmp_path: Path) -> None:
    # 1 digital + 1 raster-with-partial-text: ratio 0.5 < 0.9 -> VL regardless of the raster text.
    pdf = _make_pdf(tmp_path, "lp.pdf", ["digital", "raster:40"])
    assert probe.route_markdown(pdf) == "vl"


def test_route_markdown_empty_pdf_to_vl(tmp_path: Path) -> None:
    # An empty (0-page) PDF routes to VL, whose pipeline raises the real "empty PDF" error.
    pdf = _make_pdf(tmp_path, "empty.pdf", [])
    assert probe.route_markdown(pdf) == "vl"


def test_is_already_searchable_all_digital(tmp_path: Path) -> None:
    pdf = _make_pdf(tmp_path, "all.pdf", ["digital", "digital"])
    assert probe.is_already_searchable(pdf) is True


def test_is_already_searchable_any_raster_false(tmp_path: Path) -> None:
    pdf = _make_pdf(tmp_path, "mix.pdf", ["digital", "raster"])
    assert probe.is_already_searchable(pdf) is False


def test_is_already_searchable_empty_false(tmp_path: Path) -> None:
    pdf = _make_pdf(tmp_path, "e.pdf", [])
    assert probe.is_already_searchable(pdf) is False


def test_classify_pages_reports_fffd_key(tmp_path: Path) -> None:
    # classify_pages always reports a document-wide U+FFFD count (0 for a clean text layer); the
    # per-page detail carries the same field. A broken /ToUnicode CMap can't be synthesized with
    # pymupdf's base-14 writer (it can't encode U+FFFD), so the ratio rule is exercised on
    # classify-shaped dicts below.
    pdf = _make_pdf(tmp_path, "clean.pdf", ["digital"])
    info = probe.classify_pages(pdf)
    assert info["fffd"] == 0
    assert all("fffd" in p for p in info["pages"])


# --- already_searchable predicate (pure, over classify-result dicts) ---
# We can't easily synthesize a broken-ToUnicode PDF, so exercise the FFFD rule on hand-built
# classify_pages-shaped dicts. The predicate must reject mojibake (fffd/text_chars > 0.05).


def _info(pages: list[dict[str, int]]) -> dict[str, Any]:
    """Build a classify_pages-shaped result from per-page {chars, fffd} (all pages digital)."""
    full = [{"index": i, "chars": p["chars"], "fffd": p.get("fffd", 0), "digital": True} for i, p in enumerate(pages)]
    return {
        "total": len(full),
        "digital_pages": len(full),
        "raster_pages": 0,
        "fffd": sum(p["fffd"] for p in full),
        "pages": full,
    }


def test_already_searchable_predicate_all_digital_clean() -> None:
    assert probe.already_searchable(_info([{"chars": 100}, {"chars": 100}])) is True


def test_already_searchable_predicate_rejects_mojibake() -> None:
    # 20 of 100 chars are U+FFFD -> ratio 0.2 > 0.05 -> reject, fall through to OCR.
    assert probe.already_searchable(_info([{"chars": 100, "fffd": 20}])) is False


def test_already_searchable_predicate_tolerates_low_fffd() -> None:
    # 3 of 200 chars are U+FFFD -> ratio 0.015 <= 0.05 -> still already-searchable.
    assert probe.already_searchable(_info([{"chars": 100, "fffd": 2}, {"chars": 100, "fffd": 1}])) is True


def test_already_searchable_predicate_raster_false() -> None:
    info = _info([{"chars": 100}])
    info["raster_pages"] = 1
    assert probe.already_searchable(info) is False


def test_already_searchable_predicate_empty_false() -> None:
    assert probe.already_searchable(_info([])) is False
