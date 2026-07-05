"""Searchable-PDF pipeline: rasterize -> batch OCR (PP-OCRv6) -> invisible text layer.

Ported from the proven Flask implementation, adapted to the worker's callback
interface and with pages built in strict index order (fixes the old fallback bug).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pymupdf as fitz

from . import config
from .i18n import msg
from .text_utils import fix_ocr_text, strip_cjk_spaces, strip_cjk_spaces_across

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
    # Result may be a dict-like (.get) or only subscriptable (__getitem__); use one accessor.
    # (SIM401 would suggest res.get here, but this branch is exactly the no-.get case.)
    get = res.get if hasattr(res, "get") else (lambda k, d=None: res[k] if k in res else d)  # noqa: SIM401
    texts = get("rec_texts")
    if not texts:
        return []
    # Use len(), not truthiness: rec_polys/rec_scores may be numpy arrays, and `arr or default`
    # raises "truth value of an array is ambiguous", which would silently zero the page's text.
    polys = get("rec_polys")
    if polys is None or len(polys) == 0:
        polys = get("dt_polys")
    if polys is None:
        polys = []
    scores = get("rec_scores")
    if scores is None:
        scores = []
    # Per-word text + boxes (return_word_box=True). Used only to peel a leading list marker off a
    # hanging-indent line; absent (older result / per-image fallback) -> whole line as one run.
    words_all = get("text_word") or []
    regions_all = get("text_word_region") or []
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
                "line": i,  # source line index, for the reading-order grouping
                # Raw (un-converted) OCR word tokens + their pixel boxes for this line.
                "words": list(words_all[i]) if i < len(words_all) else None,
                "word_regions": regions_all[i] if i < len(regions_all) else None,
            }
        )
    return out


def _batch_ocr(ocr: Any, images: list[np.ndarray], min_conf: float, locale: str | None) -> list[list[dict[str, Any]]]:
    if not images:
        return []
    # return_word_box=True adds per-word text/boxes (text_word / text_word_region) alongside the
    # line-level rec_texts/rec_polys — needed to place a hanging-indent list marker's body at its
    # true indent. It doesn't change the recognized text and adds negligible cost.
    try:
        results = ocr.predict(images, return_word_box=True)
        return [parse_ocr_result(r, min_conf, locale) for r in results]
    except Exception as e:
        log.warning("batch OCR failed (%s); falling back to per-image", e)
        out: list[list[dict[str, Any]]] = []
        for img in images:
            try:
                out.append(parse_ocr_result(ocr.predict(img, return_word_box=True)[0], min_conf, locale))
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
    title: str | None = None,
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
    # Carry the source metadata, but overwrite /Title with the real (correct-UTF-8) filename.
    # Source PDFs — especially "Microsoft: Print To PDF" — routinely carry a mojibaked /Title,
    # and Chrome uses /Title as the tab/caption when viewing a PDF inline (Edge falls back to the
    # filename), so a corrupt title renders as gibberish there. The filename is what the user
    # expects to see. Also drop /Author (it can carry a stray tab/control char).
    meta = dict(src.metadata) if src.metadata else {}
    if title:
        meta["title"] = title
        meta.pop("author", None)
    if meta:
        new_doc.set_metadata(meta)

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
                rect = page.rect  # already /Rotate-normalized (the visual rectangle)
                # Cap the long edge near 4000px so a large-media page can't blow up VRAM/RAM or
                # OCR time. min(zoom, 4000/maxdim) makes the cap actually bind — the old DPI
                # floor left huge pages well above 4000px. Normal pages keep the full zoom.
                maxdim = max(rect.width, rect.height)
                eff_zoom = min(zoom, 4000.0 / maxdim) if maxdim > 0 else zoom
                eff_dpi = round(eff_zoom * PDF_POINTS_PER_INCH)
                # Do NOT prerotate: get_pixmap already honors the page's /Rotate and page.rect is
                # already the rotated (visual) rect, so prerotating rotates a SECOND time —
                # distorting the raster and misaligning the rect-sized invisible text layer.
                pix = page.get_pixmap(matrix=fitz.Matrix(eff_zoom, eff_zoom))
                images.append(pixmap_to_numpy(pix))
                # Keep pix to reuse as the output image below (no second rasterize), and the
                # numpy raster so the text overlay can snap to measured ink. Up to
                # OCR_BATCH_SIZE of each are held per batch, then freed in the insert loop.
                infos.append(
                    {
                        "idx": idx,
                        "rect": rect,
                        "zoom": eff_zoom,
                        "dpi": eff_dpi,
                        "pix": pix,
                        "img": images[-1],
                    }
                )

            ocr_results = _batch_ocr(ocr, images, min_confidence, locale)
            del images

            for info, ocr_data in zip(infos, ocr_results, strict=True):
                pix = info["pix"]
                rect = info["rect"]
                new_page = new_doc.new_page(width=rect.width, height=rect.height)  # strict order
                try:
                    new_page.insert_image(rect, pixmap=pix)
                    _overlay_text(
                        new_page, ocr_data, rect, info["zoom"], info["dpi"], font,
                        page_img=info["img"],
                    )
                except Exception as e:
                    log.error("page %d overlay failed: %s", info["idx"] + 1, e)
                    failed.append(info["idx"] + 1)
                finally:
                    # Drop the references (local alias + the batch's infos[i] pixmap and raster)
                    # so they free now, page-by-page, instead of the whole batch staying
                    # resident until `infos` is replaced on the next outer iteration.
                    info["pix"] = None
                    info["img"] = None
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


# Ink darker than this (in the darkest channel) counts as glyph ink when snapping the invisible
# text layer to the raster. min-channel catches colored text (a blue stamp is dark in red/green).
_INK_THRESHOLD = 160


def _is_inverted(gray: np.ndarray, x0: float, x1: float, y0: float, y1: float) -> bool:
    """True when a line's detection box is majority-dark (light-on-dark decoration).

    Normal print is dark-on-light; even dense bold CJK stays well under half dark over the FULL
    detection box (ascender/descender padding included). Text inside a logo/seal/floor-plan fill
    is LIGHT-on-dark — the dark threshold marks the whole background as "ink" and the measured
    band balloons to the decoration's extent (the quad then mis-tracks the glyphs). Polarity is
    decided ONCE per line from the full box and reused for every measurement window: a narrow
    band-strip window can be legitimately majority-ink for dense text, so deciding per window
    would mis-flip.
    """
    h, w = gray.shape
    xa, xb = max(0, int(x0)), min(w, int(np.ceil(x1)))
    ya, yb = max(0, int(y0)), min(h, int(np.ceil(y1)))
    if xb - xa < 2 or yb - ya < 2:
        return False
    return bool((gray[ya:yb, xa:xb] < _INK_THRESHOLD).mean() > 0.55)


def _ink_band(
    gray: np.ndarray, x0: float, x1: float, y0: float, y1: float, invert: bool = False
) -> tuple[int, int] | None:
    """[top, bottom) pixel rows of the speck-filtered ink extent inside the window, or None.

    PP-OCR detection boxes overshoot glyph ink by a line-varying margin (±35% between visually
    identical lines), so a font size taken from the box height gives adjacent lines visibly
    different selection-band thicknesses. The ink rows are the ground truth; the peak-relative
    row filter rejects specks and JPEG noise without clipping ascender/descender tips.
    """
    h, w = gray.shape
    xa, xb = max(0, int(x0)), min(w, int(np.ceil(x1)))
    ya, yb = max(0, int(y0)), min(h, int(np.ceil(y1)))
    if xb - xa < 2 or yb - ya < 2:
        return None
    ink = gray[ya:yb, xa:xb] < _INK_THRESHOLD
    if invert:
        ink = ~ink
    rows = ink.sum(axis=1)
    total = int(rows.sum())
    if total < 8:
        return None
    # FULL ink extent, not a mass-percentile trim: a trimmed band clips ascender/descender tips,
    # and since the selection quad is sized to this band the glyphs visibly poke out of the
    # highlight (user-reported). Rows below 4% of the peak row are specks/JPEG noise, not tips.
    thresh = max(2, int(rows.max() * 0.04))
    idx = np.flatnonzero(rows >= thresh)
    if idx.size == 0:
        return None
    return ya + int(idx[0]), ya + int(idx[-1]) + 1


def _ink_cols(
    gray: np.ndarray,
    x0: float,
    x1: float,
    y0: float,
    y1: float,
    min_px: int = 2,
    invert: bool = False,
) -> tuple[int, int] | None:
    """[first, last) ink column extent inside the window, or None (min_px filters speck noise)."""
    h, w = gray.shape
    xa, xb = max(0, int(x0)), min(w, int(np.ceil(x1)))
    ya, yb = max(0, int(y0)), min(h, int(np.ceil(y1)))
    if xb - xa < 2 or yb - ya < 1:
        return None
    ink = gray[ya:yb, xa:xb] < _INK_THRESHOLD
    if invert:
        ink = ~ink
    cols = np.flatnonzero(ink.sum(axis=0) >= min_px)
    if cols.size == 0:
        return None
    return xa + int(cols[0]), xa + int(cols[-1]) + 1


def _bbox(poly: list[Any]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


def _voverlap(a0: float, a1: float, b0: float, b1: float) -> float:
    """Vertical overlap of two [top, bottom] spans as a fraction of the shorter span."""
    inter = max(0.0, min(a1, b1) - max(a0, b0))
    hmin = min(a1 - a0, b1 - b0)
    return inter / hmin if hmin > 0 else 0.0


def _order_reading(items: list[dict[str, Any]], overlap_frac: float = 0.35) -> list[dict[str, Any]]:
    """Reading order for the invisible text layer.

    Tokens are grouped back into their source line, each line is treated as one atom, and the
    lines are clustered into visual rows by vertical overlap (robust to tall multi-line table
    cells and few-pixel line jitter — a fixed y-band both splits lines that differ slightly and
    lets a tall cell wedge between another column's stacked lines, which is what made table
    drag-selection jump around). Rows are emitted top->bottom; within a row, lines run L->R and
    each line's own tokens stay L->R. Keeping a line's tokens contiguous in the content stream
    is what makes a PDF viewer's drag-select follow visual rows/cells.
    """
    # Drop degenerate polys up front: _bbox() below does min()/max() over the points, which raises
    # on an empty poly. Skipping here (rather than only in _overlay_text's per-item guard) keeps one
    # bad item from aborting the whole page's reading-order pass — and thus its entire text layer.
    items = [it for it in items if it.get("poly") and len(it["poly"]) >= 4]
    if not items:
        return []
    groups: dict[Any, list[dict[str, Any]]] = {}
    for k, it in enumerate(items):
        groups.setdefault(it.get("line", ("_", k)), []).append(it)
    # per line: tokens L->R, plus the line's union extent
    lines: list[tuple[list[dict[str, Any]], float, float, float, float]] = []
    for toks in groups.values():
        boxes = [_bbox(t["poly"]) for t in toks]
        top = min(b[1] for b in boxes)
        bot = max(b[3] for b in boxes)
        left = min(b[0] for b in boxes)
        toks_lr = [t for _, t in sorted(zip(boxes, toks, strict=True), key=lambda z: z[0][0])]
        lines.append((toks_lr, top, bot, left, bot - top))
    heights = sorted(ln[4] for ln in lines)
    line_h = heights[len(heights) // 2] or 1.0  # median line height sets the row band
    # Seed rows from short (line-height) atoms first, so a tall cell attaches to the row it
    # starts in instead of ballooning a band and swallowing later rows.
    order = sorted(range(len(lines)), key=lambda i: (lines[i][4] > 1.6 * line_h, lines[i][1]))
    rows: list[dict[str, Any]] = []
    for i in order:
        _, top, bot, _left, h = lines[i]
        best, best_ov = -1, 0.0
        for r, row in enumerate(rows):
            ov = _voverlap(top, bot, row["top"], row["bot"])
            if ov > best_ov:
                best_ov, best = ov, r
        if best_ov >= overlap_frac:
            row = rows[best]
            row["members"].append(i)
            if h <= 1.6 * line_h:  # only short atoms grow the band
                row["top"] = min(row["top"], top)
                row["bot"] = max(row["bot"], bot)
        else:
            rows.append({"top": top, "bot": bot, "members": [i]})
    rows.sort(key=lambda r: r["top"])
    out: list[dict[str, Any]] = []
    for row in rows:
        row["members"].sort(key=lambda i: lines[i][3])  # L->R within the visual row
        for i in row["members"]:
            out.extend(lines[i][0])
    return out


# A box wider than its natural text is stretched to fill it (below); cap the stretch so a
# short label in an over-wide detection box doesn't blow up into absurdly spaced glyphs.
#
# The cap is 8.0, not the old 4.0. With drift anchors, a run's span comes from DETECTED word
# regions (real raster spread), so an anchored/marker run that legitimately needs a big stretch —
# a lone narrow token (digit "1", "．", "）") abutting a wide justified/indent span — must actually
# reach its box edge. When it doesn't (stretch capped below bw/nat), the run under-fills and leaves
# a horizontal gap before the NEXT abutting run; because each run is its own write_text text object,
# pymupdf/viewers synthesize a newline INTO get_text between the two objects (get_text no longer
# reads the line back verbatim, and cross-run drag-select/search degrade). Measuring the real
# corpus (all_pages.pkl, 11,210 runs): the max run stretch is 4.5x and NOTHING exceeds 5x, so 8.0
# clears every legitimate run with headroom — they now fill their box and abut with zero gap, no
# synthesized newline. 8.0 still guards the true pathology (a mis-detected region making ONE glyph
# kilometers wide, e.g. a comma spanning half a line): that stays bounded to a sane glyph width,
# and its residual gap — the correct outcome for a broken region — is the only case left uncapped.
# The corpus measurement above is of ANCHORED runs (spans from detected word regions); a line
# WITHOUT word boxes spans the raw detection poly — the unmeasured "short label in an over-wide
# box" pathology the cap exists for — so that fallback path keeps the old, tighter 4.0.
_MAX_HSTRETCH = 8.0
_MAX_HSTRETCH_FALLBACK = 4.0

# Leading enumeration markers (Arabic/CJK numerals + delimiter, or parenthesized), e.g. "5.",
# "10、", "（三）", "(2)", "參、". PP-OCR detects a hanging-indent list item ("5.  監造單位…")
# as ONE line spanning the marker, the indent gap, and the body — laying that contiguously shoves
# every body glyph left into the marker gap (invisible "監" ends up in the whitespace before the
# visible one). We peel the marker off so the body starts at its real indent.
#
# The pattern is deliberately loose (it even admits a bare "2026 " prefix) because it is only HALF
# the signal: a text match NEVER splits on its own — _line_runs still requires a confirming
# hanging-indent gap (_MARKER_GAP_FRAC × median glyph width) before it peels. So "2026 年度預算" in
# normal prose (no gap after the digits) stays one run, while "1 專案主持人" with a real indent does
# split. This lets us cover the markers PP-OCR actually emits without a delimiter — a bare digit +
# indent ("1 專案"), a digit glued to the body glyph where the raster still hangs the indent
# ("1啟動"), and single-glyph enclosed numerals (①②③ … ㉑ … ㊿, parenthesized ⑴⑵, period ⒈) — none
# of which the delimiter-anchored alternatives below match. Each new alt consumes the marker and AT
# MOST the following whitespace (never a body glyph): the bare-digit alt requires \s+ and stops
# there; the glued-digit alt uses a zero-width CJK lookahead so only the digits are consumed.
_MARKER_RE = re.compile(
    r"^\s*(?:"
    r"[（(]\s*(?:\d+|[一二三四五六七八九十]+)\s*[)）]"  # （1） (三)
    r"|\d+(?:\.\d+)+"  # 3.1  4.8  3.1.2 (multi-level, delimiter implicit)
    r"|\d+\s*[.、]"  # 1.  10、
    r"|[一二三四五六七八九十]+\s*[、]"  # 一、 三、
    r"|[壹貳參肆伍陸柒捌玖拾]+\s*[、.]"  # 參、 壹.
    r"|[①-⓿㉑-㉟㊱-㊿]\s*"  # ① ⑴ ⒈ ㉑ ㊱ (enclosed numeral, one glyph)
    r"|\d+(?=[㐀-䶿一-鿿])"  # 1啟動 (digit glued to a CJK ideograph; digits only)
    r"|\d+\s+"  # 1 專案 (bare digit + hanging-indent whitespace)
    r")"
)
# A marker is peeled off ONLY when the gap to the body exceeds this fraction of the line's median
# glyph width — a real hanging indent, not a glued "1.5" decimal or an un-indented marker. Dual
# signal (text pattern AND geometry) keeps us from ever splitting mid-sentence, so no spurious
# copy-spaces are injected between body glyphs (the failure mode of per-glyph placement).
_MARKER_GAP_FRAC = 0.35


def _word_bboxes(regions: Any) -> list[tuple[float, float] | None]:
    """Per-word horizontal [x0, x1] pixel extents from text_word_region polys.

    A degenerate word region (empty poly / no points) yields None rather than raising:
    min()/max() over an empty point list would throw ValueError and — since _line_runs has no
    try — kill the whole page's text layer. PP-OCR word regions are 1:1 with the word tokens
    that index this list, so a None keeps the positional alignment intact and lets _line_runs
    bail to the safe single-run path (every other poly consumer in this file guards likewise)."""
    out: list[tuple[float, float] | None] = []
    for r in regions:
        xs = [p[0] for p in r] if r else []
        out.append((min(xs), max(xs)) if xs else None)
    return out


def _trim_ws(
    tok: str, x0: float, x1: float, measure: Callable[[str], float]
) -> tuple[float, float]:
    """Shrink a word-region to its non-whitespace core.

    PP-OCR fuses whitespace into word tokens (". " after a list-marker digit) and the fused
    region spans the blank area too; a run edge taken from it sits in the gap, not on ink, so
    the stretched marker glyphs lean visibly right of the raster. Prorate the whitespace share
    away using the caller's width metric (font advances when placing; char count in tests).
    """
    core = tok.strip()
    if core == tok or not core:
        return x0, x1
    total = measure(tok)
    if total <= 0:
        return x0, x1
    w = x1 - x0
    lead = tok[: len(tok) - len(tok.lstrip())]
    nx0 = x0 + w * (measure(lead) / total)
    return nx0, nx0 + w * (measure(core) / total)


# A token whose detected x-origin diverges from the uniform-stretch prediction by more than
# max(this fraction of the median token width, _ANCHOR_DRIFT_MIN_PX) starts a new (exactly
# abutting) run there. Uniform stretch assumes the font's advance ratios match the raster's;
# they don't when the raster uses fullwidth punctuation the rec model normalized to halfwidth
# ("（六）" printed, "(六)" recognized — the font's "(" advance is a third of the printed glyph)
# or when justified layout gives "，" a half cell — everything after drifts and the selection
# boxes sit beside their glyphs. Measured word regions are ~1pt accurate, so the threshold can
# sit just above region noise (the px floor) while catching every visible misalignment.
_ANCHOR_DRIFT_FRAC = 0.15
_ANCHOR_DRIFT_MIN_PX = 4.0


def _anchor_runs(
    text: str,
    offs: list[int],
    words: list[str],
    boxes: list[tuple[float, float]],
    lo: int,
    hi: int,
    px0: float,
    px1: float,
    measure: Callable[[str], float],
    med: float,
) -> list[tuple[str, float, float]]:
    """Subdivide tokens[lo:hi] over [px0, px1] into EXACTLY ABUTTING runs, re-anchored wherever
    the raster layout diverges from a uniform stretch.

    Run TEXT is sliced from the CONVERTED line `text` via the token char offsets `offs`
    (offs[k] = start of token k; offs[len(words)] = len(text)) — never joined from the raw
    `words`: on the zh-TW path `text` is s2t-converted while `words` are the recognizer's raw
    (possibly Simplified) tokens, and joining them would ship Simplified characters into the
    searchable layer. Tokens are position/length information only.

    Runs abut (each ends where the next starts, zero gap), so no viewer or extractor can ever
    synthesize a space between them — the copy/search text stays byte-identical to the OCR text
    no matter how many anchors are inserted. Each run is then stretched independently, which
    absorbs local advance-vs-raster mismatch (fullwidth punctuation, mixed-width jitter) instead
    of letting it shift every following glyph. A line whose layout matches a uniform stretch gets
    no anchors and stays ONE run — identical to the pre-anchor behavior.
    """
    fallback = [(text[offs[lo] : offs[hi]].strip(), px0, px1)]
    adv = [measure(w) for w in words[lo:hi]]
    total = sum(adv)
    if total <= 0 or px1 - px0 <= 0:
        return fallback
    # Anchor selection is polyline simplification: placement maps advance-space to raster-space
    # piecewise-linearly BETWEEN anchors, so anchors must be chosen against that same piecewise
    # map — Douglas-Peucker over the (advance-prefix, detected-x0) samples guarantees every
    # token lands within eps of its detected position. (A greedy pass comparing against one
    # whole-part interpolation does not: intermediate tokens between two accepted anchors can
    # still deviate arbitrarily.)
    pts: list[tuple[float, float, int]] = []  # (advance-prefix, detected x0, token index)
    s = 0.0
    for j, k in enumerate(range(lo, hi)):
        if k > lo and words[k].strip():
            tx0, _ = _trim_ws(words[k], boxes[k][0], boxes[k][1], measure)
            pts.append((s, tx0, k))
        s += adv[j]
    eps = max(_ANCHOR_DRIFT_FRAC * med, _ANCHOR_DRIFT_MIN_PX)
    keep: list[tuple[float, float, int]] = []

    def simplify(p0: tuple[float, float], p1: tuple[float, float], seg: list[tuple[float, float, int]]) -> None:
        if not seg:
            return
        (s0, x0), (s1, x1) = p0, p1
        span = s1 - s0
        best, bi = -1.0, -1
        for i, (si, xi, _k) in enumerate(seg):
            f = x0 + ((si - s0) / span) * (x1 - x0) if span > 0 else x0
            d = abs(xi - f)
            if d > best:
                best, bi = d, i
        if best > eps:
            mid = seg[bi]
            keep.append(mid)
            simplify(p0, (mid[0], mid[1]), seg[:bi])
            simplify((mid[0], mid[1]), p1, seg[bi + 1 :])

    simplify((0.0, px0), (total, px1), pts)
    keep.sort(key=lambda t: t[0])
    bounds = [lo]
    xs = [px0]
    for _s, tx0, k in keep:
        # Anchors must leave every run monotonic and non-degenerate: strictly right of the
        # previous anchor and left of the part's end, with a minimum run width. A jittered
        # (non-monotonic) region simply doesn't become an anchor.
        if xs[-1] + 0.2 * med < tx0 < px1 - 0.2 * med:
            bounds.append(k)
            xs.append(tx0)
    xs.append(px1)
    runs: list[tuple[str, float, float]] = []
    for i, b in enumerate(bounds):
        e = bounds[i + 1] if i + 1 < len(bounds) else hi
        rt = text[offs[b] : offs[e]]
        # Outer whitespace only: the part's extents already exclude it. INTERIOR whitespace stays
        # in its run — the space glyph absorbs the blank raster area (and survives in copy text).
        if i == 0:
            rt = rt.lstrip()
        if i == len(bounds) - 1:
            rt = rt.rstrip()
        if rt and xs[i + 1] > xs[i]:
            runs.append((rt, xs[i], xs[i + 1]))
    return runs or fallback


def _line_runs(
    text: str,
    words: list[str] | None,
    regions: Any,
    box_x0: float,
    box_x1: float,
    measure: Callable[[str], float] = len,
) -> list[tuple[str, float, float]]:
    """Placement runs for one OCR line as (run_text, x0px, x1px).

    Two layers, both driven by the per-word boxes (absent boxes -> the whole line as one run):
      * a leading enumeration marker on a hanging-indent list item splits into (marker, body)
        parts separated by the REAL gap, so the body starts at its true indent (dual signal:
        _MARKER_RE and a confirming gap — never splits mid-sentence);
      * within each part, drift anchors subdivide into exactly abutting runs (see _anchor_runs)
        so advance-vs-raster mismatch can't shift glyphs off their ink.
    """
    # Placed text always derives from the CONVERTED line `text` (raw tokens can be Simplified);
    # strip_cjk_spaces here mirrors the markdown/docx exports so a stray Han<->Han OCR space
    # doesn't ship in the copy/search layer either. Applied per emitted run (never to the text
    # the token offsets index into, which must keep its 1:1 char alignment with the tokens).
    one_run = [(strip_cjk_spaces(text).strip(), box_x0, box_x1)]
    if not words or regions is None or len(words) != len(regions) or len(words) < 2:
        return one_run
    # Token char offsets slice the ALREADY-converted text (word tokens are raw OCR); valid only
    # when s2t preserved length (it is 1:1 here). Otherwise keep one run rather than mis-slice.
    if len("".join(words)) != len(text):
        return one_run
    offs = [0]
    for w in words:
        offs.append(offs[-1] + len(w))
    raw_boxes = _word_bboxes(regions)
    # A single degenerate (empty/point-less) word region makes positional extents meaningless;
    # fall back to one run rather than guess an x-origin from a missing box.
    if any(b is None for b in raw_boxes):
        return one_run
    boxes: list[tuple[float, float]] = [b for b in raw_boxes if b is not None]
    widths = sorted(x1 - x0 for x0, x1 in boxes if x1 > x0)
    if not widths:
        return one_run
    med = widths[len(widths) // 2]

    def _extent(lo: int, hi: int) -> tuple[float, float] | None:
        """min-x0 / max-x1 over the TRIMMED, non-whitespace word boxes in words[lo:hi], or None.

        Taking min/max over every ink token (rather than reading one edge off the first token and
        the other off the last) is what keeps a part monotonic: PP-OCR word regions can be
        non-monotonic (detection jitter — a later token boxed left of an earlier one), and reading
        the two edges from two independent tokens would then invert the part (x0 > x1), whose runs
        _overlay_text would drop (bw < 1) — silently deleting that side's text from the searchable
        layer. _trim_ws no-ops on a token with no edge whitespace, so this also folds a fused
        ". "/" 監" token down to its ink share."""
        x0s: list[float] = []
        x1s: list[float] = []
        for k in range(lo, hi):
            if not words[k].strip():
                continue
            tx0, tx1 = _trim_ws(words[k], boxes[k][0], boxes[k][1], measure)
            x0s.append(tx0)
            x1s.append(tx1)
        return (min(x0s), max(x1s)) if x0s else None

    # Layer 1: the hanging-indent marker split (real gap between the parts).
    parts: list[tuple[int, int, float, float]] | None = None
    m = _MARKER_RE.match(text)
    if m and m.end() > 0:
        # First word index past the matched marker text (tokens can be multi-char, e.g. ". ").
        acc, split, char_split = 0, None, 0
        for k, w in enumerate(words):
            acc += len(w)
            if acc >= m.end():
                split, char_split = k + 1, acc
                break
        if split is not None:
            # Whitespace-only tokens carry gap area, not ink — they belong to neither side.
            while split < len(words) and not words[split].strip():
                char_split += len(words[split])
                split += 1
            if 0 < split < len(boxes):
                marker_ext = _extent(0, split)
                body_ext = _extent(split, len(boxes))
                if marker_ext is not None and body_ext is not None:
                    mx0, mx1 = marker_ext
                    bx0, ex1 = body_ext
                    marker, body = text[:char_split].strip(), text[char_split:].strip()
                    if (
                        bx0 - mx1 > _MARKER_GAP_FRAC * med  # a real hanging indent, not "1.5"
                        and marker
                        and body
                        and mx1 > mx0
                        and ex1 > bx0
                    ):
                        parts = [(0, split, mx0, mx1), (split, len(words), bx0, ex1)]
    if parts is None:
        parts = [(0, len(words), box_x0, box_x1)]

    # Layer 2: drift anchors within each part. Han<->Han space stripping happens on the emitted
    # run texts (post-slice), keeping the offsets' char alignment intact — and it must run
    # ACROSS the run list, not per run: an anchor can land right after a space token, leaving
    # the stray space as run A's trailing char with its Han neighbor at the start of run B,
    # where the per-run regex (which needs Han on BOTH sides in one string) cannot see it.
    out: list[tuple[str, float, float]] = []
    for lo, hi, px0, px1 in parts:
        out.extend(_anchor_runs(text, offs, words, boxes, lo, hi, px0, px1, measure, med))
    stripped_texts = strip_cjk_spaces_across([t for t, _, _ in out]) if out else None
    if stripped_texts is not None:
        out = [(t, a, b) for t, (_, a, b) in zip(stripped_texts, out, strict=True)]
    out = [(strip_cjk_spaces(t), a, b) for t, a, b in out]
    out = [(t, a, b) for t, a, b in out if t]
    return out or one_run


def _overlay_text(
    new_page: fitz.Page,
    ocr_data: list[dict[str, Any]],
    rect: fitz.Rect,
    zoom: float,
    dpi: int,
    font: fitz.Font,
    page_img: np.ndarray | None = None,
) -> None:
    scale = 1.0 / zoom  # raster px -> PDF points (rect.width / (rect.width * zoom))

    # A glyph's invisible quad spans [baseline - ascender*fs, baseline - descender*fs]; the
    # font's own metrics let us center that box on the OCR box vertically.
    asc = font.ascender or 0.9
    desc = font.descender or -0.1

    # Darkest channel of the rasterized page (the same image OCR saw): ground truth for where
    # glyph ink actually is. OCR detection boxes over/undershoot it by a line-varying margin,
    # so both the selection-band height and the run edges snap to measured ink when available.
    gray = page_img.min(axis=2) if page_img is not None and page_img.ndim == 3 else None

    def _advance(s: str) -> float:
        # Width metric for whitespace trimming in _line_runs: real font advances beat char
        # counts (a fused ". " token is mostly space, not dot).
        return float(font.text_length(s, fontsize=1.0))

    for item in _order_reading(ocr_data):
        text, poly = item["text"], item["poly"]
        if not text or len(poly) < 4:
            continue
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        lx0, lx1 = min(xs), max(xs)  # pixel-space line extent
        py0, py1 = min(ys), max(ys)  # pixel-space vertical extent
        y0, y1 = py0 * scale, py1 * scale
        bh = y1 - y0
        if (lx1 - lx0) * scale < 1 or bh < 1:
            continue
        # Line-level metrics shared by every run so a split marker + body keep ONE uniform glyph
        # height and sit on ONE baseline (no thick/thin jitter). The font size comes from the
        # measured ink band when the raster is available — detection-box heights differ by ±35%
        # between visually identical lines, which reads as random selection-band thickness — and
        # falls back to the box height otherwise. Clamps guard against a mis-measured band
        # (dirt, table rules) hijacking the line.
        inv = _is_inverted(gray, lx0, lx1, py0, py1) if gray is not None else False
        band = _ink_band(gray, lx0, lx1, py0, py1, invert=inv) if gray is not None else None
        if band is not None:
            # The selection quad must COVER the ink, not trace it: sized exactly to the ink
            # extent, ascender tips and antialiased edges visibly poke out of the highlight
            # (user-reported 字會突出). Pad proportionally so bands stay uniform across lines.
            pad = 0.08 * (band[1] - band[0])
            band_h = (band[1] - band[0] + 2 * pad) * scale
            span = asc - desc if asc > desc else 1.0
            fs = min(max(band_h / span, 0.45 * bh), 1.15 * bh)
            baseline = (band[0] - pad) * scale + asc * fs  # quad top sits just above the ink
        else:
            fs = bh
            baseline = (y0 + y1) / 2 + (asc + desc) / 2 * fs  # center on the OCR box
        # A line is one run when a uniform stretch fits its layout; a hanging-indent list item
        # splits into (marker, body) at the real gap, and drift anchors subdivide further into
        # exactly ABUTTING runs where the raster diverges from the font's advances.
        runs = _line_runs(
            text, item.get("words"), item.get("word_regions"), lx0, lx1, measure=_advance
        )
        for ri, (run_text, rx0, rx1) in enumerate(runs):
            # Snap the run's edges to the measured ink columns inside its own window (bounded by
            # the neighboring run so marker/body can't claim each other's ink). OCR word regions
            # are quantized to the rec grid (~3pt) and detection boxes carry padding, so an edge
            # can sit a few pt off the glyph — the user sees a selection box leaning off its
            # digit. The shift cap distrusts far-away ink (table rules, neighbor bleed).
            # ONLY non-abutting edges snap: anchored runs share their boundary coordinate by
            # construction (zero gap = no synthesized copy-space), and moving a shared edge
            # would open a gap a viewer turns into a spurious space.
            if band is not None:
                snap_lo = ri == 0 or rx0 - runs[ri - 1][2] > 2
                snap_hi = ri == len(runs) - 1 or runs[ri + 1][1] - rx1 > 2
                wlo = (lx0 - 3.0) if ri == 0 else (runs[ri - 1][2] + rx0) / 2
                whi = (lx1 + 3.0) if ri == len(runs) - 1 else (rx1 + runs[ri + 1][1]) / 2
                if snap_lo or snap_hi:
                    cols = _ink_cols(gray, wlo, whi, band[0], band[1], invert=inv)  # type: ignore[arg-type]
                    if cols is not None:
                        limit = max(6.0, 0.5 * (band[1] - band[0]))
                        if snap_lo and abs(cols[0] - rx0) <= limit:
                            rx0 = float(cols[0])
                        if snap_hi and abs(cols[1] - rx1) <= limit:
                            rx1 = float(cols[1])
            # The built-in "cjk" font has no glyph for supplementary-plane CJK (e.g. CJK Ext-B,
            # which s2t can PRODUCE on the default zh-TW path). pymupdf renders a glyphless char as
            # .notdef and writes a literal NUL into the text layer — silently corrupting the
            # copy/paste + search text (get_text returns "\x00"). Drop any codepoint the font can't
            # encode: substituting U+FFFD round-trips as a *wrong* CJK char via the ToUnicode map,
            # so omission is cleaner. The layer is invisible (render_mode=3), so dropping a glyph
            # never affects the visible raster.
            run_text = "".join(c for c in run_text if font.has_glyph(ord(c)))
            if not run_text.strip():
                continue
            px0, px1 = rx0 * scale, rx1 * scale
            bw = px1 - px0
            if bw < 1:
                continue
            # Lay the run as ONE contiguous run, then apply a horizontal scale (the PDF Tz operator,
            # via a per-run write_text matrix) so it exactly spans the box width. This satisfies all
            # three properties a searchable text layer needs at once:
            #  * font size from the box HEIGHT -> uniform glyph height (uniform selection band);
            #  * one contiguous run -> get_text and a viewer's copy read it back verbatim, with NO
            #    spaces injected between glyphs (per-glyph placement leaves inter-box gaps that a
            #    viewer turns into spurious spaces — "字 與 字" — even when get_text looks clean);
            #  * scaled to the box width -> the run reaches its right edge and doesn't pile up left,
            #    so glyphs sit on their ink instead of drifting after narrow "）"/digits.
            nat = font.text_length(run_text, fontsize=fs)
            if nat <= 0:
                continue
            # Tz horizontal scale to fill the box width. Anchored/marker runs (word boxes
            # present) span DETECTED raster spread and may legitimately need a big stretch;
            # the no-word-box fallback spans the raw detection poly — the "short label in an
            # over-wide box" pathology — and keeps the tighter cap.
            anchored = bool(item.get("words")) and item.get("word_regions") is not None
            sx = min(bw / nat, _MAX_HSTRETCH if anchored else _MAX_HSTRETCH_FALLBACK)
            try:
                tw = fitz.TextWriter(new_page.rect)
                tw.append((0.0, baseline), run_text, font=font, fontsize=fs)
                # Matrix scales x by sx and translates the origin to the run's left edge; d=1 keeps
                # the height, so the horizontal scale never touches the uniform glyph size.
                tw.write_text(new_page, matrix=fitz.Matrix(sx, 0.0, 0.0, 1.0, px0, 0.0), render_mode=3)
            except Exception:
                continue
