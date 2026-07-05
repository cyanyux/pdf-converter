"""Digital-markdown pipeline (Docling, CPU): born-digital PDF -> Markdown.

Used when probe.route_markdown routes a markdown job to 'docling' (the PDF has a
faithful text layer). Unlike the VL path this is BYTE-FAITHFUL: it emits Docling's
extraction verbatim — NO s2tw / fix_ocr_text / character normalization — because the
text layer is ground truth and the whole point is exact characters.

Output matches the VL save_markdown contract exactly so every downstream consumer
(server download, SPA) is unchanged:
  outputs/<download_id>/<download_id>.md
  outputs/<download_id>/imgs/<file>            (extracted picture images)
  markdown image refs rewritten to relative `imgs/<file>`

Docling (and its torch) are imported lazily inside run_digital_markdown so the
supervisor, the VL/PP-OCR children, and the unit tests never load them. The child
that runs this path is booted with the GPU hidden (CUDA_VISIBLE_DEVICES="") so it
can never touch VRAM.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pymupdf as fitz

# docparse's module-level imports are numpy/pymupdf/postprocess (no paddle), so importing these
# helpers here does NOT pull paddle at import time — the digital path stays import-light.
from .docparse import list_images, reset_output_dir
from .i18n import msg

log = logging.getLogger("worker.docparse_digital")

ProgressCb = Callable[[int, int, str, str], None]
CancelCb = Callable[[], bool]

# Matches a Markdown image ref `![alt](path)` so we can rewrite Docling's absolute artifact
# paths to the relative `imgs/<basename>` the contract requires.
_MD_IMG = re.compile(r"(!\[[^\]]*\]\()([^)]+)(\))")


def _build_converter(on_page_done: Callable[[int], None]) -> Any:
    """Docling DocumentConverter for born-digital PDFs (text-faithful, no OCR).

    do_ocr=False (trust the text layer), TableFormer ACCURATE mode, and
    generate_picture_images=True so picture crops can be written to imgs/.

    Docling's converter has no progress callback, so per-page progress is grafted on via
    pipeline_cls: a StandardPdfPipeline subclass whose _release_page_resources — the assemble
    stage's per-item postprocess hook, i.e. the moment a page finishes the last pipeline stage —
    also reports the page. on_page_done runs on the assemble-stage thread, NOT the caller's
    thread, and must never raise (an exception there kills the stage thread and wedges the
    whole conversion): keep it to a queue put. The closure subclass is safe because the
    pipeline cache is per-DocumentConverter instance and we build a fresh converter per job.
    """
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline

    class _ProgressPipeline(StandardPdfPipeline):
        def _release_page_resources(self, item: Any) -> None:
            super()._release_page_resources(item)
            on_page_done(item.page_no)

    opts = PdfPipelineOptions()
    opts.do_ocr = False
    opts.generate_picture_images = True
    opts.do_table_structure = True
    # docling types table_structure_options as the base class (no `.mode`), but the concrete
    # TableStructureOptions has it — set ACCURATE for faithful table extraction.
    opts.table_structure_options.mode = TableFormerMode.ACCURATE  # type: ignore[attr-defined]
    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_cls=_ProgressPipeline, pipeline_options=opts)
        }
    )


def _rewrite_image_refs(md: str, imgs_dir: Path) -> str:
    """Rewrite Docling's absolute image paths in `![alt](path)` refs to relative `imgs/<file>`.

    save_as_markdown(image_mode=REFERENCED, artifacts_dir=imgs_dir) writes the crops into
    imgs_dir but references them by absolute path; the contract needs `imgs/<basename>`.
    """

    def repl(m: re.Match[str]) -> str:
        path = m.group(2).strip()
        name = Path(path).name
        # Only rewrite refs that actually point at a file we extracted into imgs/.
        if (imgs_dir / name).is_file():
            return f"{m.group(1)}imgs/{name}{m.group(3)}"
        return m.group(0)

    return _MD_IMG.sub(repl, md)


def run_digital_markdown(
    input_pdf: str,
    out_dir: Path,
    download_id: str,
    on_progress: ProgressCb,
    should_cancel: CancelCb,
    locale: str | None,
) -> dict[str, Any]:
    """Convert a born-digital PDF to Markdown via Docling. Returns the save_markdown result
    dict shape: {total_pages, download_id, images}.

    Wipes+recreates out_dir first (requeue safety: a prior crashed attempt must not leave stale
    .md / imgs behind — only this job writes here). Raises Cancelled if a cancel is requested
    before the Docling conversion begins; the conversion itself is uninterruptible, so
    mid-convert cancels are handled by the supervisor's watchdog kill, exactly like the VL path.

    Progress streams per page: convert() runs on a worker thread while THIS thread drains page
    completions off a queue and reports them, so every on_progress/Store write stays on the
    caller's thread (the child's sqlite connection is check_same_thread=True). Progress keeps
    the save-phase status ("saving", looser SAVE_IDLE_TIMEOUT_S): the first job's model load
    and the tail (reading order, doc assembly, markdown save) are still opaque stretches with
    no page heartbeat, so the strict recognition timeout stays wrong for this path.
    """
    from .searchable_pdf import Cancelled

    if should_cancel():
        raise Cancelled()

    from docling_core.types.doc import ImageRefMode  # type: ignore[attr-defined]

    with fitz.open(input_pdf) as probe_doc:
        total = probe_doc.page_count

    on_progress(0, total or 1, "saving", msg("converting_doc", locale))

    reset_output_dir(out_dir)

    # None is the completion sentinel from the convert thread; ints are finished page numbers.
    page_events: queue.SimpleQueue[int | None] = queue.SimpleQueue()
    converter = _build_converter(page_events.put)

    outcome: dict[str, Any] = {}

    def _convert() -> None:
        try:
            outcome["result"] = converter.convert(input_pdf)
        except BaseException as e:  # re-raised on the caller's thread below
            outcome["error"] = e
        finally:
            page_events.put(None)

    convert_thread = threading.Thread(target=_convert, name="docling-convert", daemon=True)
    convert_thread.start()
    pages_done = 0
    while page_events.get() is not None:
        pages_done = min(pages_done + 1, total or 1)
        try:
            on_progress(
                pages_done,
                total or 1,
                "saving",
                msg("converting_page", locale, current=pages_done, total=total),
            )
        except Exception:
            # Progress is best-effort; a transient DB write failure must not abandon the
            # convert thread (the loop must keep draining until the sentinel).
            log.warning("progress write failed for page %s", pages_done, exc_info=True)
    convert_thread.join()

    if "error" in outcome:
        raise outcome["error"]
    doc = outcome["result"].document
    total = doc.num_pages()

    if should_cancel():
        raise Cancelled()

    imgs_dir = out_dir / "imgs"
    final = out_dir / f"{download_id}.md"
    # REFERENCED image mode writes each picture crop into imgs_dir and references it (by absolute
    # path, which we rewrite below). No character normalization: the text layer is ground truth.
    doc.save_as_markdown(final, artifacts_dir=imgs_dir, image_mode=ImageRefMode.REFERENCED)

    md = final.read_text(encoding="utf-8")
    md = _rewrite_image_refs(md, imgs_dir) if imgs_dir.is_dir() else md
    final.write_text(md, encoding="utf-8")

    images = list_images(out_dir)
    on_progress(total or 1, total or 1, "saving", msg("consolidating", locale))
    return {"total_pages": total, "download_id": download_id, "images": images}
