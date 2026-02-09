# Repository Guidelines

## Project Structure & Module Organization

- `app.py`: Flask backend (API routes, job tracking, OCR/VL pipelines, file cleanup).
- `static/index.html`: Single-page frontend (Traditional Chinese UI) calling `/api/*`.
- `s2t_dict.py`: Simplified-to-Traditional Chinese one-to-one mapping used by `fix_ocr_text()`.
- `Dockerfile`, `docker-compose.yml`, `entrypoint.sh`: Container build/run (GPU + pandoc).
- Docs/config: `CLAUDE.md`, `.gitignore`, `.dockerignore`.

Runtime paths (inside container):
- Outputs: `/tmp/pdf_ocr_output` (mounted as `pdf-ocr-output` volume)
- Uploads: `/tmp/pdf_ocr_uploads`

## Build, Run, and Development Commands

```bash
docker network create app-net   # one-time prerequisite (compose uses an external network)
docker compose up -d --build    # build + run the service (requires NVIDIA GPU)
docker compose logs -f pdf-ocr  # follow logs
docker compose down             # stop
```

Quick sanity check (no GPU required):
```bash
python3 -m py_compile app.py
```

## Coding Style & Naming Conventions

- Python: 4-space indentation; keep changes localized (this is a single-file app today).
- Frontend: avoid inline JS handlers for dynamic content; prefer `addEventListener` and `data-*`.
- User-facing text: Traditional Chinese (繁體中文). Log messages: English with prefixes like `OCR:`, `VL:`.
- Filenames/IDs used in URLs must be safe: sanitize on the server and `encodeURIComponent()` in the UI.

## Testing Guidelines

No formal unit-test suite yet. Do a smoke test after changes:
- `GET /api/health` should return `{"status":"ok"}`
- Upload a PDF via the UI, or:
  - `POST /api/ocr` (or `/api/markdown`, `/api/word`, `/api/export`)
  - Poll `GET /api/job/<job_id>` until `done|error|cancelled`

## Commit & Pull Request Guidelines

- Commit messages in this repo are short, sentence-case summaries (e.g. `Fix XSS, cancellation races, and job polling`).
- PRs should include:
  - What changed and why (especially for OCR accuracy, performance, or security)
  - Manual test notes (endpoint(s) exercised, expected outputs)
  - Any config/env var changes (update `CLAUDE.md` if relevant)

## Security & Configuration Tips

- Don’t commit sample PDFs or secrets (`.dockerignore`/`.gitignore` already exclude common artifacts).
- Keep rate limits and polling behavior aligned (frontend polls `/api/job/<id>`; backend enforces limits).
