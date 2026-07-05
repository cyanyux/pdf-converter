"""Unit tests for the child-side markdown-sibling co-production rule.

Exercises child._co_produce_markdown_sibling (pure decision function) without loading VL — the
sibling's own requested engine takes precedence over the probe.
"""

from pathlib import Path

import pymupdf as fitz

from worker import child

SCHEMA = Path(__file__).resolve().parents[2] / "db" / "schema.sql"


def _digital_pdf(tmp_path: Path, name: str, pages: int = 2) -> str:
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), "PDFConverterDigitalText" * 6, fontsize=8)
    path = tmp_path / name
    doc.save(path)
    doc.close()
    return str(path)


def _scanned_pdf(tmp_path: Path, name: str) -> str:
    doc = fitz.open()
    page = doc.new_page()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 60, 60), False)
    pix.clear_with(220)
    page.insert_image(fitz.Rect(72, 72, 172, 172), pixmap=pix)
    path = tmp_path / name
    doc.save(path)
    doc.close()
    return str(path)


def test_sibling_engine_vl_is_co_produced(tmp_path: Path) -> None:
    # A markdown sibling pinned engine='vl' is co-produced by the VL word pass even though the
    # (digital) upload would route to docling under 'auto' — the sibling's engine wins.
    pdf = _digital_pdf(tmp_path, "d.pdf")
    sib = {"id": "md", "mode": "markdown", "engine": "vl"}
    assert child._co_produce_markdown_sibling(sib, pdf) is True


def test_sibling_engine_docling_is_declined(tmp_path: Path) -> None:
    # A markdown sibling pinned engine='docling' is DECLINED by the VL word pass — it gets its own
    # docling child when claimed — even on a scanned upload that would route to VL under 'auto'.
    pdf = _scanned_pdf(tmp_path, "s.pdf")
    sib = {"id": "md", "mode": "markdown", "engine": "docling"}
    assert child._co_produce_markdown_sibling(sib, pdf) is False


def test_sibling_engine_auto_probes(tmp_path: Path) -> None:
    # engine='auto' (or missing) defers to the probe: a digital upload routes to docling -> declined;
    # a scanned one routes to VL -> co-produced.
    digital = _digital_pdf(tmp_path, "auto_d.pdf")
    scanned = _scanned_pdf(tmp_path, "auto_s.pdf")
    assert child._co_produce_markdown_sibling({"id": "m", "mode": "markdown", "engine": "auto"}, digital) is False
    assert child._co_produce_markdown_sibling({"id": "m", "mode": "markdown"}, scanned) is True  # missing -> auto


def test_sibling_probe_failure_declines(tmp_path: Path) -> None:
    # An 'auto' sibling whose upload can't be probed declines — never co-produce with a guessed
    # engine; defer to the sibling's own claim.
    assert child._co_produce_markdown_sibling({"id": "m", "mode": "markdown"}, "/no/such/file.pdf") is False
