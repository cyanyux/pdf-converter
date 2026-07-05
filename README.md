# PDF Converter

Self-hosted PDF conversion web app: turn any PDF into a **searchable PDF**, **Markdown**,
or **Word (.docx)** — with a clean web UI and a first-class API/MCP surface for AI agents.

Every document is probed and routed to the engine that preserves the most fidelity:
born-digital PDFs have their text copied losslessly from the embedded text layer
(**Docling**, CPU); scanned documents go through GPU OCR with strong
Traditional/Simplified Chinese accuracy (**PaddleOCR 3.7**: PP-OCRv6 + PaddleOCR-VL).
Everything runs on your own hardware — no data leaves your network.

## Outputs and routing

| Output | For | Engine |
|---|---|---|
| Searchable PDF | humans (read, search, cite) | PP-OCRv6 invisible text layer (GPU). Already-searchable input is detected and returned as-is — no pointless re-OCR |
| Word (.docx) | humans (edit) | PaddleOCR-VL (GPU) — real merged table cells, Heading 1–6 outline |
| Markdown (+images) | AI agents | born-digital → **Docling** (CPU, character-exact from the text layer); scanned → **PaddleOCR-VL** (GPU) |

The router probes each PDF's pages (embedded text vs raster) per job; the chosen
`engine` is reported in the job result and shown in the UI.

## Features

- **Fidelity-first routing** — OCR is never applied to text that already exists losslessly.
- **GPU-accelerated where it counts** — a dedicated worker owns the GPU and swaps models
  by tearing children down so VRAM is reclaimed cleanly (fits a 12 GB card); Docling jobs
  run CPU-only and never contend for VRAM.
- **Agent-friendly** — documented REST API (`/openapi.json`, Swagger UI at `/docs`) and an
  **MCP server** so tools like Claude can submit and fetch conversions directly.
- **Reliable by design** — durable SQLite job queue (jobs survive restarts), crash-recovery
  reaper, hang watchdog, per-page cancellation, streamed uploads, retention GC.
- **Multilingual UI** — Traditional Chinese, Simplified Chinese, English; automatic
  Simplified→Traditional correction for zh-TW users on OCR output (never applied to
  text-layer extractions, which are already ground truth).
- **Single-container deploy** — one `docker compose up`.

## Quick start (Docker)

**Prerequisites:** NVIDIA GPU + driver, Docker, and the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).

```bash
git clone https://github.com/cyanyux/pdf-converter.git
cd pdf-converter
docker network create app-net       # one-time; shared external network (reverse proxy / tunnel attaches here)
docker compose up -d --build
```

Open **http://localhost:5000** and start uploading PDFs.

> First conversion downloads OCR model weights (~2 GB); they're cached in a volume, so
> subsequent runs are fast. To run without a GPU set `PDF_CONVERTER_DEVICE: cpu`
> (much slower for scanned input; born-digital markdown is CPU anyway).

**Migrating from the old `pdf-ocr` name:** volumes and env vars were renamed
(`pdf-ocr-*` → `pdf-converter-*`, `PDF_OCR_*` → `PDF_CONVERTER_*`, no fallbacks; the
container warns about any legacy vars it sees). To keep the model cache and job data,
copy each old volume once, e.g.
`docker run --rm -v pdf-ocr-paddlex:/from -v pdf-converter-paddlex:/to alpine cp -a /from/. /to/`
— or skip it and let the weights re-download.

## Architecture

```
Browser / AI agent ──HTTP+SSE / MCP──▶ TypeScript server (Hono, Node)
                                        · REST API + SSE + static SPA + MCP + auth
                                        │  enqueue / read
                                        ▼
                              SQLite (WAL) job queue  ◀──▶  shared files
                                        ▲  claim / write
                                        │
                              Python worker (probe → route per job)
                                · scanned → GPU child: PP-OCRv6 / PaddleOCR-VL
                                · born-digital markdown → CPU child: Docling
                                · already-searchable pdf → returned as-is
```

