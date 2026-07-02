# PDF OCR

Self-hosted, GPU-accelerated OCR web app that converts scanned or image-based PDFs into
**searchable PDF**, **Markdown**, or **Word (.docx)** — with a clean web UI and a first-class
API/MCP surface for AI agents.

Built on **PaddleOCR 3.7** (PP-OCRv6 for the searchable-PDF text layer, PaddleOCR-VL for
document parsing) with strong Traditional/Simplified Chinese and multilingual accuracy.
Everything runs on your own hardware — no data leaves your network.

## Features

- **Three outputs from one upload** — searchable PDF (invisible, selectable text layer),
  Markdown (with extracted images/tables), and editable Word. Markdown + Word are produced
  from a single vision-language pass.
- **GPU-accelerated** — CUDA via PaddlePaddle; a dedicated worker owns the GPU and swaps
  models by tearing children down so VRAM is reclaimed cleanly (fits a 12 GB card).
- **Agent-friendly** — documented REST API (`/openapi.json`, Swagger UI at `/docs`) and an
  **MCP server** so tools like Claude can submit and fetch conversions directly.
- **Reliable by design** — durable SQLite job queue (jobs survive restarts), crash-recovery
  reaper, per-page cancellation, streamed uploads, retention GC.
- **Multilingual UI** — Traditional Chinese, Simplified Chinese, English; automatic
  Simplified→Traditional correction for zh-TW users.
- **Single-container deploy** — one `docker compose up`.

## Quick start

**Prerequisites:** NVIDIA GPU + driver, Docker, and the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).

```bash
git clone https://github.com/cyanyux/pdf-ocr.git
cd pdf-ocr
docker compose up -d --build
```

Open **http://localhost:5000** and start uploading PDFs.

> First launch downloads model weights (~2 GB) on the first conversion; they're cached in a
> volume, so subsequent runs are fast.

## Architecture

```
Browser / AI agent ──HTTP+SSE / MCP──▶ TypeScript server (Hono, Node)
                                        · REST API + SSE + static SPA + MCP + auth
                                        │  enqueue / read
                                        ▼
                              SQLite (WAL) job queue  ◀──▶  shared files
                                        ▲  claim / write
                                        │
                              Python GPU worker (PaddleOCR 3.7)
                                · one model child at a time (VRAM-safe)
                                · PP-OCRv6 · PaddleOCR-VL · PyMuPDF · pandoc
```

The whole human/agent-facing surface is **TypeScript** (managed by [Vite+](https://viteplus.dev));
Python is reduced to a headless GPU worker. The SQLite schema (`db/schema.sql`) is the
language-agnostic contract between them.

## Configuration

Environment variables (set in `docker-compose.yml`); all optional.

| Variable | Default | Description |
|---|---|---|
| `API_KEY` | — | If set, require it (`Authorization: Bearer` / `X-API-Key`) on the API + MCP |
| `PDF_OCR_DEVICE` | `gpu:0` | `cpu` to disable GPU |
| `PDF_OCR_OCR_VERSION` | `PP-OCRv6` | Searchable-PDF OCR model (`PP-OCRv5` for continuity) |
| `PDF_OCR_DOCX_BACKEND` | `native` | `native` (save_to_word) or `pandoc` |
| `PDF_OCR_MAX_UPLOAD_MB` | `500` | Max upload size per file |
| `PDF_OCR_MAX_QUEUE` | `100` | Reject new jobs above this queue depth (429) |
| `PDF_OCR_JOB_MAX_AGE` | `7200` | Retention (seconds) for outputs and job rows |
| `PDF_OCR_ENABLE_HPI` | `1` | Install/use HPI (ONNX Runtime/OpenVINO) acceleration on first boot |

## API & agents

- **OpenAPI:** `GET /openapi.json`, Swagger UI at `/docs`.
- **MCP (stdio):** `node apps/server/dist/mcp-stdio.mjs` (env `PDF_OCR_URL`, `API_KEY`).
  Tools: `submit_pdf`, `get_job`, `wait_for_job`, `get_markdown`, `download_result`, `cancel_job`.
- **REST flow:** `POST /api/v1/jobs` (multipart `files`, `modes`, `locale`) → poll
  `GET /api/v1/jobs/{id}` (or SSE `…/events`) → `GET /api/v1/download/{id}`.

See [AGENTS.md](AGENTS.md) for details.

## Development

Uses **Vite+** (`vp`) for the TypeScript workspace and a `uv`/venv for the Python worker.

```bash
vp install            # install JS deps (pnpm, managed by vp)
vp dev                # SPA dev server + Hono API
vp check              # format + lint + type-check (TS7 native)
vp test               # Vitest

# Python worker
cd worker && uv venv --python 3.12 .venv
uv pip install --python .venv -e ".[dev]"
PYTHONPATH=src .venv/bin/pytest        # unit tests (no GPU)
```

## Tech stack

- **OCR:** PaddleOCR 3.7 (PP-OCRv6 + PaddleOCR-VL) · PaddlePaddle 3.3 / CUDA 12.6
- **Frontend:** React 19 + TypeScript on Vite+ (Rolldown / Oxlint / Oxfmt / Vitest)
- **API/MCP:** Hono on Node · `@modelcontextprotocol/sdk` · SQLite (`node:sqlite`)
- **Worker:** Python 3.12 · PyMuPDF · python-docx / docxcompose · pandoc
- **Container:** NVIDIA CUDA 12.6 + cuDNN, single service via supervisord

## Security

No authentication by default. Set `API_KEY` and/or place the service behind a reverse proxy
with access control before exposing it to a network.

## License

MIT
