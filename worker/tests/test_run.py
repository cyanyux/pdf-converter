"""Unit tests for supervisor-side routing + the already-searchable short-circuit contract.

Exercises run.pick_family and run.complete_already_searchable at the store level (no model
child, no GPU). Docling/VL are never loaded.
"""

import json
from pathlib import Path

import pymupdf as fitz
import pytest

from worker import config, run
from worker.store import Store

SCHEMA = Path(__file__).resolve().parents[2] / "db" / "schema.sql"


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "jobs.db", SCHEMA)


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


def test_pick_family_pdf_and_word_fixed(tmp_path: Path) -> None:
    assert run.pick_family({"id": "a", "mode": "pdf", "upload_path": None}) == "ppocr"
    assert run.pick_family({"id": "b", "mode": "word", "upload_path": None}) == "vl"


def test_pick_family_markdown_digital_docling(tmp_path: Path) -> None:
    pdf = _digital_pdf(tmp_path, "d.pdf")
    assert run.pick_family({"id": "m", "mode": "markdown", "upload_path": pdf}) == "docling"


def test_pick_family_markdown_scanned_vl(tmp_path: Path) -> None:
    pdf = _scanned_pdf(tmp_path, "s.pdf")
    assert run.pick_family({"id": "m", "mode": "markdown", "upload_path": pdf}) == "vl"


def test_pick_family_markdown_no_upload_vl(tmp_path: Path) -> None:
    assert run.pick_family({"id": "m", "mode": "markdown", "upload_path": None}) == "vl"


def test_pick_family_markdown_missing_engine_is_auto(tmp_path: Path) -> None:
    # A job dict without an `engine` key routes as 'auto' (the column default): probe decides.
    pdf = _digital_pdf(tmp_path, "auto.pdf")
    assert run.pick_family({"id": "m", "mode": "markdown", "upload_path": pdf}) == "docling"


def test_pick_family_markdown_engine_vl_skips_probe(tmp_path: Path) -> None:
    # engine='vl' pins the VL family directly — no probe. Use a DIGITAL pdf that would route to
    # 'docling' under 'auto', proving the engine wins over the probe verdict.
    pdf = _digital_pdf(tmp_path, "pinvl.pdf")
    assert run.pick_family({"id": "m", "mode": "markdown", "engine": "vl", "upload_path": pdf}) == "vl"


def test_pick_family_markdown_engine_docling_qualifying(tmp_path: Path) -> None:
    # engine='docling' on a born-digital PDF is verified eligible -> the docling family.
    pdf = _digital_pdf(tmp_path, "pind.pdf")
    assert run.pick_family({"id": "m", "mode": "markdown", "engine": "docling", "upload_path": pdf}) == "docling"


def test_pick_family_markdown_engine_docling_ineligible_raises(tmp_path: Path) -> None:
    # engine='docling' on a scanned/raster PDF (routes to VL) must NOT silently fall back — it
    # raises DoclingIneligible with the exact user-facing message.
    pdf = _scanned_pdf(tmp_path, "pind_scan.pdf")
    with pytest.raises(run.DoclingIneligible) as exc:
        run.pick_family({"id": "m", "mode": "markdown", "engine": "docling", "upload_path": pdf})
    assert str(exc.value) == run.DOCLING_INELIGIBLE_MSG


def test_pick_family_markdown_engine_docling_no_upload_raises(tmp_path: Path) -> None:
    # A pinned docling job with no readable upload is ineligible too (never falls back to VL).
    with pytest.raises(run.DoclingIneligible):
        run.pick_family({"id": "m", "mode": "markdown", "engine": "docling", "upload_path": None})


def test_docling_ineligible_fails_job_and_cleans_upload(tmp_path: Path) -> None:
    # The claim-loop failure path for a pinned engine='docling' markdown job over a scanned PDF:
    # pick_family raises DoclingIneligible, and the loop records set_error(exact message) +
    # cleanup_upload (mirroring the already-searchable short-circuit's error handling). Drive that
    # exact sequence against the store and assert the observable end state.
    s = _store(tmp_path)
    pdf = _scanned_pdf(tmp_path, "scan.pdf")
    now = 1.0
    s.conn.execute(
        "INSERT INTO jobs(id,mode,filename,locale,status,engine,upload_path,created_at,updated_at) "
        "VALUES('di','markdown','orig.pdf','en','processing','docling',?,?,?)",
        (pdf, now, now),
    )
    job = {
        "id": "di",
        "mode": "markdown",
        "filename": "orig.pdf",
        "locale": "en",
        "engine": "docling",
        "upload_path": pdf,
    }

    with pytest.raises(run.DoclingIneligible) as exc:
        run.pick_family(job)
    # ... which the loop turns into set_error + cleanup_upload:
    s.set_error(job["id"], str(exc.value))
    run.cleanup_upload(s, job)

    assert s.status_of("di") == "error"
    row = s.conn.execute("SELECT error FROM jobs WHERE id='di'").fetchone()
    assert row["error"] == run.DOCLING_INELIGIBLE_MSG
    assert not Path(pdf).exists()  # the sole reference is gone -> upload unlinked


def test_already_searchable_short_circuit_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A mode=pdf job over an all-digital PDF completes WITHOUT a child: the original upload is
    # copied to outputs/<id>/<id>.pdf and the result carries engine='none' +
    # notice='already_searchable', with the normal downloadId/originalName/totalPages fields.
    outputs = tmp_path / "outputs"
    monkeypatch.setattr(config, "OUTPUTS_DIR", outputs)
    s = _store(tmp_path)
    pdf = _digital_pdf(tmp_path, "up.pdf", pages=3)
    now = 1.0
    s.conn.execute(
        "INSERT INTO jobs(id,mode,filename,locale,status,upload_path,created_at,updated_at) "
        "VALUES('sc','pdf','orig.pdf','en','processing',?,?,?)",
        (pdf, now, now),
    )

    job = {"id": "sc", "mode": "pdf", "filename": "orig.pdf", "locale": "en", "upload_path": pdf}
    run.complete_already_searchable(s, job, total=3)

    row = s.conn.execute("SELECT status, result_json, download_id FROM jobs WHERE id='sc'").fetchone()
    assert row["status"] == "done"
    assert row["download_id"] == "sc"
    res = json.loads(row["result_json"])
    assert res["engine"] == "none"
    assert res["notice"] == "already_searchable"
    assert res["downloadId"] == "sc"
    assert res["originalName"] == "orig.pdf"
    assert res["totalPages"] == 3
    # The artifact is the original upload with a clean /Title stamped in (incremental save, so it
    # is NOT byte-identical — but it opens and its text content is preserved). The title is set
    # from the job filename stem, mirroring child.run_ppocr.
    out_pdf = outputs / "sc" / "sc.pdf"
    assert out_pdf.is_file()
    doc = fitz.open(out_pdf)
    try:
        assert (doc.metadata or {})["title"] == "orig"  # /Title stamped from filename stem
        assert doc.page_count == 3
    finally:
        doc.close()
