from pathlib import Path
from typing import Any

import numpy as np
import pymupdf as fitz

from worker.searchable_pdf import (
    _line_runs,
    _order_reading,
    _overlay_text,
    create_searchable_pdf,
    parse_ocr_result,
)


def _quad(x0: float, y0: float, x1: float, y1: float) -> list[list[float]]:
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


class _StubOCR:
    """Returns one text box per page; rec_polys/rec_scores are numpy arrays (as PP-OCR emits)."""

    def predict(self, images: list[np.ndarray], **kwargs: Any) -> list[dict[str, Any]]:
        out = []
        for img in images:
            w = img.shape[1]
            out.append(
                {
                    "rec_texts": ["測試文字"],
                    "rec_polys": np.array([[[10, 10], [w // 2, 10], [w // 2, 40], [10, 40]]]),
                    "rec_scores": np.array([0.99]),
                }
            )
        return out


def test_overlay_text_drops_glyphless_chars_no_nul_byte() -> None:
    # The built-in "cjk" font has no glyph for supplementary-plane CJK (e.g. U+205E3, which
    # s2t produces on the default zh-TW path). pymupdf used to write a literal NUL for such a
    # char, silently corrupting the searchable text layer. The overlay must drop it — never
    # emit a NUL — while keeping the BMP characters intact.
    font = fitz.Font("cjk")
    doc = fitz.open()
    page = doc.new_page(width=300, height=100)
    ocr_data = [{"text": "\U000205e3一二", "poly": [[10, 10], [200, 10], [200, 40], [10, 40]], "score": 1.0}]
    _overlay_text(page, ocr_data, page.rect, zoom=1.0, dpi=72, font=font)

    data = doc.tobytes(garbage=4, deflate=True)
    text = fitz.open(stream=data, filetype="pdf")[0].get_text()
    assert "\x00" not in text, "NUL byte leaked into the searchable text layer"
    assert "一二" in text  # BMP CJK survives the drop


def _overlay_chars(page: fitz.Page) -> list[dict[str, Any]]:
    return [
        ch for b in page.get_text("rawdict")["blocks"] for ln in b["lines"] for sp in ln["spans"] for ch in sp["chars"]
    ]


def test_overlay_text_uniform_height_and_contiguous() -> None:
    # The invisible layer must (1) use ONE font size per line so every character's selection
    # quad is the same height (no thick-CJK / thin-digit jitter — the "sometimes thick,
    # sometimes thin" bug), and (2) lay the string as a single contiguous run so get_text()
    # reads it back verbatim — no spaces injected between spread-apart glyphs — while natural
    # CJK/ASCII metrics keep each glyph over its ink and full-width CJK advances ~2x a digit.
    font = fitz.Font("cjk")
    doc = fitz.open()
    page = doc.new_page(width=400, height=200)
    # box 10..310 (bw=300) x 10..110 (bh=100); zoom=1 so image px == PDF pt.
    ocr = [{"text": "12一二", "poly": [[10, 10], [310, 10], [310, 110], [10, 110]], "score": 1.0}]
    _overlay_text(page, ocr, page.rect, zoom=1.0, dpi=200, font=font)

    chars = _overlay_chars(page)
    # (2) contiguous: the copied text is exactly the recognized string, no injected whitespace
    assert page.get_text().split() == ["12一二"]
    # (1) uniform height: every quad is within 1% of the same height
    heights = [ch["bbox"][3] - ch["bbox"][1] for ch in chars]
    assert max(heights) - min(heights) < 0.01 * max(heights)
    # placed left-to-right from the box's left edge; full-width CJK advances ~2x a half-width digit
    ox = {ch["c"]: ch["origin"][0] for ch in chars}
    assert abs(ox["1"] - 10) < 3  # starts at the box's left edge
    assert ox["1"] < ox["2"] < ox["一"] < ox["二"]
    assert (ox["二"] - ox["一"]) > 1.6 * (ox["2"] - ox["1"])
    # vertically centered on the box (center y = 60)
    cy = sum((ch["bbox"][1] + ch["bbox"][3]) / 2 for ch in chars) / len(chars)
    assert abs(cy - 60) < 8


def test_parse_ocr_result_line_level_and_score_gate() -> None:
    # One item per recognized line, tagged with its source-line index; a below-threshold line
    # is dropped.
    res = {
        "rec_texts": ["hello", "低分"],
        "rec_scores": [0.9, 0.1],
        "rec_polys": [_quad(0, 0, 50, 10), _quad(0, 20, 30, 30)],
    }
    items = parse_ocr_result(res, 0.5, "en")
    assert [it["text"] for it in items] == ["hello"]  # the 0.1-score line is gated out
    assert items[0]["line"] == 0


def test_order_reading_row_major_lines_contiguous() -> None:
    # A two-cell table row (left + right cell at the same y) must emit L->R, and the row below
    # comes after — regardless of input order.
    def line(t: str, x0: float, y0: float, idx: int) -> dict[str, Any]:
        return {"text": t, "poly": _quad(x0, y0, x0 + 10, y0 + 10), "score": 1.0, "line": idx}

    items = [
        line("R", 200, 0, 1),  # right cell (input out of order)
        line("L", 0, 0, 0),  # left cell, same visual row
        line("2", 0, 40, 2),  # next row down
    ]
    assert [it["text"] for it in _order_reading(items)] == ["L", "R", "2"]


def test_overlay_line_tz_fills_box_contiguous_no_gaps() -> None:
    # The searchable-layer invariants a PDF viewer's copy/selection depends on: a mixed-width
    # line (CJK + full-width paren + digits) is laid as ONE contiguous run scaled to fill the box
    # width. So (1) glyphs abut — NO inter-glyph gap that a viewer turns into a spurious space
    # ("字 與 字"); (2) the line spans the box left-to-right (no drift/under-fill); (3) uniform
    # height; (4) get_text reads it back verbatim.
    font = fitz.Font("cjk")
    doc = fitz.open()
    page = doc.new_page(width=400, height=100)
    box = _quad(50, 40, 350, 70)  # x=[50,350] (w=300), y=[40,70] (h=30); zoom=1
    _overlay_text(page, [{"text": "（三）戒護12", "poly": box, "score": 1.0, "line": 0}], page.rect, 1.0, 72, font)
    chars = _overlay_chars(page)
    xchars = sorted(chars, key=lambda c: c["bbox"][0])
    # (4) verbatim, (2) fills the box: first glyph at the left edge, last reaches the right edge
    assert page.get_text().split() == ["（三）戒護12"]
    assert abs(xchars[0]["bbox"][0] - 50) < 2 and abs(xchars[-1]["bbox"][2] - 350) < 2
    # (1) contiguous — no gap between consecutive glyph quads exceeds a small fraction of the size
    fs = xchars[0]["bbox"][3] - xchars[0]["bbox"][1]
    max_gap = max(xchars[i + 1]["bbox"][0] - xchars[i]["bbox"][2] for i in range(len(xchars) - 1))
    assert max_gap < 0.1 * fs, f"inter-glyph gap {max_gap} would copy as a space"
    # (3) uniform height
    heights = [c["bbox"][3] - c["bbox"][1] for c in chars]
    assert max(heights) - min(heights) < 0.01 * max(heights)


def _wregions(spans: list[tuple[float, float]]) -> list[list[list[float]]]:
    # Word regions as quads (y is irrelevant to the horizontal split logic).
    return [_quad(x0, 0, x1, 30) for x0, x1 in spans]


def test_line_runs_peels_hanging_indent_marker() -> None:
    # PP-OCR reads a hanging-indent list item as ONE line spanning the marker, the indent gap,
    # and the body. Laying it contiguously shoves the body left into the gap (invisible "監" lands
    # in the whitespace before the visible one). The marker must be peeled off so the body run
    # STARTS at its real detected indent (x0 of the first body word), not right after "5.".
    text = "5. 監造單位"
    words = ["5", ". ", "監", "造", "單", "位"]
    # 5|. abut; then a real gap (30 -> 55) before 監; body glyphs ~25px wide (median).
    regions = _wregions([(10, 18), (19, 30), (55, 80), (80, 105), (105, 130), (130, 155)])
    runs = _line_runs(text, words, regions, 10, 155)
    marker, body = _gap_groups(runs)
    assert _group_text(marker) == "5."  # marker text, trailing space stripped
    assert _group_text(body) == "監造單位"
    assert abs(body[0][1] - 55) < 0.01  # body starts at 監's true x0, NOT at ~30 (after the marker)


def test_line_runs_peels_bare_digit_marker() -> None:
    # A hanging-indent list marker with NO delimiter — a bare digit followed by the indent
    # whitespace ("1 專案…") — is what PP-OCR emits for many numbered lists. It must peel like
    # "1." does: the "1" run stays on its digit, the body starts at its true indent x0.
    text = "1 專案主持人"
    words = ["1", " ", "專", "案", "主", "持", "人"]
    # digit 108..114 (narrow), a REAL indent gap, body glyphs ~24px wide starting at 136.
    regions = _wregions([(108, 114), (114, 116), (136, 160), (160, 184), (184, 208), (208, 232), (232, 256)])
    runs = _line_runs(text, words, regions, 108, 256)
    marker, body = _gap_groups(runs)
    assert _group_text(marker) == "1"  # bare digit, trailing space stripped
    assert abs(marker[-1][2] - 114) < 0.01  # marker ends at the digit's ink, not across the gap
    assert _group_text(body) == "專案主持人"
    assert abs(body[0][1] - 136) < 0.01  # body starts at 專's true x0, NOT in the indent gap


def test_line_runs_peels_digit_glued_to_cjk() -> None:
    # PP-OCR can glue the digit to the first body glyph ("1啟動") when the print has no space,
    # yet the raster still hangs the indent. The bare-digit-lookahead alt peels just the digit;
    # the geometric gap between the digit ink and the body confirms the real hanging indent.
    text = "1啟動階段"
    words = ["1", "啟", "動", "階", "段"]
    # digit 108..114; real gap; body starts at 136.
    regions = _wregions([(108, 114), (136, 160), (160, 184), (184, 208), (208, 232)])
    runs = _line_runs(text, words, regions, 108, 232)
    marker, body = _gap_groups(runs)
    assert _group_text(marker) == "1"
    assert _group_text(body) == "啟動階段"
    assert abs(body[0][1] - 136) < 0.01


def test_line_runs_peels_circled_numeral_marker() -> None:
    # Single-glyph enclosed numerals (①②③ … ㉑ … ㊿, parenthesized ⑴, period ⒈) are markers with
    # no trailing delimiter; the regex admits the glyph itself and the confirming gap peels it.
    for marker_ch in ("①", "③", "㉑", "⑴", "⒈"):
        text = f"{marker_ch}專案主持人"
        words = [marker_ch, "專", "案", "主", "持", "人"]
        regions = _wregions([(108, 120), (136, 160), (160, 184), (184, 208), (208, 232), (232, 256)])
        runs = _line_runs(text, words, regions, 108, 256)
        marker, body = _gap_groups(runs)
        assert _group_text(marker) == marker_ch, f"{marker_ch!r} not peeled: {runs}"
        assert _group_text(body) == "專案主持人"
        assert abs(body[0][1] - 136) < 0.01


def test_line_runs_bare_digit_normal_prose_never_splits() -> None:
    # NEGATIVE (the safety invariant): a digit run in normal prose ("2026 年度預算") matches the
    # loose text pattern but has NO hanging-indent gap after it, so the dual signal must veto the
    # split — the line stays one abutting group (no marker peeled, no spurious copy-space).
    text = "2026 年度預算"
    words = ["2026", " ", "年", "度", "預", "算"]
    # normal spacing: digits 0..96, a thin space, body immediately after at 100 (gap ~4px << indent).
    regions = _wregions([(0, 96), (96, 100), (100, 124), (124, 148), (148, 172), (172, 196)])
    runs = _line_runs(text, words, regions, 0, 196)
    assert len(_gap_groups(runs)) == 1, f"prose split on a false marker: {runs}"
    assert "".join(t for t, _, _ in runs) == "2026 年度預算"


def test_overlay_text_snaps_band_and_edges_to_ink() -> None:
    # OCR detection boxes over/undershoot glyph ink by a line-varying margin (the user sees
    # random selection-band thickness and a "5." box leaning right of its digit). With the
    # rasterized page available, the overlay must take the font size from the measured ink ROWS
    # and snap run edges to the measured ink COLUMNS — not trust the detection box.
    font = fitz.Font("cjk")
    doc = fitz.open()
    page = doc.new_page(width=300, height=100)
    img = np.full((100, 300, 3), 255, dtype=np.uint8)
    img[45:57, 30:150] = 0  # true ink: 12px tall band, columns 30..150
    # Detection box overshoots the ink by a realistic margin: 25px tall (40..65), 6px wider each side.
    ocr_data = [{"text": "測試文字", "poly": _quad(24, 40, 156, 65), "score": 1.0}]
    _overlay_text(page, ocr_data, page.rect, zoom=1.0, dpi=72, font=font, page_img=img)

    chars = _overlay_chars(page)
    assert chars, "no text placed"
    heights = [c["bbox"][3] - c["bbox"][1] for c in chars]
    # Band height tracks the ink (12px + pads), NOT the 25px detection box.
    assert max(heights) < 19, f"selection band followed the detection box: {heights}"
    assert min(heights) > 11, f"selection band collapsed below the ink: {heights}"
    x0 = min(c["bbox"][0] for c in chars)
    x1 = max(c["bbox"][2] for c in chars)
    assert abs(x0 - 30) < 3, f"left edge not snapped to ink column: {x0}"
    assert abs(x1 - 151) < 3, f"right edge not snapped to ink column: {x1}"
    # Vertically the quad must cover the ink band.
    y0 = min(c["bbox"][1] for c in chars)
    y1 = max(c["bbox"][3] for c in chars)
    assert y0 < 46 and y1 > 56, f"quad missed the ink band: y=[{y0},{y1}]"


def test_overlay_band_inverted_polarity_white_on_dark() -> None:
    # Text inside a logo/seal is WHITE on a dark fill; the dark threshold would read the whole
    # decoration as "ink" and balloon the band. A majority-dark window must flip polarity so the
    # light glyphs are measured instead.
    font = fitz.Font("cjk")
    doc = fitz.open()
    page = doc.new_page(width=300, height=100)
    img = np.zeros((100, 300, 3), dtype=np.uint8)  # dark logo fill everywhere
    # WHITE glyph STROKES in rows 45..65 (every 3rd column — like real thin strokes, the light
    # pixels stay a minority of the window so the dark fill is the majority class).
    img[45:65, 30:150:3] = 255
    ocr_data = [{"text": "矯正署核", "poly": _quad(24, 40, 156, 70), "score": 1.0}]
    _overlay_text(page, ocr_data, page.rect, zoom=1.0, dpi=72, font=font, page_img=img)
    chars = _overlay_chars(page)
    y0 = min(c["bbox"][1] for c in chars)
    y1 = max(c["bbox"][3] for c in chars)
    assert y0 < 45 and y1 > 65, f"quad missed the light glyph band: y=[{y0},{y1}]"
    assert y1 - y0 < 27, f"band ballooned to the dark fill: {y1 - y0}"


def test_overlay_text_without_page_img_keeps_box_metrics() -> None:
    # No raster available (fallback path) -> the detection box still drives the metrics.
    font = fitz.Font("cjk")
    doc = fitz.open()
    page = doc.new_page(width=300, height=100)
    ocr_data = [{"text": "測試文字", "poly": _quad(10, 20, 170, 80), "score": 1.0}]
    _overlay_text(page, ocr_data, page.rect, zoom=1.0, dpi=72, font=font)
    chars = _overlay_chars(page)
    heights = [c["bbox"][3] - c["bbox"][1] for c in chars]
    assert all(h > 40 for h in heights), f"box-height fallback lost: {heights}"


def test_overlay_text_ink_snap_ignores_far_ink() -> None:
    # Ink far outside the run edge (a table rule / neighboring column) must NOT drag the edge:
    # the snap distrusts shifts beyond its cap and keeps the OCR edge.
    font = fitz.Font("cjk")
    doc = fitz.open()
    page = doc.new_page(width=400, height=100)
    img = np.full((100, 400, 3), 255, dtype=np.uint8)
    img[45:57, 30:150] = 0  # the line's true ink
    img[20:80, 300:304] = 0  # a vertical table rule well right of the text
    ocr_data = [{"text": "測試文字", "poly": _quad(10, 40, 310, 65), "score": 1.0}]
    _overlay_text(page, ocr_data, page.rect, zoom=1.0, dpi=72, font=font, page_img=img)
    chars = _overlay_chars(page)
    # The ink starts at column 30, but that is 20px from the OCR edge (10) — beyond the shift
    # cap for this band height, so the snap must distrust it and keep the OCR edge.
    x0 = min(c["bbox"][0] for c in chars)
    assert abs(x0 - 10) < 3, f"left edge took a shift beyond the cap: {x0}"


def test_line_runs_trims_trailing_space_from_marker_edge() -> None:
    # PP-OCR fuses the space after a marker into the dot token (". "), and that token's region
    # spans the indent gap. The marker run must end at the DOT's ink share, not the fused region's
    # right edge — otherwise the stretched "5" leans right of the visible glyph (user-reported).
    text = "5. 監造單位"
    words = ["5", ". ", "監", "造", "單", "位"]
    # ". " region runs 19->40: half dot, half space (char-count prorate under the default measure).
    regions = _wregions([(10, 18), (19, 40), (55, 80), (80, 105), (105, 130), (130, 155)])
    runs = _line_runs(text, words, regions, 10, 155)
    marker, body = _gap_groups(runs)
    assert _group_text(marker) == "5."
    assert abs(marker[0][1] - 10) < 0.01
    assert abs(marker[-1][2] - 29.5) < 0.01  # 19 + (40-19) * 1/2, NOT 40 (the fused region's edge)
    assert _group_text(body) == "監造單位"


def test_line_runs_skips_whitespace_only_token_for_body_start() -> None:
    # A standalone " " token sits in the indent gap; the body extent must start at the first INK
    # token (監), and the gap check must not be masked by the space token's region.
    text = "5. 監造單位"
    words = ["5", ".", " ", "監", "造", "單", "位"]
    regions = _wregions([(10, 18), (19, 24), (25, 54), (55, 80), (80, 105), (105, 130), (130, 155)])
    runs = _line_runs(text, words, regions, 10, 155)
    marker, body = _gap_groups(runs)
    assert _group_text(marker) == "5."
    assert abs(marker[-1][2] - 24) < 0.01  # marker ends at the dot's region, not the space's
    assert _group_text(body) == "監造單位"
    assert abs(body[0][1] - 55) < 0.01  # body starts at 監's ink, not at the space token


def _assert_abutting(runs: list[tuple[str, float, float]], lo: float, hi: float) -> None:
    # Runs must tile [lo, hi] with ZERO gaps: abutting boundaries are what make anchor
    # subdivision safe — no viewer/extractor can synthesize a space at a zero-width gap.
    assert abs(runs[0][1] - lo) < 0.01 and abs(runs[-1][2] - hi) < 0.01
    for i in range(len(runs) - 1):
        assert abs(runs[i][2] - runs[i + 1][1]) < 0.01, f"gap between runs {i} and {i + 1}: {runs}"
    for _t, a, b in runs:
        assert a < b


def _gap_groups(runs: list[tuple[str, float, float]]) -> list[list[tuple[str, float, float]]]:
    # Split runs into maximal ABUTTING groups; group boundaries are the REAL gaps (the marker
    # indent). Group-level assertions stay stable however many drift anchors subdivide a group.
    groups = [[runs[0]]]
    for r in runs[1:]:
        if abs(r[1] - groups[-1][-1][2]) < 0.01:
            groups[-1].append(r)
        else:
            groups.append([r])
    return groups


def _group_text(g: list[tuple[str, float, float]]) -> str:
    return "".join(t for t, _, _ in g)


def test_line_runs_trims_leading_space_from_body_edge() -> None:
    # Leading whitespace fused into the body's first token (" 監") must not drag the body run
    # left into the indent gap.
    text = "5. 監造單位"
    words = ["5", ".", " 監", "造", "單", "位"]
    # " 監" region 30->80: 1 space char + 1 ink char -> body starts at the ink half (55).
    regions = _wregions([(10, 18), (19, 24), (30, 80), (80, 105), (105, 130), (130, 155)])
    runs = _line_runs(text, words, regions, 10, 155)
    marker, body = _gap_groups(runs)
    assert _group_text(marker) == "5."
    assert abs(body[0][1] - 55) < 0.01  # 30 + (80-30) * 1/2, not the fused region's 30
    assert _group_text(body) == "監造單位"
    _assert_abutting(body, body[0][1], 155)  # drift anchors may subdivide, but never leave gaps


def test_line_runs_keeps_glued_marker_gapless() -> None:
    # A marker-shaped prefix with NO hanging-indent gap (e.g. the decimal "1.5") must NOT be
    # separated by a real gap — the geometry (small gap) vetoes the text pattern. Drift anchors
    # may still subdivide the line, but only into exactly ABUTTING runs (no gap anywhere means
    # no viewer can inject a space into "1.5倍" on copy).
    words = ["1", ".", "5", "倍"]
    regions = _wregions([(10, 18), (19, 24), (25, 33), (34, 59)])  # all abutting
    runs = _line_runs("1.5倍", words, regions, 10, 59)
    assert "".join(t for t, _, _ in runs) == "1.5倍"
    _assert_abutting(runs, 10, 59)


def test_line_runs_anchors_correct_fullwidth_advance_drift() -> None:
    # The raster prints fullwidth "（六）" but the rec model normalizes to halfwidth "(六)", whose
    # font advances are a fraction of the printed glyph width — a uniform stretch then shifts
    # every glyph after the parens left of its ink (user-reported: the 戒 selection box sits on
    # the "）"). Drift anchors must re-sync at the detected positions with exactly abutting runs.
    text = "(六)戒護安全"
    words = ["(", "六", ")", "戒", "護", "安", "全"]
    # Raster: parens are FULLWIDTH (24px each) like the CJK glyphs; uniform text = 168px.
    regions = _wregions([(0, 24), (24, 48), (48, 72), (72, 96), (96, 120), (120, 144), (144, 168)])

    # measure mimicking the font: halfwidth 1 unit for ( ) — CJK 2 units.
    def adv(s: str) -> float:
        return sum(1.0 if c in "()" else 2.0 for c in s)

    runs = _line_runs(text, words, regions, 0, 168, measure=adv)
    _assert_abutting(runs, 0, 168)
    assert "".join(t for t, _, _ in runs) == text
    # 戒 must be re-anchored at (or very near) its true ink x0=72 — under a uniform stretch its
    # advance-based position is 4/12 * 168 = 56, a full glyph off.
    starts = {t[0]: x0 for t, x0, _ in runs}
    anchored_at = min((abs(x0 - 72), x0) for t, x0, _ in runs if t.startswith("戒"))[1]
    assert abs(anchored_at - 72) < 6, f"戒 not re-anchored near its ink: {runs} {starts}"


def test_overlay_band_covers_ink_with_margin() -> None:
    # The selection quad must COVER the glyph ink with a small margin — sized exactly to the ink
    # extent, ascender tips visibly poke out of the highlight (user-reported 字會突出).
    font = fitz.Font("cjk")
    doc = fitz.open()
    page = doc.new_page(width=300, height=100)
    img = np.full((100, 300, 3), 255, dtype=np.uint8)
    img[45:57, 30:150] = 0  # ink rows 45..56
    ocr_data = [{"text": "測試文字", "poly": _quad(24, 40, 156, 65), "score": 1.0}]
    _overlay_text(page, ocr_data, page.rect, zoom=1.0, dpi=72, font=font, page_img=img)
    chars = _overlay_chars(page)
    y0 = min(c["bbox"][1] for c in chars)
    y1 = max(c["bbox"][3] for c in chars)
    assert y0 < 45 and y1 > 57, f"quad does not cover the ink: y=[{y0},{y1}] vs ink [45,57)"
    assert y1 - y0 < 18, f"quad ballooned: {y1 - y0}"


def test_line_runs_use_converted_text_not_raw_words() -> None:
    # On the zh-TW path `text` is s2t-CONVERTED while `words` are the recognizer's RAW
    # (Simplified) tokens. Run text must be sliced from `text` — joining raw words shipped
    # Simplified characters into the searchable layer (caught by the all-format QA round).
    text = "5. 國家實現"  # converted
    words = ["5", ". ", "国", "家", "实", "现"]  # raw Simplified tokens, same lengths
    regions = _wregions([(10, 18), (19, 30), (55, 80), (80, 105), (105, 130), (130, 155)])
    runs = _line_runs(text, words, regions, 10, 155)
    joined = "".join(t for t, _, _ in runs)
    assert "国" not in joined and "实" not in joined and "现" not in joined
    assert "國家實現" in joined
    # And the anchored no-marker path too (uniform regions -> may or may not anchor):
    runs2 = _line_runs("國家實現開發", ["国", "家", "实", "现", "开", "发"],
                       _wregions([(0, 24), (24, 48), (48, 72), (72, 96), (96, 120), (120, 144)]), 0, 144)
    joined2 = "".join(t for t, _, _ in runs2)
    assert joined2 == "國家實現開發"


def test_line_runs_strips_han_space_from_placed_text() -> None:
    # A stray Han<->Han OCR space must not ship in the searchable layer (markdown/docx already
    # strip it). Both the one-run fallback and the token path must drop it.
    assert _line_runs("總務 科辦公室", None, None, 0, 300) == [("總務科辦公室", 0, 300)]
    runs = _line_runs("總務 科", ["總", "務", " ", "科"],
                      _wregions([(0, 24), (24, 48), (48, 60), (60, 84)]), 0, 84)
    assert "".join(t for t, _, _ in runs).replace(" ", "") == "總務科"
    for t, _a, _b in runs:
        assert "務 科" not in t


def test_line_runs_no_marker_and_no_wordboxes_stay_one_run() -> None:
    # A body line with no leading marker stays one run (no mid-sentence split -> no copy-spaces).
    body = _line_runs("監造單位有督察工程進行", ["監", "造"], _wregions([(0, 25), (25, 50)]), 0, 400)
    assert body == [("監造單位有督察工程進行", 0, 400)]
    # And with word boxes absent entirely (per-image fallback / older result), also one run.
    assert _line_runs("5. 監造單位", None, None, 5, 300) == [("5. 監造單位", 5, 300)]


def test_line_runs_empty_word_region_falls_back_to_one_run() -> None:
    # A degenerate word region (empty poly []) previously made _word_bboxes' min()/max() raise
    # ValueError, and _line_runs has no try — so it killed the whole page's text layer. The
    # guard must instead bail to the safe single-run path (never raise).
    text = "5. 監造單位"
    words = ["5", ". ", "監", "造", "單", "位"]
    regions: list[list[list[float]]] = _wregions([(10, 18), (19, 30), (55, 80), (80, 105), (105, 130)])
    regions.insert(2, [])  # one point-less region among otherwise valid ones (len still matches)
    runs = _line_runs(text, words, regions, 10, 155)
    assert runs == [("5. 監造單位", 10, 155)]  # whole line as one run, no exception


def test_line_runs_non_monotonic_regions_never_invert() -> None:
    # PP-OCR word regions can be non-monotonic (detection jitter): a later body token boxed LEFT
    # of the first. The body run must still span min..max over ALL its ink tokens — never invert
    # (rx0 > rx1), which _overlay_text would silently drop (bw < 1), deleting the body from the
    # searchable text layer.
    text = "5. 監造單位"
    words = ["5", ". ", "監", "造", "單", "位"]
    # Body tokens 監造單 run right, but the LAST body token 位 is boxed far left (jitter).
    regions = _wregions([(10, 18), (19, 30), (55, 80), (80, 105), (105, 130), (40, 50)])
    runs = _line_runs(text, words, regions, 10, 155)
    marker, body = _gap_groups(runs)
    assert _group_text(marker) == "5."
    assert _group_text(body) == "監造單位"
    # The body spans min..max over ALL its ink tokens (40..130), not (first.x0, last.x1) which
    # would invert to (55, 50); the jittered 位 region can subdivide nothing (an anchor must be
    # strictly monotonic), so every run stays rx0 < rx1 and the tiling has no gaps.
    _assert_abutting(body, 40, 130)


def test_line_runs_pathological_regions_fall_back_never_invert() -> None:
    # A heavily non-monotonic marker region set (later marker token boxed left of the first) must
    # never yield an inverted marker run; the worst case is a fall-back to a single run.
    text = "（1）本文"
    words = ["（", "1", "）", "本", "文"]
    # Marker tokens deliberately out of order (（=100..120, 1=60..80, ）=40..55); body normal.
    regions = _wregions([(100, 120), (60, 80), (40, 55), (200, 230), (230, 260)])
    runs = _line_runs(text, words, regions, 40, 260)
    for _t, rx0, rx1 in runs:
        assert rx0 < rx1, f"inverted run emitted: {(_t, rx0, rx1)}"


def test_overlay_capped_abutting_run_no_synthesized_newline() -> None:
    # Finding #9: when an abutting run's stretch was capped (old _MAX_HSTRETCH=4.0), its glyphs
    # under-filled the box span, leaving a horizontal gap before the NEXT abutting run — and since
    # each run is its own write_text text object, pymupdf synthesizes a NEWLINE into get_text
    # between them, so get_text no longer reads the line back verbatim.
    #
    # Trigger: a lone narrow token ("、") whose detected region occupies a WIDE justified cell, then
    # the CJK body abuts. A drift anchor splits ('、', wide) | ('監造單位工程', ...) with '、' needing a
    # ~6x stretch — above the old 4.0 cap, below the new 8.0 cap.
    import worker.searchable_pdf as sp

    font = fitz.Font("cjk")
    text = "、監造單位工程"
    words = ["、", "監", "造", "單", "位", "工", "程"]
    # "、" over a wide cell [50,230] (~180px, ~6x its own advance); body abuts [230,374] at 24px each.
    regions = _wregions([(50, 230), (230, 254), (254, 278), (278, 302), (302, 326), (326, 350), (350, 374)])
    ocr = {"text": text, "poly": _quad(50, 40, 374, 70), "score": 1.0, "line": 0,
           "words": words, "word_regions": regions}

    def _get_text(cap: float) -> str:
        old = sp._MAX_HSTRETCH
        sp._MAX_HSTRETCH = cap
        try:
            doc = fitz.open()
            page = doc.new_page(width=500, height=100)
            sp._overlay_text(page, [dict(ocr)], page.rect, 1.0, 72, font)
            data = doc.tobytes(garbage=4, deflate=True)
            return str(fitz.open(stream=data, filetype="pdf")[0].get_text())
        finally:
            sp._MAX_HSTRETCH = old

    # REPRODUCTION: the old 4.0 cap under-fills -> a newline splits the line in get_text.
    assert _get_text(4.0).strip() != text, "reproduction failed: old cap did not inject a newline"
    assert "\n" in _get_text(4.0).strip()
    # FIX: the current cap lets the run fill its box -> abuts -> get_text reads the line verbatim.
    assert _get_text(sp._MAX_HSTRETCH).strip() == text, "capped run still injects a newline"


def test_overlay_line_tz_shrinks_when_text_wider_than_box() -> None:
    # A long line in a short box scales DOWN (sx < 1) to fit — never spills past the right edge.
    font = fitz.Font("cjk")
    doc = fitz.open()
    page = doc.new_page(width=400, height=100)
    box = _quad(10, 10, 110, 40)  # narrow box for 10 CJK chars
    _overlay_text(
        page, [{"text": "一二三四五六七八九十", "poly": box, "score": 1.0, "line": 0}], page.rect, 1.0, 72, font
    )
    right = max(c["bbox"][2] for c in _overlay_chars(page))
    assert right <= 110 + 2


def test_searchable_pdf_sets_clean_title_over_mojibake(tmp_path: Path) -> None:
    # Source PDFs (esp. "Microsoft: Print To PDF") carry a mojibaked /Title that Chrome shows as
    # the caption of an inline-viewed PDF. The output must carry the clean filename instead, and
    # drop the source /Author (it can hold stray control chars).
    src = fitz.open()
    src.new_page(width=200, height=200)
    src.set_metadata({"title": "03.\x0b‹@-", "author": "se\t"})
    inp = tmp_path / "in.pdf"
    src.save(str(inp))
    src.close()
    outp = tmp_path / "out.pdf"
    create_searchable_pdf(
        _StubOCR(),
        str(inp),
        str(outp),
        lambda *a: None,
        lambda: False,
        locale="zh-TW",
        dpi=100,
        title="16-需求說明書",
    )
    meta = fitz.open(str(outp)).metadata
    assert meta is not None
    assert meta["title"] == "16-需求說明書"
    assert not meta.get("author")


def test_searchable_pdf_rotated_page_not_double_rotated(tmp_path: Path) -> None:
    # A /Rotate=90 page must produce an output page at the VISUAL (rotated) rect, not a
    # double-rotated one, and the text layer must land on it. Regression: the old prerotate
    # rotated a second time, distorting the image and misaligning the text.
    src = fitz.open()
    page = src.new_page(width=612, height=792)  # portrait
    page.set_rotation(90)  # visual rect becomes 792 x 612 (landscape)
    inp = tmp_path / "in.pdf"
    src.save(str(inp))
    src.close()

    outp = tmp_path / "out.pdf"
    result = create_searchable_pdf(
        _StubOCR(), str(inp), str(outp), lambda *a: None, lambda: False, locale="zh-TW", dpi=150
    )
    assert result["total_pages"] == 1

    out = fitz.open(str(outp))
    # output page matches the source's VISUAL rect (landscape), i.e. not double-rotated
    assert (round(out[0].rect.width), round(out[0].rect.height)) == (792, 612)
    text = out[0].get_text()
    assert "測試文字" in text and "\x00" not in text  # text layer present + numpy poly path OK
