"""Office (docx / xlsx / pptx) -> Markdown via PaddleOCR 3.5+ doc2md.

Digital Office documents are converted directly (CPU, no GPU / no OCR), so these
jobs run in the supervisor without spawning a model child or disturbing the GPU.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import config
from .i18n import msg
from .postprocess import process_markdown

if TYPE_CHECKING:
    from .store import Store

log = logging.getLogger("worker.office")

SUPPORTED_EXTS = (".docx", ".xlsx", ".pptx")


def is_office(path: str) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTS


def process(store: Store, job: dict[str, Any]) -> None:
    from paddleocr import doc2md_convert

    jid, locale = job["id"], job["locale"]
    out_dir = config.OUTPUTS_DIR / jid
    out_dir.mkdir(parents=True, exist_ok=True)
    store.set_progress(jid, 0, 1, "processing", msg("converting_start", locale, pages=1))

    result = doc2md_convert(job["upload_path"])

    images = getattr(result, "images", None) or {}
    for name, img in images.items():
        dest = out_dir / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(Exception):
            img.save(dest)

    # doc2md emits HTML tables; process_markdown converts them to Markdown + S->T.
    md = process_markdown(result.markdown, out_dir, locale, images=True)
    (out_dir / f"{jid}.md").write_text(md, encoding="utf-8")

    meta = getattr(result, "metadata", None) or {}
    pages = int(meta.get("paragraph_count", 0) or 0) or 1
    store.set_result(
        jid,
        jid,
        {
            "totalPages": pages,
            "downloadId": jid,
            "imagesCount": len(images),
            "originalName": job["filename"],
        },
        msg("done", locale),
    )
