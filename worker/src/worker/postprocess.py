"""Markdown/HTML/DOCX post-processing shared by the doc-parse pipelines.

Ports the Flask app's HTML-table/image -> Markdown conversion and Word style
fix-ups, plus multi-page DOCX merging via docxcompose (for native save_to_word).
"""

from __future__ import annotations

import base64
import contextlib
import logging
import re
import uuid
from collections.abc import Callable
from pathlib import Path

from bs4 import BeautifulSoup

from .text_utils import fix_ocr_text

log = logging.getLogger("worker.postprocess")

_TABLE_PATTERNS = [
    re.compile(
        r"<div[^>]*>\s*<html>\s*<body>\s*<table[^>]*>.*?</table>\s*</body>\s*</html>\s*</div>",
        re.DOTALL,
    ),
    re.compile(r'<div[^>]*class="[^"]*table[^"]*"[^>]*>.*?</div>', re.DOTALL),
    re.compile(r"<table[^>]*>.*?</table>", re.DOTALL),
]
_IMG_PATTERNS = [
    re.compile(r"<div[^>]*>\s*<img[^>]+/?>\s*</div>", re.DOTALL),
    re.compile(r"<img[^>]+/?>", re.DOTALL),
]
_EXT_BY_MEDIA = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
}


def html_table_to_markdown(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        return html
    rows = table.find_all("tr")
    if not rows:
        return html
    md_rows: list[list[str]] = []
    max_cols = 0
    for row in rows:
        cells = row.find_all(["td", "th"])
        row_cells: list[str] = []
        for cell in cells:
            colspan = int(str(cell.get("colspan") or 1))
            for br in cell.find_all("br"):
                br.replace_with(" ")
            text = " ".join(cell.get_text(separator=" ", strip=True).replace("|", "\\|").split())
            row_cells.append(text or " ")
            row_cells.extend([" "] * (colspan - 1))
        if row_cells:
            md_rows.append(row_cells)
            max_cols = max(max_cols, len(row_cells))
    if not md_rows:
        return html
    for r in md_rows:
        r.extend([" "] * (max_cols - len(r)))
    out = ["| " + " | ".join(md_rows[0]) + " |", "| " + " | ".join(["---"] * max_cols) + " |"]
    out.extend("| " + " | ".join(r) + " |" for r in md_rows[1:])
    return "\n\n" + "\n".join(out) + "\n\n"


def html_img_to_markdown(html: str, output_dir: Path | None) -> str:
    soup = BeautifulSoup(html, "html.parser")
    img = soup.find("img")
    if not img:
        return html
    src = str(img.get("src") or "")
    alt = str(img.get("alt") or "")
    if not src:
        return html
    if alt.strip().lower() == "image":
        alt = ""
    alt = alt.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    if src.startswith("data:"):
        if not (output_dir and output_dir.is_dir()):
            return f"\n\n<!-- Image: {alt or 'embedded'} (data URI) -->\n\n"
        m = re.match(r"data:([^;,]+)?(?:;base64)?,(.+)", src, re.DOTALL)
        if not m:
            return f"\n\n<!-- Image: {alt or 'embedded'} (invalid data URI) -->\n\n"
        ext = _EXT_BY_MEDIA.get(m.group(1) or "", ".bin")
        name = f"extracted_{uuid.uuid4().hex[:8]}{ext}"
        try:
            (output_dir / name).write_bytes(base64.b64decode(m.group(2)))
            src = name
        except Exception:
            return f"\n\n<!-- Image: {alt or 'embedded'} (extract failed) -->\n\n"
    src = src.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
    return f"\n\n![{alt}]({src})\n\n"


def _replace_all(text: str, patterns: list[re.Pattern[str]], fn: Callable[[str], str]) -> str:
    # Convert every match in one pass per pattern (unbounded; earlier patterns are more
    # specific, so they run first). fn receives the full matched chunk (m.group(0)).
    for pat in patterns:
        text = pat.sub(lambda m: fn(m.group(0)), text)
    return text


def process_markdown(md: str, output_dir: Path | None, locale: str | None, images: bool) -> str:
    """Fix S->T, convert HTML tables to md, optionally convert HTML <img> to md."""
    md = fix_ocr_text(md, locale)
    md = re.sub(r'alt="Image"', 'alt=""', md, flags=re.IGNORECASE)
    md = _replace_all(md, _TABLE_PATTERNS, html_table_to_markdown)
    if images:
        md = _replace_all(md, _IMG_PATTERNS, lambda chunk: html_img_to_markdown(chunk, output_dir))
    return md


def merge_docx(paths: list[Path], out_path: Path) -> None:
    """Merge multiple .docx into one (native save_to_word is per-result)."""
    from docx import Document

    if len(paths) == 1:
        paths[0].replace(out_path)
        return
    from docxcompose.composer import Composer

    master = Document(str(paths[0]))
    composer = Composer(master)
    for p in paths[1:]:
        composer.append(Document(str(p)))
    composer.save(str(out_path))


def fix_word_styles(docx_path: Path) -> None:
    """Force black text + add table borders (normalizes generator quirks)."""
    try:
        from docx import Document
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        from docx.shared import RGBColor

        doc = Document(str(docx_path))
        black = RGBColor(0, 0, 0)
        for para in doc.paragraphs:
            for run in para.runs:
                with contextlib.suppress(Exception):
                    run.font.color.rgb = black
        for table in doc.tables:
            try:
                tbl = table._tbl
                tblPr = tbl.tblPr if tbl.tblPr is not None else OxmlElement("w:tblPr")
                if tbl.tblPr is None:
                    tbl.insert(0, tblPr)
                borders = OxmlElement("w:tblBorders")
                for name in ("top", "left", "bottom", "right", "insideH", "insideV"):
                    el = OxmlElement(f"w:{name}")
                    el.set(qn("w:val"), "single")
                    el.set(qn("w:sz"), "4")
                    el.set(qn("w:space"), "0")
                    el.set(qn("w:color"), "000000")
                    borders.append(el)
                existing = tblPr.find(qn("w:tblBorders"))
                if existing is not None:
                    tblPr.remove(existing)
                tblPr.append(borders)
                for row in table.rows:
                    for cell in row.cells:
                        for para in cell.paragraphs:
                            for run in para.runs:
                                with contextlib.suppress(Exception):
                                    run.font.color.rgb = black
            except Exception as e:
                log.warning("table style fix skipped: %s", e)
        tmp = docx_path.with_suffix(".docx.tmp")
        doc.save(str(tmp))
        tmp.replace(docx_path)
    except Exception as e:
        log.warning("word style fix skipped: %s", e)
