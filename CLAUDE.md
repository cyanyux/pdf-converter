## What this is

**PDF Converter** — self-hosted PDF conversion. Upload a PDF, get back a **searchable PDF**,
**Markdown**, or **Word (.docx)**. Each job is probed and routed to the highest-fidelity
engine: born-digital PDFs → **Docling** (CPU, characters copied from the embedded text
layer — never OCR'd); scanned PDFs → **PaddleOCR 3.7** (PP-OCRv6 for the searchable-PDF
text layer, PaddleOCR-VL for Markdown/Word) on the GPU. A pdf-mode job whose input is
already fully searchable is returned as-is (`notice: "already_searchable"`). Markdown
routing can be pinned per job via `engine=docling|vl` (default `auto` = probe routing;
a docling pin on a non-qualifying document fails the job rather than losing raster
content).

Architecture: a **TypeScript** monorepo (React SPA + Hono API + MCP, managed by
**Vite+**) talking to a headless **Python worker** through a durable **SQLite**
job queue. The web layer never touches the GPU; the worker owns it. Docling children
run with `CUDA_VISIBLE_DEVICES=""` — only PaddleOCR children may use VRAM.

```
apps/spa      React 19 + TS SPA (Vite+)          packages/shared  Zod contract (SPA + server)
apps/server   Hono API + SSE + static + MCP      worker/          Python worker (probe → Docling | PaddleOCR)
db/schema.sql canonical SQLite schema (the cross-language contract)
```

Routing lives in `worker/src/worker/probe.py` (`route_markdown`, `is_already_searchable`);
the digital markdown pipeline is `worker/src/worker/docparse_digital.py` (byte-faithful:
no s2tw / fix_ocr_text — the text layer is ground truth). Every job result carries
`engine`: `docling` | `paddleocr-vl` | `pp-ocrv6` | `none`.

## Toolchain — Vite+ (`vp`)

This project uses **Vite+** (unified toolchain: Vite, Rolldown, Vitest, tsdown,
Oxlint, Oxfmt). Use the `vp` CLI, not npm/vite directly.

- `vp install` — install dependencies (pnpm, managed by vp)
- `vp dev` / `pnpm dev` — run the SPA dev server + API together
- `vp check` — format + lint + type-check (run before committing)
- `vp test` — run Vitest
- `vp build` / `vp run -r build` — production build (SPA `dist/` + bundled server)

The Python worker uses its own toolchain: `ruff`, `mypy`, `pytest` (see `worker/pyproject.toml`).
Its venv needs CPU-only torch installed BEFORE `-e ".[dev]"` (see README dev section) —
CUDA torch must never enter the venv or the Docker image.

## Using the service as an agent

The API is **async job-based**: submit → poll → download. Auth is optional
(set `API_KEY`; send it as `Authorization: Bearer <key>` or `X-API-Key: <key>`).

Endpoint: the self-hosted service listens on **port 5000** (the Docker image
sets `PORT=5000`; a bare `vp dev` / `pnpm dev` server uses the code default
`8000`). Point agents at its host — e.g. `http://nvidia:5000` from elsewhere on
the LAN, or `http://127.0.0.1:5000` on the box itself. The examples below use
`http://nvidia:5000`; override via `$PDF_CONVERTER_URL`.

### Option A — MCP (recommended for tool-using agents)

A stdio MCP server exposes the service as tools. Run:

```bash
PDF_CONVERTER_URL=http://nvidia:5000 API_KEY=... node apps/server/dist/mcp-stdio.mjs
# (dev: `pnpm --filter server mcp`)
```

Tools: `submit_pdf(path, modes[], locale?)`, `get_job(id)`, `wait_for_job(id, timeout_seconds?)`,
`get_markdown(id)`, `download_result(id, out_path)`, `cancel_job(id)`.
`modes` ∈ `pdf | markdown | word`; `locale` ∈ `zh-TW | zh-CN | en`.

### Option B — REST API

Machine-readable schema at **`/openapi.json`**; Swagger UI at **`/docs`**.

```bash
# submit (markdown + word share one VL pass when both route to VL);
# optional -F engine=docling|vl pins the markdown engine (default auto)
curl -F files=@doc.pdf -F modes=markdown -F modes=word -F locale=zh-TW \
     http://nvidia:5000/api/v1/jobs
# -> {"jobs":[{"id":"...","mode":"markdown",...},{"id":"...","mode":"word",...}]}

# poll until status == "done" (or stream GET /api/v1/jobs/{id}/events)
curl http://nvidia:5000/api/v1/jobs/<id>

# download the artifact (pdf / zip / docx by mode)
curl -OJ http://nvidia:5000/api/v1/download/<id>
```

Job statuses: `queued → processing → saving → done` (or `error` / `cancelled`).
For verified extractions with a QA loop, use the `/pdf-converter` skill
(`.claude/skills/pdf-converter/`) on top of this API.

## Conventions

- Run `vp check` (TS) and `ruff check` + `mypy` (Python `worker/`) before committing.
- The SQLite schema in `db/schema.sql` is the single source of truth shared by the
  TS server (`node:sqlite`) and Python worker (`sqlite3`) — change all three together.
- Shared API types live in `packages/shared` (Zod) and are imported by both the SPA
  and the server; `/openapi.json` is generated from them.
- Env vars are `PDF_CONVERTER_*` (renamed from `PDF_OCR_*`, no fallbacks — the
  entrypoint warns if legacy vars are set).
- Group invariants (markdown+word dual export): each job is saved exactly once; a
  Docling child produces ONLY its claimed job; a VL child declines markdown siblings
  destined for Docling (pinned via `engine` or probe-routed). Cancellation always
  resolves the whole group.

## Agent coordination

- Use Fable for planning and coordination. For anything you can scope into a clean subtask, start a Opus 4.8 xHigh subagent.

- Give each subagent a clear goal, the relevant context, and what to bring back. Don't have them invent the plan. Run independent pieces in parallel.

- When they return, review the results before you merge anything. If something's off, rewrite the brief and spin another, don't silently patch over it yourself unless it's trivial.
