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
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

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


def _build_converter() -> Any:
    """Docling DocumentConverter for born-digital PDFs (text-faithful, no OCR).

    do_ocr=False (trust the text layer), TableFormer ACCURATE mode, and
    generate_picture_images=True so picture crops can be written to imgs/.
    """
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
    from docling.document_converter import DocumentConverter, PdfFormatOption

    opts = PdfPipelineOptions()
    opts.do_ocr = False
    opts.generate_picture_images = True
    opts.do_table_structure = True
    # docling types table_structure_options as the base class (no `.mode`), but the concrete
    # TableStructureOptions has it — set ACCURATE for faithful table extraction.
    opts.table_structure_options.mode = TableFormerMode.ACCURATE  # type: ignore[attr-defined]
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})


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
    before the (single, opaque) Docling conversion begins; the conversion itself is uninterruptible,
    so mid-convert cancels are handled by the supervisor's watchdog kill, exactly like the VL path.
    """
    from .searchable_pdf import Cancelled

    if should_cancel():
        raise Cancelled()

    from docling_core.types.doc import ImageRefMode  # type: ignore[attr-defined]

    # Emit a save-phase status so the supervisor watchdog applies the looser SAVE_IDLE_TIMEOUT_S:
    # Docling's convert() is one opaque CPU call with no per-page hook, so there is no per-page
    # heartbeat to keep the strict recognition timeout honest. The status also gives the SPA a
    # live "converting" state instead of a frozen bar.
    on_progress(0, 1, "saving", msg("converting_doc", locale))

    reset_output_dir(out_dir)

    converter = _build_converter()
    result = converter.convert(input_pdf)
    doc = result.document
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
