"""Document-parse pipeline (PaddleOCR-VL v1.6): PDF -> Markdown / Word.

One VL pass serves both markdown and word for a dual-export group. VL input
resolution is capped (max_pixels + render downscale) to bound VRAM, with an
OOM -> downscale-and-retry guard. DOCX uses native save_to_word + docxcompose
by default; pandoc is the fallback.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pymupdf as fitz

from . import config
from .i18n import msg
from .postprocess import fix_word_styles, merge_docx, process_markdown
from .searchable_pdf import Cancelled, pixmap_to_numpy

log = logging.getLogger("worker.docparse")

ProgressCb = Callable[[int, int, str, str], None]
CancelCb = Callable[[], bool]


def _render_page(page: fitz.Page, zoom: float, max_pixels: int) -> np.ndarray:
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    if pix.width * pix.height > max_pixels:
        factor = (max_pixels / (pix.width * pix.height)) ** 0.5
        z = max(0.4, zoom * factor)
        pix = page.get_pixmap(matrix=fitz.Matrix(z, z))
    return pixmap_to_numpy(pix)


def _predict_with_oom_guard(vl: Any, img: np.ndarray, max_pixels: int) -> list[Any]:
    for attempt in range(2):
        try:
            return list(
                vl.predict(input=img, max_pixels=max_pixels, max_new_tokens=config.VL_MAX_NEW_TOKENS)
            )
        except (MemoryError, RuntimeError) as e:
            if "memory" in str(e).lower() and attempt == 0:
                h, w = img.shape[:2]
                img = _downscale(img, 0.7)
                log.warning("VL OOM on %dx%d; retrying at 0.7x", w, h)
                continue
            raise
    raise RuntimeError("VL predict retries exhausted")  # unreachable: loop returns or raises


def _downscale(img: np.ndarray, factor: float) -> np.ndarray:
    h, w = img.shape[:2]
    nh, nw = max(1, int(h * factor)), max(1, int(w * factor))
    ys = (np.arange(nh) / factor).astype(int).clip(0, h - 1)
    xs = (np.arange(nw) / factor).astype(int).clip(0, w - 1)
    return img[ys][:, xs].copy()


def run_vl(
    vl: Any,
    input_pdf: str,
    on_progress: ProgressCb,
    should_cancel: CancelCb,
    locale: str | None,
    total_steps: int,
) -> tuple[list[Any], int]:
    """Render + VL-predict each page, then restructure. Returns (restructured, total_pages)."""
    doc = fitz.open(input_pdf)
    if doc.is_encrypted:
        doc.close()
        raise RuntimeError(msg("err_encrypted_pdf", locale))
    total = len(doc)
    if total == 0:
        doc.close()
        raise RuntimeError(msg("err_empty_pdf", locale))
    if total_steps <= 0:
        total_steps = total

    on_progress(0, total_steps, "processing", msg("converting_start", locale, pages=total))
    pages_res: list[Any] = []
    try:
        for i in range(total):
            if should_cancel():
                raise Cancelled()
            on_progress(
                i + 1,
                total_steps,
                "processing",
                msg("recognizing_page", locale, current=i + 1, total=total),
            )
            img = _render_page(doc[i], config.VL_RENDER_ZOOM, config.VL_MAX_PIXELS)
            pages_res.extend(_predict_with_oom_guard(vl, img, config.VL_MAX_PIXELS))
            del img
        doc.close()
    except BaseException:
        if not doc.is_closed:
            doc.close()
        raise

    on_progress(total, total_steps, "saving", msg("consolidating", locale))
    restructured = vl.restructure_pages(pages_res, merge_tables=True, relevel_titles=True, concatenate_pages=True)
    return list(restructured), total


def save_markdown(restructured: list[Any], out_dir: Path, download_id: str, locale: str | None) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    for res in restructured:
        res.save_to_markdown(save_path=str(out_dir))
    md_files = sorted(out_dir.glob("*.md"))
    if not md_files:
        raise RuntimeError(msg("err_no_markdown", locale))
    md = "\n\n".join(f.read_text(encoding="utf-8") for f in md_files)
    md = process_markdown(md, out_dir, locale, images=True)
    final = out_dir / f"{download_id}.md"
    final.write_text(md, encoding="utf-8")
    for f in md_files:
        if f != final:
            f.unlink(missing_ok=True)
    images = [p for ext in ("png", "jpg", "jpeg", "gif", "webp") for p in out_dir.glob(f"**/*.{ext}")]
    return {"total_pages": len(restructured), "download_id": download_id, "images": images}


def save_word(restructured: list[Any], out_dir: Path, download_id: str, locale: str | None) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    final_docx = out_dir / f"{download_id}.docx"
    if config.DOCX_BACKEND == "pandoc":
        _pandoc_docx(restructured, out_dir, download_id, locale, final_docx)
    else:
        try:
            tmp = out_dir / "_docx"
            tmp.mkdir(exist_ok=True)
            for res in restructured:
                res.save_to_word(save_path=str(tmp))
            docx_files = sorted(tmp.glob("*.docx"))
            if not docx_files:
                raise RuntimeError("native save_to_word produced no .docx")
            merge_docx(docx_files, final_docx)
            fix_word_styles(final_docx)
            # move any extracted images alongside the docx for the download bundle
            for img in tmp.glob("imgs/*"):
                img.replace(out_dir / img.name)
        except Exception as e:
            log.warning("native DOCX failed (%s); falling back to pandoc", e)
            _pandoc_docx(restructured, out_dir, download_id, locale, final_docx)
    images = [p for ext in ("png", "jpg", "jpeg", "gif", "webp") for p in out_dir.glob(f"*.{ext}")]
    return {
        "total_pages": len(restructured),
        "download_id": download_id,
        "images_count": len(images),
    }


def _pandoc_docx(
    restructured: list[Any], out_dir: Path, download_id: str, locale: str | None, final_docx: Path
) -> None:
    for res in restructured:
        res.save_to_markdown(save_path=str(out_dir))
    md_files = sorted(out_dir.glob("*.md"))
    md = "\n\n".join(f.read_text(encoding="utf-8") for f in md_files)
    md = process_markdown(md, out_dir, locale, images=True)
    md_path = out_dir / f"{download_id}_pandoc.md"
    md_path.write_text(md, encoding="utf-8")
    try:
        subprocess.run(
            [
                "pandoc",
                str(md_path),
                "-o",
                str(final_docx),
                "--resource-path",
                str(out_dir),
                "--extract-media",
                str(out_dir),
            ],
            check=True,
            capture_output=True,
            cwd=str(out_dir),
            timeout=config.PANDOC_TIMEOUT,
        )
    except FileNotFoundError as e:
        raise RuntimeError(msg("err_pandoc_missing", locale)) from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(msg("err_pandoc_timeout", locale, seconds=config.PANDOC_TIMEOUT)) from e
    except subprocess.CalledProcessError as e:
        detail = f"{e.stdout.decode()} {e.stderr.decode()}"
        raise RuntimeError(msg("err_pandoc_failed", locale, detail=detail)) from e
    fix_word_styles(final_docx)
    md_path.unlink(missing_ok=True)
