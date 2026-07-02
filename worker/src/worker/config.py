"""Worker configuration from environment (mirrors the server's config surface)."""

from __future__ import annotations

import os
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    v = os.environ.get(name)
    try:
        return int(v) if v is not None else default
    except ValueError:
        return default


ROOT = Path(os.environ.get("PDF_OCR_ROOT", ".")).resolve()
DB_PATH = Path(os.environ.get("PDF_OCR_DB", str(ROOT / "data/pdf-ocr.db")))
SCHEMA_PATH = Path(os.environ.get("PDF_OCR_SCHEMA", str(ROOT / "db/schema.sql")))
UPLOADS_DIR = Path(os.environ.get("PDF_OCR_UPLOADS", str(ROOT / "data/uploads")))
OUTPUTS_DIR = Path(os.environ.get("PDF_OCR_OUTPUTS", str(ROOT / "data/outputs")))

# 'cpu' | 'gpu' | 'gpu:0'
DEVICE = os.environ.get("PDF_OCR_DEVICE", "gpu:0")
ENABLE_HPI = _bool("PDF_OCR_ENABLE_HPI", True)
# Searchable-PDF OCR engine: PP-OCRv6 is the 3.7 default; pin PP-OCRv5 for continuity.
OCR_VERSION = os.environ.get("PDF_OCR_OCR_VERSION", "PP-OCRv6")
OCR_BATCH_SIZE = _int("PDF_OCR_BATCH", 4)
TEXT_REC_BATCH_SIZE = _int("PDF_OCR_REC_BATCH", 16)
CPU_THREADS = _int("PDF_OCR_CPU_THREADS", 8)
SEARCHABLE_DPI = _int("PDF_OCR_DPI", 200)

# Cap VL input resolution to avoid native dynamic-graph VRAM spikes on dense pages.
VL_RENDER_ZOOM = float(os.environ.get("PDF_OCR_VL_ZOOM", "1.5"))
VL_MAX_PIXELS = _int("PDF_OCR_VL_MAX_PIXELS", 2200 * 2200)

# DOCX backend: 'native' (res.save_to_word + docxcompose) or 'pandoc' (fallback).
DOCX_BACKEND = os.environ.get("PDF_OCR_DOCX_BACKEND", "native")
PANDOC_TIMEOUT = _int("PDF_OCR_PANDOC_TIMEOUT", 120)

POLL_S = _int("PDF_OCR_POLL_MS", 200) / 1000
STALE_S = _int("PDF_OCR_STALE_S", 120)
MAX_ATTEMPTS = _int("PDF_OCR_MAX_ATTEMPTS", 3)
JOB_MAX_AGE_S = _int("PDF_OCR_JOB_MAX_AGE", 7200)
GC_INTERVAL_S = _int("PDF_OCR_GC_INTERVAL", 600)
