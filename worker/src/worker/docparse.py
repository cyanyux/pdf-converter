"""Document-parse pipeline (PaddleOCR-VL v1.6): PDF -> Markdown / Word.

One VL pass serves both markdown and word for a dual-export group. VL input
resolution is capped (max_pixels + render downscale) to bound VRAM, with an
OOM -> downscale-and-retry guard. DOCX is built natively via save_to_word +
docxcompose.
"""

from __future__ import annotations

import logging
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pymupdf as fitz

from . import config
from .i18n import msg
from .postprocess import (
    apply_word_heading_styles,
    apply_word_table_merges,
    drain_word_table_merge_specs,
    finalize_docx_text,
    fix_word_styles,
    merge_docx,
    process_markdown,
    reset_word_table_merge_specs,
    word_table_grid,
)
from .searchable_pdf import Cancelled, pixmap_to_numpy
from .text_utils import fix_ocr_text

log = logging.getLogger("worker.docparse")


def _patch_paddlex_word_table() -> None:
    """Make PaddleX's native DOCX writer honor table rowspan/colspan.

    Its `_parse_html_table` ignores them and pads short (merged-cell continuation) rows at the
    end, shifting their content one column left (unfixed in paddlex 3.7.2 / upstream main). We
    swap in a rowspan-aware parser. Guarded + idempotent: if the symbol is gone (upstream
    refactor/fix), we leave it alone rather than crash the word export."""
    try:
        from paddlex.inference.common.result.converter import word_converter as wc
    except Exception as e:
        log.warning("could not patch PaddleX word-table parser: %s", e)
        return
    if getattr(wc, "_pdfocr_rowspan_patched", False) or not hasattr(wc, "_parse_html_table"):
        return
    wc._parse_html_table = word_table_grid
    wc._pdfocr_rowspan_patched = True


ProgressCb = Callable[[int, int, str, str], None]
CancelCb = Callable[[], bool]
# Extensions counted as extracted images (raster + svg + the .bin data-URI fallback).
_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "svg", "bin"}


def reset_output_dir(out_dir: Path) -> None:
    """Wipe + recreate a markdown job's output dir (requeue safety).

    A requeued attempt must not glob-merge stale .md / images a prior (crashed) attempt left
    behind. Only one job's save writes here, so wiping is safe. Shared by both markdown pipelines
    (VL save_markdown + the digital Docling path)."""
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)


def list_images(out_dir: Path) -> list[Path]:
    """Every extracted image file under out_dir (by _IMAGE_EXTS). Shared by both markdown paths."""
    return [p for p in out_dir.glob("**/*") if p.is_file() and p.suffix.lstrip(".").lower() in _IMAGE_EXTS]


def _render_page(page: fitz.Page, zoom: float, max_pixels: int) -> np.ndarray:
    # Pick the final zoom up front (from the page rect) so we rasterize only once.
    rect = page.rect
    area = (rect.width * zoom) * (rect.height * zoom)
    if area > max_pixels:
        zoom = max(0.4, zoom * (max_pixels / area) ** 0.5)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    return pixmap_to_numpy(pix)


def _predict_with_oom_guard(vl: Any, img: np.ndarray, max_pixels: int) -> list[Any]:
    for attempt in range(2):
        try:
            return list(vl.predict(input=img, max_pixels=max_pixels, max_new_tokens=config.VL_MAX_NEW_TOKENS))
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
    out: np.ndarray = img[ys][:, xs].copy()
    return out


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
    reset_output_dir(out_dir)
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
    images = list_images(out_dir)
    return {"total_pages": len(restructured), "download_id": download_id, "images": images}


def save_word(restructured: list[Any], out_dir: Path, download_id: str, locale: str | None) -> dict[str, Any]:
    # Recreate the dir so a requeued attempt can't merge stale per-page .docx a prior
    # (crashed) attempt left in _docx. Only this job's save writes here, so wiping is safe.
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    final_docx = out_dir / f"{download_id}.docx"
    tmp = out_dir / "_docx"
    tmp.mkdir(parents=True, exist_ok=True)
    _patch_paddlex_word_table()  # honor table rowspan/colspan (PaddleX's writer drops merged cells)
    reset_word_table_merge_specs()  # start clean; word_table_grid appends one spec per table parsed
    try:
        for res in restructured:
            res.save_to_word(save_path=str(tmp))
        docx_files = sorted(tmp.glob("*.docx"))
        if not docx_files:
            raise RuntimeError(msg("err_no_word", locale))
        # Drain the per-table merge specs the patched parser recorded, IN PARSE ORDER, before the
        # merge shuffles files around. save_to_word writes per-result .docx in `restructured` order
        # and glob(sorted) reads them back in the same page order, so the spec order matches the
        # final document's <w:tbl> order (apply_word_table_merges tolerates any mismatch anyway).
        merge_specs = drain_word_table_merge_specs()
        merge_docx(docx_files, final_docx)
        apply_word_table_merges(final_docx, merge_specs)  # blank continuation cells -> real vMerge/gridSpan
        fix_word_styles(final_docx)
        # Native save_to_word bypasses process_markdown, so normalize the DOCX text itself:
        # render inline LaTeX (all locales) + Simplified->Traditional (zh-TW only). Best-effort,
        # mirroring fix_word_styles: text normalization is cosmetic, so an exception here must
        # ship the un-normalized (but complete) .docx rather than hard-fail the whole export.
        try:
            finalize_docx_text(final_docx, locale)
        except Exception as e:
            log.warning("docx text normalization skipped: %s", e)
        # Promote size/bold heading paragraphs to real 'Heading N' styles so Word's navigation
        # pane / TOC see an outline (the writer ships everything as 'Normal'). Heading DEPTH
        # comes from the SAME VL pass's markdown (the writer flattens every level to one size,
        # so size-ranking alone yields a two-level outline): harvest {heading text: level} from
        # each result's markdown. Both steps best-effort like the ones above: a failure ships
        # the un-outlined (but complete) .docx rather than hard-fail.
        try:
            heading_levels: dict[str, int] = {}
            for res in restructured:
                try:
                    md_text = res.markdown.get("markdown_texts") or ""
                    # zh-TW jobs convert the docx text; convert the harvested headings the same
                    # way so the normalized-text keys actually match the styled paragraphs.
                    md_text = fix_ocr_text(md_text, locale)
                    for m in re.finditer(r"^(#{1,6})\s+(.+?)\s*$", md_text, re.MULTILINE):
                        key = "".join(m.group(2).split())
                        heading_levels.setdefault(key, len(m.group(1)))
                except Exception as e:
                    log.debug("heading harvest skipped for one result: %s", e)
            apply_word_heading_styles(final_docx, heading_levels)
        except Exception as e:
            log.warning("docx heading style pass skipped: %s", e)
        # Images are already embedded in the .docx (word/media); the loose crops under
        # _docx/imgs are only counted for the UI, so tally them, then drop the whole temp dir.
        imgs_dir = tmp / "imgs"
        images_count = sum(1 for p in imgs_dir.glob("*") if p.is_file()) if imgs_dir.is_dir() else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {
        "total_pages": len(restructured),
        "download_id": download_id,
        "images_count": images_count,
    }
