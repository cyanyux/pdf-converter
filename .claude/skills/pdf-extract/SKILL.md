---
name: pdf-extract
description: Convert PDFs into QA-verified searchable PDF, Markdown, or Word via the PDF Converter service, reconciling every output against the PDF's own text layer. Use when the user wants a PDF converted or documentation extracted with fidelity guarantees, e.g. "/pdf-extract <path.pdf> modes=markdown".
---

# pdf-extract — PDF → verified outputs

Convert each given PDF to the requested formats via the PDF Converter service,
then **prove** the result faithful before delivering it. Core principle: a
born-digital PDF's embedded text layer is the character-level ground truth —
no OCR/VLM output may disagree with it. The bundled reconciler enforces that
deterministically; you (the agent) judge and fix what it flags.

The service routes each job itself (job result reports `engine`):
born-digital markdown → `docling` (character-exact from the text layer);
scanned markdown/word → `paddleocr-vl`; searchable pdf → `pp-ocrv6`; and a
pdf-mode job on an already-searchable input returns the original with
`notice: "already_searchable"`. Your job is conversion orchestration + QA,
not backend choice.

## Arguments

`/pdf-extract <pdf...> [modes=pdf,markdown,word] [out=<dir>] [locale=zh-TW|zh-CN|en]`

- Default `modes=markdown`, `locale=zh-TW`, output next to each source PDF.
- "documentation for coding agents" style requests → markdown; "make this
  searchable" → pdf; "editable" → word.

## Tools & environment

- **PDF Converter service**: `$PDF_CONVERTER_URL`, default
  `http://127.0.0.1:5000`; add `-H "X-API-Key: $API_KEY"` if set. Preflight:
  `curl -sf $PDF_CONVERTER_URL/api/v1/health` — if down, tell the user to
  start it and stop.
- **Reconciler**: `.claude/skills/pdf-extract/scripts/reconcile.py` (repo-root
  relative). Run with `worker/.venv/bin/python` (has pymupdf, opencc,
  python-docx), else any python with pymupdf. Modes: `probe`, `check`
  (source PDF vs `.md`/`.docx`), `pdfcheck` (searchable-PDF text layer
  hygiene), `render` (page PNG).

## Workflow

Submit all jobs up front (GPU jobs process serially; docling jobs are fast and
CPU-side). For 2+ documents, run each document's QA as a parallel subagent
briefed with paths, mode, locale, and reconciler usage; review their reports
before the final summary.

### 1. Probe (sets QA expectations)

`<python> scripts/reconcile.py probe <pdf>` → per-page digital/raster counts.
Digital pages have a text-layer oracle for QA; raster pages get visual QA.
Predict the engine the service should pick (≥90% digital pages with
text-free raster pages → `docling` for markdown; all-digital → the pdf mode
short-circuits). Keep the JSON for the report.

### 2. Convert (one request per doc; markdown+word together)

```bash
curl -sf -F files=@<pdf> -F modes=<mode> [-F modes=<mode2>] -F locale=<locale> $PDF_CONVERTER_URL/api/v1/jobs
# poll GET /api/v1/jobs/<id> every ~10s: queued → processing → saving → done | error
# GET /api/v1/download/<id>  (pdf → .pdf, word → .docx, markdown → <name>-markdown.zip)
```

Docling jobs: ~1–2 s/page. VL jobs: ~10–20 s/page. On `error` report the
message; on 429 wait and resubmit; stuck job → `POST /api/v1/jobs/<id>/cancel`.

When the job finishes, compare `result.engine` against your step-1 prediction.
A mismatch isn't an error, but note it in the report and QA accordingly (a
VL-converted digital doc needs the full misread-hunting posture). If the
result carries `notice: "already_searchable"`, relay that to the user —
the original file is returned, nothing to QA.

### 3. Download and place

