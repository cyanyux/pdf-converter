# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PDF-OCR is a GPU-accelerated Flask web service that converts scanned/image-based PDFs into searchable PDFs, Markdown, or Word documents using PaddleOCR and PaddleOCR-VL models. The UI is in Traditional Chinese (繁體中文).

## Build and Run Commands

```bash
# Build and start the service
docker compose up -d --build

# View logs
docker compose logs -f pdf-ocr

# Rebuild after code changes
docker compose up -d --build

# Stop the service
docker compose down
```

The service runs on port 5000 and requires an NVIDIA GPU with CUDA support.

Prerequisite: the external Docker network `app-net` must exist (`docker network create app-net`).

## Architecture

### Core Components

- **app.py**: Main Flask application with all endpoints and processing logic
  - Two separate model pipelines: OCR (for searchable PDFs) and VL (for Markdown/Word)
  - Separate processing locks (`ocr_processing_lock`, `vl_processing_lock`) allow OCR and VL jobs to run in parallel
  - Lazy model loading with 30-minute idle timeout to release VRAM
  - Background job processing with SSE progress updates

- **s2t_dict.py**: Simplified-to-Traditional Chinese character mapping dictionary (~3810 one-to-one mappings)

- **entrypoint.sh**: Docker entrypoint that installs HPI (High-Performance Inference) plugin on first run, then starts gunicorn with 1 worker / 4 threads / 600s timeout

- **static/index.html**: Single-page frontend application (Traditional Chinese UI)

### Concurrency Model

Single gunicorn worker is required due to GPU memory constraints—models cannot be shared across processes. The two processing locks allow one OCR job and one VL job to run simultaneously but prevent multiple jobs of the same type from competing for GPU resources.

### Processing Pipelines

1. **Searchable PDF (OCR)**: Uses PP-OCRv5 server models
   - Renders PDF pages to images (200 DPI, adaptive reduction for large pages)
   - Batch OCR processing (4 pages at a time, rec_batch_num=16)
   - Adds invisible text layer to original PDF
   - Uses HPI (ONNX Runtime GPU) acceleration when available

2. **Markdown/Word (VL)**: Uses PaddleOCR-VL-1.5
   - Vision-language model for document understanding
   - Handles tables, images, and document structure
   - Post-processes VL output: merges cross-page tables, relevels titles, converts simplified→traditional Chinese
   - Word conversion: markdown → pandoc → DOCX with style post-processing (border fixes, color cleanup)

3. **Dual Export** (`/api/export`): Runs VL pipeline once, produces both Markdown and Word output from shared results

### API Endpoints

- `POST /api/ocr` - Start searchable PDF job
- `POST /api/markdown` - Start Markdown conversion job
- `POST /api/word` - Start Word conversion job
- `POST /api/export` - Start dual Markdown + Word export (single VL pass)
- `GET /api/job/<job_id>` - Get job status
- `GET /api/progress/<task_id>` - SSE endpoint for real-time progress
- `POST /api/cancel/<job_id>` - Cancel a running job
- `GET /api/download/<filename>` - Download processed PDF
- `GET /api/download/markdown/<folder_name>` - Download Markdown as zip
- `GET /api/download/word/<folder_name>` - Download Word document
- `GET /api/view/<filename>` - View PDF in browser
- `GET /api/view/markdown/<folder_name>` - View markdown in browser
- `DELETE /api/delete/<file_id>` - Delete exported file/folder
- `GET /api/health` - Health check
- `GET /api/csrf-token` - Get CSRF token

Rate limits: 1000 req/min default; 2000 req/min on `GET /api/job/<job_id>`; 5 req/min on `/api/ocr`, `/api/markdown`, `/api/word`, `/api/export`.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PDF_OCR_MAX_UPLOAD_MB` | 500 | Max upload size in MB |
| `PDF_OCR_CLEANUP_INTERVAL` | 3600 | Seconds between cleanup runs |
| `PDF_OCR_MAX_FILE_AGE` | 3600 | Seconds before output files are deleted |
| `PDF_OCR_MODEL_IDLE_TIMEOUT` | 1800 | Seconds before idle models are unloaded from VRAM |
| `PDF_OCR_JOB_MAX_AGE` | 7200 | Seconds before job metadata is purged |
| `SECRET_KEY` | random | Flask secret key (auto-generated if unset) |

### Key Technical Details

- All state (jobs, progress, cancel flags) is in-memory with threading locks—no database
- `fix_ocr_text()` converts commonly misrecognized simplified Chinese characters to traditional using `s2t_dict.py`
- `validate_pdf_file()` checks PDF magic bytes (`%PDF-`); `validate_path()` prevents path traversal
- Output directory: `/tmp/pdf_ocr_output` (Docker volume); uploads: `/tmp/pdf_ocr_uploads` (temporary)
- Background cleanup thread removes old files and releases idle models

### Conventions

- UI text and user-facing error messages are in Traditional Chinese (繁體中文)
- Log messages use English with category prefixes: `OCR:`, `VL:`, `Cleanup:`, `Word:`, `Memory:`
- CSRF protection is enabled; frontend fetches tokens from `/api/csrf-token`

### Dependencies

- PaddlePaddle GPU 3.2.1+ (installed from paddlepaddle.org.cn, CUDA 12.6)
- PaddleOCR 3.4.0+ with doc-parser (VL model support)
- PyMuPDF (fitz) for PDF manipulation
- pandoc for Markdown-to-Word conversion
- python-docx for Word style post-processing
- Flask-WTF (CSRF), Flask-Limiter (rate limiting)
