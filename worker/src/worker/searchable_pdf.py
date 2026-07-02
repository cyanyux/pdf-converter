"""Searchable-PDF pipeline: rasterize -> batch OCR (PP-OCRv6) -> invisible text layer.

Ported from the proven Flask implementation, adapted to the worker's callback
interface and with pages built in strict index order (fixes the old fallback bug).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pymupdf as fitz

from . import config
from .i18n import msg
from .text_utils import fix_ocr_text

log = logging.getLogger("worker.searchable_pdf")

PDF_POINTS_PER_INCH = 72

ProgressCb = Callable[[int, int, str, str], None]
CancelCb = Callable[[], bool]


class Cancelled(RuntimeError):
    pass


def pixmap_to_numpy(pix: fitz.Pixmap) -> np.ndarray:
    """PyMuPDF Pixmap -> BGR numpy array (what PaddleOCR expects)."""
    n = 4 if pix.alpha else 3
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, n)
    return img[:, :, :3][:, :, ::-1].copy()  # RGB(A) -> BGR


def parse_ocr_result(res: Any, min_confidence: float, locale: str | None) -> list[dict[str, Any]]:
    texts = res.get("rec_texts") if hasattr(res, "get") else res["rec_texts"]
    if not texts:
        return []
    polys = (res.get("rec_polys") if hasattr(res, "get") else None) or res.get("dt_polys") or []
    scores = res.get("rec_scores") or []
    out: list[dict[str, Any]] = []
    for i, text in enumerate(texts):
        if i >= len(polys):
            continue
        score = float(scores[i]) if i < len(scores) else 1.0
        if score < min_confidence or not text or not text.strip():
            continue
        poly = polys[i]
        out.append(
            {
                "text": fix_ocr_text(text, locale),
                "poly": poly.tolist() if hasattr(poly, "tolist") else poly,
                "score": score,
            }
        )
    return out


def _batch_ocr(ocr: Any, images: list[np.ndarray], min_conf: float, locale: str | None) -> list[list[dict]]:
    if not images:
        return []
    try:
        results = ocr.predict(images)
        return [parse_ocr_result(r, min_conf, locale) for r in results]
    except Exception as e:
        log.warning("batch OCR failed (%s); falling back to per-image", e)
        out: list[list[dict]] = []
        for img in images:
            try:
                out.append(parse_ocr_result(ocr.predict(img)[0], min_conf, locale))
            except Exception:
                out.append([])
        return out


def create_searchable_pdf(
    ocr: Any,
    input_pdf: str,
    output_pdf: str,
    on_progress: ProgressCb,
    should_cancel: CancelCb,
    locale: str | None = None,
    dpi: int = 200,
    min_confidence: float = 0.5,
) -> dict[str, Any]:
    output_path = Path(output_pdf)
    src = fitz.open(input_pdf)
    if src.is_encrypted:
        src.close()
        raise RuntimeError(msg("err_encrypted_pdf", locale))
    total = len(src)
    if total == 0:
        src.close()
        raise RuntimeError(msg("err_empty_pdf", locale))

    zoom = dpi / PDF_POINTS_PER_INCH
    new_doc = fitz.open()
    font = fitz.Font("cjk")
    if src.metadata:
        new_doc.set_metadata(src.metadata)

    on_progress(0, total, "processing", msg("processing_start", locale, pages=total))
    failed: list[int] = []

    try:
        for start in range(0, total, config.OCR_BATCH_SIZE):
            if should_cancel():
                raise Cancelled()
            end = min(start + config.OCR_BATCH_SIZE, total)
            on_progress(
                start + 1,
                total,
                "processing",
                msg("recognizing_pages", locale, start=start + 1, end=end, total=total),
            )

            images: list[np.ndarray] = []
            infos: list[dict[str, Any]] = []
            for idx in range(start, end):
                page = src[idx]
                rect = page.rect
                rot = page.rotation
                eff_dpi = dpi
                if max(rect.width, rect.height) * zoom > 4000:
                    eff_dpi = max(150, int(4000 / max(rect.width, rect.height) * 72))
                eff_zoom = eff_dpi / PDF_POINTS_PER_INCH
                m = fitz.Matrix(eff_zoom, eff_zoom)
                if rot:
                    m = m.prerotate(rot)
                pix = page.get_pixmap(matrix=m)
                images.append(pixmap_to_numpy(pix))
                infos.append({"idx": idx, "rect": rect, "rot": rot, "zoom": eff_zoom, "dpi": eff_dpi})
                del pix

            ocr_results = _batch_ocr(ocr, images, min_confidence, locale)
            del images

            for info, ocr_data in zip(infos, ocr_results, strict=True):
                page = src[info["idx"]]
                m = fitz.Matrix(info["zoom"], info["zoom"])
                if info["rot"]:
                    m = m.prerotate(info["rot"])
                pix = page.get_pixmap(matrix=m)
                rect = info["rect"]
                new_page = new_doc.new_page(width=rect.width, height=rect.height)  # strict order
                try:
                    new_page.insert_image(rect, pixmap=pix)
                    _overlay_text(new_page, ocr_data, rect, info["zoom"], info["dpi"], font)
                except Exception as e:
                    log.error("page %d overlay failed: %s", info["idx"] + 1, e)
                    failed.append(info["idx"] + 1)
                finally:
                    del pix

        src.close()
        on_progress(total, total, "saving", msg("saving_pdf", locale))
        new_doc.save(output_pdf, garbage=4, deflate=True)
        new_doc.close()

        result: dict[str, Any] = {"total_pages": total, "output_path": output_pdf}
        if failed:
            result["warning"] = msg("err_partial_pages", locale, pages=failed)
        return result
    except BaseException:
        new_doc.close()
        if not src.is_closed:
            src.close()
        if output_path.exists():
            output_path.unlink(missing_ok=True)
        raise


def _overlay_text(
    new_page: fitz.Page,
    ocr_data: list[dict],
    rect: fitz.Rect,
    zoom: float,
    dpi: int,
    font: fitz.Font,
) -> None:
    img_w, img_h = rect.width * zoom, rect.height * zoom
    scale_x, scale_y = rect.width / img_w, rect.height / img_h
    row_tol = max(10, int(20 * (dpi / 200)))

    def sort_key(item: dict) -> tuple[int, float]:
        poly = item["poly"]
        if len(poly) >= 4:
            y_min = min(p[1] for p in poly)
            x_min = min(p[0] for p in poly)
            return (int(y_min / row_tol), x_min)
        return (0, 0.0)

    for item in sorted(ocr_data, key=sort_key):
        text, poly = item["text"], item["poly"]
        if not text or len(poly) < 4:
            continue
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        x0, y0 = min(xs) * scale_x, min(ys) * scale_y
        x1, y1 = max(xs) * scale_x, max(ys) * scale_y
        bw, bh = x1 - x0, y1 - y0
        if bw < 1 or bh < 1:
            continue
        ref = 10.0
        tl = font.text_length(text, fontsize=ref)
        fontsize = ref * (bw / tl) if tl > 0 else bh * 0.8
        fontsize = max(2.0, min(fontsize, 200.0))
        try:
            tw = fitz.TextWriter(new_page.rect)
            tw.append((x0, y1 - bh * 0.15), text, font=font, fontsize=fontsize)
            tw.write_text(new_page, render_mode=3)  # invisible
        except Exception:
            continue
