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
# PP-OCR inference engine on GPU: 'onnxruntime' (paddle2onnx + ONNX Runtime — measured
# ~1.14x faster than Paddle on PP-OCRv6, identical output) or 'paddle' (native Paddle
# Inference). build_ocr() auto-falls back to Paddle if onnxruntime-gpu/paddle2onnx are
# unavailable. TensorRT is intentionally not offered: PP-OCRv6's detection graph does not
# convert (Int32/Int64 concat), so it yields ~1.01x — no gain.
OCR_ENGINE = os.environ.get("PDF_OCR_ENGINE", "onnxruntime").strip().lower()
# HPI (High-Performance Inference) auto-selects a backend for the 'paddle' engine. Off by
# default: for PP-OCRv6 it resolves to the Paddle backend anyway (no speedup); it only
# helps older models such as PP-OCRv5_server. Needs `paddleocr install_hpi_deps gpu`.
ENABLE_HPI = _bool("PDF_OCR_ENABLE_HPI", False)
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
