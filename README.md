# PDF-OCR (GPU)

GPU-accelerated web service for converting scanned/image-based PDFs into:

- **Searchable PDF** (original layout + invisible text layer)
- **Markdown**
- **Word (.docx)**

Backend is a single-file Flask app (`app.py`) using PaddleOCR and PaddleOCR-VL; the UI is a static SPA (`static/index.html`, Traditional Chinese).

## Requirements

- NVIDIA GPU with CUDA support
- Docker + Docker Compose
- NVIDIA Container Toolkit (so containers can access the GPU)
- Internet access on first run (installs HPI + downloads model assets)

## Quick Start

```bash
docker network create app-net
docker compose up -d --build
curl http://localhost:5000/api/health
```

Open the UI at `http://localhost:5000/` and upload PDFs.

## Configuration

Environment variables (all optional):

- `PDF_OCR_MAX_UPLOAD_MB` (default `500`)
- `PDF_OCR_CLEANUP_INTERVAL` (default `3600`)
- `PDF_OCR_MAX_FILE_AGE` (default `3600`)
- `PDF_OCR_MODEL_IDLE_TIMEOUT` (default `1800`)
- `PDF_OCR_JOB_MAX_AGE` (default `7200`)
- `PDF_OCR_DEVICE` (default `auto`; use `cpu` only if you explicitly want a non-GPU override)
- `PDF_OCR_CUDA_VISIBLE_DEVICES` (optional GPU selection override)
- `PDF_OCR_DISABLE_HPI` (default `false`; set `true` to disable HPI/ONNX GPU path)
- `PDF_OCR_ALLOW_GPU_PARALLELISM` (default `false`; set `true` only if one GPU can safely run OCR and VL together)
- `SECRET_KEY` (default: generated at startup)

Outputs are written under `/tmp/pdf_ocr_output` inside the container (mounted as the `pdf-ocr-output` volume by `docker-compose.yml`).

## API Overview

- `POST /api/ocr` (PDF -> searchable PDF)
- `POST /api/markdown` (PDF -> Markdown zip)
- `POST /api/word` (PDF -> Word docx)
- `POST /api/export` (Markdown + Word from one VL pass)
- `GET /api/job/<job_id>` (poll job status/progress)
- `POST /api/cancel/<job_id>` (cancel)
- `GET /api/download/...` (download outputs)
- `GET /api/view/markdown/<folder>` (preview markdown)
- `DELETE /api/delete/<id>` (delete exports)

## Development Notes

- Recommended workflow is Docker. `entrypoint.sh` installs the HPI plugin on first run, then starts `gunicorn`.
- Local sanity check (no GPU required): `python3 -m py_compile app.py`

## Troubleshooting GPU

- Check the app health endpoint: `curl http://localhost:5000/api/health`
- Verify GPU access inside the container: `docker exec pdf-ocr nvidia-smi`
- If the host GPU works but the container does not, recreate the service after confirming the NVIDIA Container Toolkit/runtime is healthy:

```bash
docker compose down
docker compose up -d --build
```

- If you recently had an NVIDIA driver auto-update, also restart Docker after the update so new containers pick up the refreshed runtime cleanly.
- If GPU remains unavailable, fix the host/container NVIDIA runtime before restarting the service. Use `PDF_OCR_DEVICE=cpu` only if you intentionally want a non-GPU override.

## Security Notes

This service has **no authentication** and stores job state in-memory. Do not expose it directly to the public internet; put it behind a reverse proxy with access control if needed.
