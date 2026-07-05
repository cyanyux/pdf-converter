---
name: pdf-converter
description: Convert PDFs into QA-verified searchable PDF, Markdown, or Word via the networked PDF Converter service (default http://nvidia:5000), reconciling every output against the PDF's own text layer. For markdown you choose the engine — Docling (text-layer exact), PaddleOCR-VL (visual), or both compared and integrated. Use when the user wants a PDF converted or documentation extracted with fidelity guarantees, e.g. "/pdf-converter path.pdf modes=markdown".
---

# pdf-converter — PDF → verified outputs

Convert each given PDF to the requested formats via the PDF Converter service,
then **prove** the result faithful before delivering it. Core principle: a
born-digital PDF's embedded text layer is the character-level ground truth —
no OCR/VLM output may disagree with it. The bundled reconciler enforces that
deterministically; you (the agent) judge and fix what it flags.

Engines (each job result reports `engine`): searchable pdf → `pp-ocrv6`
(already-searchable input returns the original with
`notice: "already_searchable"`); word → `paddleocr-vl` always. Markdown has
two engines and **you choose** — see "Choosing the markdown engine".

## Arguments

`/pdf-converter <pdf...> [modes=pdf,markdown,word] [engine=auto|docling|vl|both] [out=<dir>] [locale=zh-TW|zh-CN|en]`

- Default `modes=markdown`, `engine=auto`, `locale=zh-TW`, output next to each
  source PDF. `engine` applies to markdown only. `both` is a strategy YOU
  implement with two submissions — the API accepts only `auto|docling|vl`;
  never send `engine=both` as a form field.
- "documentation for coding agents" style requests → markdown; "make this
  searchable" → pdf; "editable" → word.

## Tools & environment

You run on a **remote agent**: the PDF Converter service lives on another host
and you reach it over the network. You can't start, restart, or read logs from
it — if it's down, that's the user's to fix. Everything else (the source PDFs,
the reconciler, the downloaded outputs) is on your own machine.

- **PDF Converter service**: `$PDF_CONVERTER_URL`, default `http://nvidia:5000`.
  No auth (if a request returns 401, the deployment has since enabled auth —
  ask the user for the key and send it as `X-API-Key`). Preflight:
  `curl -sf $PDF_CONVERTER_URL/api/v1/health` — if it
  fails, tell the user the service is unreachable at that URL and stop. Never
  fall back to your own extraction and pass it off as the pipeline's output.
- **Reconciler**: `scripts/reconcile.py`, shipped next to this SKILL.md (invoke
  it by its path within the skill directory). Run it with **uv** — the script
  declares its deps inline (PEP 723), so `uv run scripts/reconcile.py <cmd> ...`
  provisions them automatically on first run; the only prerequisite is `uv` on
  PATH (install: https://docs.astral.sh/uv/). It reads only local files, never
  the network. Commands: `probe`, `check` (source PDF vs `.md`/`.docx`),
  `pdfcheck` (searchable-PDF text-layer hygiene), `render` (page PNG).

## Choosing the markdown engine

Two engines, different strengths (measured on the same 70-page born-digital
doc): `docling` copies the embedded text layer — character-exact (98.1%
coverage), fast, CPU — but is blind to raster content and can mangle complex
visual structure; `paddleocr-vl` reads pages visually on the GPU — handles
scans and hairy layouts (dense tables, multi-column reading order) — but
misreads characters (92.7% coverage: digits, rare hanzi, casing) that the
reconcile loop must then fix.

Pass `-F engine=docling|vl` when submitting (markdown jobs only; omit for
`auto`). The job JSON echoes the requested `engine` — if it's absent from
`GET /api/v1/jobs/<id>`, the deployment predates engine selection: tell the
user to update the service, and continue with `auto`.

- `engine=auto` (default): the service probe-routes — born-digital → docling,
  anything else → VL — which matches the measured winner for each input class.
- `engine=vl`: always honored.
- `engine=docling`: only valid for born-digital PDFs (≥90% digital pages,
  raster pages text-free — your step-1 probe predicts this). On a document
  that doesn't qualify the job **errors** rather than silently losing raster
  content; relay the message and resubmit with `engine=vl` or `auto`.

`engine=both`: submit two markdown jobs for the same file — one `docling`, one
`vl` — QA each, then integrate into one file: docling text is the character
base (the layer is ground truth); adopt VL's structure where docling's is
wrong. Re-run `check` on the merged file until clean, and note per-section
provenance in the report. The VL pass adds real cost (~10–20 s/page on a
serial GPU queue shared with other users), so escalate per document where QA
shows the need — don't default to `both`.

## Workflow

Submit all jobs up front (GPU jobs process serially; docling jobs are fast and
CPU-side). For 2+ documents, if your harness can spawn subagents, run each
document's QA as a parallel subagent briefed with paths, mode, locale, and
reconciler usage, then review their reports before the final summary; otherwise
QA them one at a time.

### 1. Probe (sets the engine decision + QA expectations)

`uv run scripts/reconcile.py probe <pdf>` → per-page digital/raster counts.
Digital pages have a text-layer oracle for QA; raster pages get visual QA.
For markdown, decide the engine now (see "Choosing the markdown engine");
all-digital + pdf mode short-circuits to the original. Keep the JSON for the
report.

### 2. Convert (one request per doc; markdown+word together)

```bash
curl -sf -F files=@<pdf> -F modes=<mode> [-F modes=<mode2>] [-F engine=docling|vl] -F locale=<locale> $PDF_CONVERTER_URL/api/v1/jobs
# poll GET /api/v1/jobs/<id> every ~10s: queued → processing → saving → done | error
# GET /api/v1/download/<id>  (pdf → .pdf, word → .docx, markdown → <name>-markdown.zip)
```

For `engine=both`, submit two requests for the same file (one `engine=docling`,
one `engine=vl`). Docling jobs: ~1–2 s/page. VL jobs: ~10–20 s/page. On
`error` report the message; on 429 wait and resubmit; stuck job →
`POST /api/v1/jobs/<id>/cancel`.

When the job finishes, check `result.engine` matches your step-1 decision. A
mismatch isn't an error, but note it in the report and QA accordingly (a
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
uv run scripts/reconcile.py check <source.pdf> <output.md|output.docx> --locale <locale>
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
uv run scripts/reconcile.py render <pdf> <page> <scratchpad>/page-N.png
```

Read the PNG vs the output: cell placement, merged cells, reading order, list
nesting. Any miss → check that whole section, fix, re-run `check` to confirm
the fix kept character fidelity. If a docling-converted doc has systemic
structural problems (mangled tables, scrambled reading order) that fixes
can't reasonably cover, escalate to `engine=both`: resubmit with `engine=vl`
and integrate per "Choosing the markdown engine".

#### Searchable PDF

```bash
uv run scripts/reconcile.py pdfcheck <output.pdf> --locale <locale>
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

Per document and mode: probe numbers, engine decision + engine(s) actually
used (for `both`: coverage numbers per engine and which sections came from
which), job ids, output paths, misses found → fixed (concrete examples),
justified leftovers, structural fixes, pdfcheck summary, unverified flags.
This report is the user's evidence the conversion is trustworthy — be
concrete.

## Hard rules

- The text layer wins every character disagreement on digital pages.
- Don't hand-edit outputs for style; only fidelity fixes.
- Temp files (zips, renders) go in the session scratchpad.
- If the service is unreachable or a job errors, stop and report — don't
  substitute your own extraction and pass it off as the pipeline's output.
