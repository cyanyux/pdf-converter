# PDF OCR

Self-hosted, GPU-accelerated OCR web app that converts scanned or image-based PDFs into searchable, editable documents.

Upload a PDF and get back:

- **Searchable PDF** — original layout preserved with an invisible, selectable text layer
- **Markdown** — structured plain text with tables and images extracted
- **Word (.docx)** — editable document ready for further editing

Built with [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) and PaddleOCR-VL for high-accuracy Chinese and multilingual document recognition. Runs entirely on your own hardware — no data leaves your network.

## Features

- **GPU-accelerated** — CUDA-powered OCR and vision-language model inference via PaddlePaddle
- **Multiple output formats** — searchable PDF, Markdown (with images), and Word from a single upload
- **Batch processing** — upload multiple PDFs at once; jobs run in the background
- **Multilingual UI** — Traditional Chinese (繁體中文), Simplified Chinese (简体中文), and English
- **Smart Chinese text handling** — automatic Simplified-to-Traditional conversion for Traditional Chinese users; Simplified Chinese users get native output
- **Real-time progress** — live progress bar and status updates per page
- **Self-hosted & private** — everything runs locally in Docker; your documents never leave your server
- **Single-container deployment** — one `docker compose up` and you're running

## Quick Start

**Prerequisites:** NVIDIA GPU, Docker, [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)

```bash
git clone https://github.com/cyanyux/pdf-ocr.git
cd pdf-ocr
docker compose up -d --build
```

Open **http://localhost:5000** in your browser and start uploading PDFs.

> First launch takes a few minutes to download model weights (~2 GB). Subsequent starts are instant.

## Configuration

All settings are optional environment variables (set in `docker-compose.yml`):

| Variable | Default | Description |
|---|---|---|
| `PDF_OCR_DEVICE` | `auto` | Force `cpu` to disable GPU |
| `PDF_OCR_CUDA_VISIBLE_DEVICES` | — | Select specific GPU (e.g. `0`) |
| `PDF_OCR_MAX_UPLOAD_MB` | `500` | Max upload size per file (MB) |
| `PDF_OCR_MODEL_IDLE_TIMEOUT` | `1800` | Seconds before unloading idle models from VRAM |
| `PDF_OCR_CLEANUP_INTERVAL` | `3600` | Output file cleanup interval (seconds) |
| `PDF_OCR_MAX_FILE_AGE` | `3600` | Delete output files older than this (seconds) |
| `PDF_OCR_DISABLE_HPI` | `false` | Disable HPI/ONNX GPU acceleration |
| `SECRET_KEY` | auto-generated | Flask secret key for CSRF |

## API

All endpoints accept multipart file uploads and return JSON with job IDs for async polling.

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/ocr` | Convert PDF to searchable PDF |
| `POST` | `/api/markdown` | Convert PDF to Markdown (zip with images) |
| `POST` | `/api/word` | Convert PDF to Word (.docx) |
| `POST` | `/api/export` | Markdown + Word from single VL pass |
| `GET` | `/api/job/<id>` | Poll job status and progress |
| `POST` | `/api/cancel/<id>` | Cancel a running job |
| `GET` | `/api/download/...` | Download completed output |
| `GET` | `/api/health` | Health check (device, GPU status) |

## Tech Stack

- **OCR Engine:** [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) 3.4 + PaddleOCR-VL-1.5
- **GPU Framework:** [PaddlePaddle](https://github.com/PaddlePaddle/Paddle) 3.2 with CUDA 12.6
- **Backend:** Python / Flask / Gunicorn
- **Frontend:** Vanilla JS single-page app
- **Container:** NVIDIA CUDA 12.6.3 + cuDNN on Ubuntu 24.04
- **Document conversion:** Pandoc, PyMuPDF, python-docx

## Troubleshooting

**Check service health:**
```bash
curl http://localhost:5000/api/health
```

**Verify GPU access inside container:**
```bash
docker exec pdf-ocr nvidia-smi
```

**GPU not detected?**
1. Ensure [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) is installed
2. Restart Docker after driver updates: `sudo systemctl restart docker`
3. Rebuild: `docker compose down && docker compose up -d --build`

## Security

This service has **no built-in authentication**. Do not expose it directly to the internet. Use a reverse proxy (Nginx, Caddy, Traefik) with access control for production deployments.

## License

MIT