The whole human/agent-facing surface is **TypeScript** (managed by [Vite+](https://viteplus.dev));
Python is reduced to a headless conversion worker. The SQLite schema (`db/schema.sql`) is the
language-agnostic contract between them.

## Configuration

Environment variables, set via `.env` / `docker-compose.yml` (or the shell in dev).

| Variable | Default | Description |
|---|---|---|
| `API_KEY` | — | If set, require it (`Authorization: Bearer` / `X-API-Key`) on the API + MCP; unset = open |
| `PDF_CONVERTER_DEVICE` | `gpu:0` | `cpu` to run OCR without a GPU |
| `PDF_CONVERTER_OCR_VERSION` | `PP-OCRv6` | Searchable-PDF OCR model (`PP-OCRv5` for continuity) |
| `PDF_CONVERTER_ENGINE` | `onnxruntime` | PP-OCR GPU backend: ~1.14× faster than `paddle` on PP-OCRv6, identical output; auto-falls back to `paddle` if unavailable |
| `PDF_CONVERTER_MAX_UPLOAD_MB` | `500` | Max upload size per file |
| `PDF_CONVERTER_MAX_QUEUE` | `100` | Reject new jobs above this queue depth (429) |
| `PDF_CONVERTER_JOB_MAX_AGE` | `7200` | Retention (seconds) for outputs and job rows; raise it if downloads must outlive 2 h |
| `PDF_CONVERTER_ENABLE_HPI` | `0` | Install/use HPI on first boot for the `paddle` engine — only helps older models (e.g. PP-OCRv5); no effect on PP-OCRv6 |

## API & agents

- **OpenAPI:** `GET /openapi.json`, Swagger UI at `/docs` (both require the `API_KEY` when set).
- **MCP (stdio):** `node apps/server/dist/mcp-stdio.mjs` (env `PDF_CONVERTER_URL`, `API_KEY`).
  Tools: `submit_pdf`, `get_job`, `wait_for_job`, `get_markdown`, `download_result`, `cancel_job`.
- **REST flow:** `POST /api/v1/jobs` (multipart `files`, `modes`, `locale`) → poll
  `GET /api/v1/jobs/{id}` (or SSE `…/events`) → `GET /api/v1/download/{id}`
  (PDF/DOCX by mode; Markdown downloads as `<name>-markdown.zip`). Job results include
  the `engine` used (`docling` / `paddleocr-vl` / `pp-ocrv6` / `none`) and a
  `notice: "already_searchable"` when a pdf-mode input needed no conversion.

See [AGENTS.md](AGENTS.md) for details.

## Development

The TypeScript workspace is driven by **Vite+** (`vp`); the Python worker uses **uv**.
All three processes share `data/pdf-converter.db` and `data/` under the repo root.

```bash
# Web layer: SPA dev server on :5173 (proxies /api) + Hono API on 127.0.0.1:8000
vp install
vp dev

# Worker (Python 3.12): one-time setup, then run alongside `vp dev`
cd worker
uv venv --python 3.12 .venv
uv pip install --python .venv torch torchvision --index-url https://download.pytorch.org/whl/cpu
uv pip install --python .venv -e ".[dev]"   # worker + docling + test/lint deps (CPU torch above must come first)
# Scanned-input OCR additionally needs the GPU stack (not on PyPI — same pins as the Dockerfile):
uv pip install --python .venv --index-strategy unsafe-best-match paddlepaddle-gpu==3.3.1 \
  --index-url https://www.paddlepaddle.org.cn/packages/stable/cu126/ \
  --extra-index-url https://pypi.org/simple/
uv pip install --python .venv onnxruntime-gpu==1.23.0 paddle2onnx==2.0.2rc3   # default PP-OCR engine (falls back to paddle if absent)
cd .. && worker/.venv/bin/python -m worker.run
```

Before committing:

```bash
vp check              # format + lint + type-check (TS)
vp test               # Vitest
cd worker && .venv/bin/ruff check . && .venv/bin/mypy && PYTHONPATH=src .venv/bin/pytest
```

## Tech stack

- **Digital-PDF parsing:** Docling 2.110 (layout + TableFormer, CPU-only torch)
- **OCR:** PaddleOCR 3.7 (PP-OCRv6 + PaddleOCR-VL 1.6) · PaddlePaddle 3.3 / CUDA 12.6
- **Frontend:** React 19 + TypeScript on Vite+ (Rolldown / Oxlint / Oxfmt / Vitest)
- **API/MCP:** Hono on Node · `@modelcontextprotocol/sdk` · SQLite (`node:sqlite`)
- **Worker:** Python 3.12 · PyMuPDF · python-docx / docxcompose
- **Container:** NVIDIA CUDA 12.6 + cuDNN runtime, uv-built venv, single service via supervisord

## Security

No authentication by default — the intended deployments are local-only or behind an
access-controlled tunnel/proxy (e.g. Cloudflare Zero Trust) that handles auth upstream.
If the service is reachable without such a layer, set `API_KEY` to guard the REST API +
MCP. Note the web UI has no key-entry field: with `API_KEY` set, browser users must store
the key in localStorage under `pdfConverterApiKey` (API/MCP clients send it as a header).

## License

MIT