Unzip markdown in a scratchpad tempdir. Place outputs (default: the source
PDF's directory, or `out=`): `<name>.md` — images into `<name>-imgs/` (rename
from the zip's `imgs/` so multiple PDFs in one directory can't collide),
rewriting refs in the md; `.pdf`/`.docx` keep the document's name. Verify
every image ref resolves.

### 4. QA and fix (per format)

#### Markdown and Word — reconcile loop (the core)

```bash
<python> scripts/reconcile.py check <source.pdf> <output.md|output.docx> --locale <locale>
```

Reports, per page, text-layer lines missing from the output, each with its
closest fuzzy match (`closest_in_md`, `ratio`). Judge each miss:

- **High ratio (≳0.7), few chars differ** → misread. Fix the output: the layer
  is always right about characters. Typical (mostly on `paddleocr-vl` output;
  `docling` output copies the layer so misses are usually structural): rare
  hanzi or variant forms (歷→歴), ordinals (陸→隆), digits, identifier casing
  (`userID`→`userId`), zh-TW vocabulary drift (訊息→信息 — valid characters,
  wrong region; s2tw can't catch these), long tokens abbreviated (…此處省略…),
  and "corrected" source typos — if the source spells it `upsertSserver`, the
  output says `upsertSserver`.
- **Low ratio** → dropped content. Insert the layer text at the right spot —
  locate by page number and the neighboring lines that DID match.
- **Text inside a kept figure image** → fine to leave out of the body; note it.
- **`ignorable_missing`** (headers/footers/watermarks like 限閱, page numbers,
  lines repeating on 3+ pages) → usually expected; skim once for false
  ignorables.

Loop until `summary.clean` or every remaining miss is justified (record each).
Never delete content to silence a mismatch. For .docx, fix text via the
markdown twin when both were requested, or edit the docx XML carefully;
re-run `check` after edits.

Word extras: the file must load in python-docx (validity), tables keep merged
cells (no shifted columns), and the heading outline exists (Heading 1–6, not
all Normal).

#### Markdown/Word — structural QA (page-sampled + targeted)

Everywhere: heading hierarchy sane, every pipe table well-formed (consistent
column counts), no truncation (last page's content present). Then sample ~10%
of pages (min 3) plus **every** complex-table page:

```bash
<python> scripts/reconcile.py render <pdf> <page> <scratchpad>/page-N.png
```

Read the PNG vs the output: cell placement, merged cells, reading order, list
nesting. Any miss → check that whole section, fix, re-run `check` to confirm
the fix kept character fidelity. If a docling-converted doc has systemic
structural problems (mangled tables, scrambled reading order) that fixes
can't reasonably cover, report it as a routing-quality finding — the user may
want the VL engine for that document (no API override exists yet).

#### Searchable PDF

```bash
<python> scripts/reconcile.py pdfcheck <output.pdf> --locale <locale>
```

Must be: 0 NUL/control chars, 0 Simplified leaks, no unexpected
`empty_text_pages` (blank/image-only source pages are fine — render them to
confirm). Then sample ~10% of pages: render source and output at the same
zoom, confirm identical appearance, and extract the sampled pages' text — it
must read as coherent lines matching the visible content. Raster sources have
no character oracle, so cross-check numbers and identifiers you can read in
the render.

#### Raster pages in markdown/word (no oracle)

Raise visual sampling to ~30% of those pages; cross-check every number,
identifier, and code-like token against the render. Flag low-confidence spots
inline with `<!-- unverified: ... -->` (markdown only).

### 5. Report

Per document and mode: probe numbers + engine used (and whether it matched
your prediction), job id, output paths, misses found → fixed (concrete
examples), justified leftovers, structural fixes, pdfcheck summary,
unverified flags. This report is the user's evidence the conversion is
trustworthy — be concrete.

## Hard rules

- The text layer wins every character disagreement on digital pages.
- Don't hand-edit outputs for style; only fidelity fixes.
- Temp files (zips, renders) go in the session scratchpad.
- If the service is unreachable or a job errors, stop and report — don't
  substitute your own extraction and pass it off as the pipeline's output.
