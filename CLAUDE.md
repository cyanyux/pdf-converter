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

## Architecture

### Core Components

- **app.py**: Main Flask application with all endpoints and processing logic
  - Two separate model pipelines: OCR (for searchable PDFs) and VL (for Markdown/Word)
  - Separate processing locks (`ocr_processing_lock`, `vl_processing_lock`) allow OCR and VL jobs to run in parallel
  - Lazy model loading with 30-minute idle timeout to release VRAM
  - Background job processing with SSE progress updates

- **s2t_dict.py**: Simplified-to-Traditional Chinese character mapping dictionary (one-to-one mappings only)

- **entrypoint.sh**: Docker entrypoint that installs HPI (High-Performance Inference) plugin on first run

### Processing Pipelines

1. **Searchable PDF (OCR)**: Uses PP-OCRv5 server models
   - Renders PDF pages to images
   - Batch OCR processing (4 pages at a time)
   - Adds invisible text layer to original PDF

2. **Markdown/Word (VL)**: Uses PaddleOCR-VL-1.5
   - Vision-language model for document understanding
   - Handles tables, images, and document structure
   - Word conversion uses pandoc with post-processing for styles

### API Endpoints

- `POST /api/ocr` - Start searchable PDF job
- `POST /api/markdown` - Start Markdown conversion job
- `POST /api/word` - Start Word conversion job
- `GET /api/job/<job_id>` - Get job status
- `GET /api/progress/<task_id>` - SSE endpoint for real-time progress
- `POST /api/cancel/<job_id>` - Cancel a running job
- `GET /api/download/<filename>` - Download processed PDF
- `GET /api/download/markdown/<folder_name>` - Download Markdown as zip
- `GET /api/download/word/<folder_name>` - Download Word document
- `DELETE /api/delete/<file_id>` - Delete exported file/folder

### Key Technical Details

- Max upload size: 500MB
- Output files auto-cleanup: 1 hour
- Model idle timeout: 30 minutes (releases VRAM)
- OCR batch size: 4 pages
- Default DPI: 200 (adaptive reduction for large pages)
- Uses `fix_ocr_text()` to convert commonly misrecognized simplified Chinese characters to traditional

### Dependencies

- PaddlePaddle GPU 3.2.1+ (installed from paddlepaddle.org.cn)
- PaddleOCR with doc-parser (VL model support)
- PyMuPDF (fitz) for PDF manipulation
- pandoc for Markdown-to-Word conversion
- python-docx for Word style post-processing
