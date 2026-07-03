# AGENTS.md

Guidance for AI agents and contributors working in this repository.

## What this is

Self-hosted, GPU-accelerated PDF OCR. Upload a PDF, get back a **searchable PDF**,
**Markdown**, or **Word (.docx)** — powered by PaddleOCR 3.7 (PP-OCRv6 + PaddleOCR-VL).

Architecture: a **TypeScript** monorepo (React SPA + Hono API + MCP, managed by
**Vite+**) talking to a headless **Python GPU worker** through a durable **SQLite**
job queue. The web layer never touches the GPU; the worker owns it.

```
apps/spa      React 19 + TS SPA (Vite+)          packages/shared  Zod contract (SPA + server)
apps/server   Hono API + SSE + static + MCP      worker/          Python GPU worker (PaddleOCR 3.7)
db/schema.sql canonical SQLite schema (the cross-language contract)
```

## Toolchain — Vite+ (`vp`)

This project uses **Vite+** (unified toolchain: Vite, Rolldown, Vitest, tsdown,
Oxlint, Oxfmt). Use the `vp` CLI, not npm/vite directly.

- `vp install` — install dependencies (pnpm, managed by vp)
- `vp dev` / `pnpm dev` — run the SPA dev server + API together
- `vp check` — format + lint + type-check (run before committing)
- `vp test` — run Vitest
- `vp build` / `vp run -r build` — production build (SPA `dist/` + bundled server)

The Python worker uses its own toolchain: `ruff`, `mypy`, `pytest` (see `worker/pyproject.toml`).

## Using the OCR service as an agent

The API is **async job-based**: submit → poll → download. Auth is optional
(set `API_KEY`; send it as `Authorization: Bearer <key>` or `X-API-Key: <key>`).

### Option A — MCP (recommended for tool-using agents)

A stdio MCP server exposes the service as tools. Run:

```bash
PDF_OCR_URL=http://127.0.0.1:8000 API_KEY=... node apps/server/dist/mcp-stdio.mjs
# (dev: `pnpm --filter server mcp`)
```

Tools: `submit_pdf(path, modes[], locale?)`, `get_job(id)`, `wait_for_job(id, timeout_seconds?)`,
`get_markdown(id)`, `download_result(id, out_path)`, `cancel_job(id)`.
`modes` ∈ `pdf | markdown | word`; `locale` ∈ `zh-TW | zh-CN | en`.

### Option B — REST API

Machine-readable schema at **`/openapi.json`**; Swagger UI at **`/docs`**.

```bash
# submit (markdown + word share one VL pass)
curl -F files=@doc.pdf -F modes=markdown -F modes=word -F locale=zh-TW \
     http://127.0.0.1:8000/api/v1/jobs
# -> {"jobs":[{"id":"...","mode":"markdown",...},{"id":"...","mode":"word",...}]}

# poll until status == "done" (or stream GET /api/v1/jobs/{id}/events)
curl http://127.0.0.1:8000/api/v1/jobs/<id>

# download the artifact (pdf / zip / docx by mode)
curl -OJ http://127.0.0.1:8000/api/v1/download/<id>
```

Job statuses: `queued → processing → saving → done` (or `error` / `cancelled`).

## Conventions

- Run `vp check` (TS) and `ruff check` + `mypy` (Python `worker/`) before committing.
- The SQLite schema in `db/schema.sql` is the single source of truth shared by the
  TS server (`node:sqlite`) and Python worker (`sqlite3`) — change all three together.
- Shared API types live in `packages/shared` (Zod) and are imported by both the SPA
  and the server; `/openapi.json` is generated from them.
