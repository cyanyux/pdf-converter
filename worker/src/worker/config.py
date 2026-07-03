"""Worker configuration from environment (mirrors the server's config surface)."""

from __future__ import annotations

import os

# Force EAGER CUDA module loading before paddle initializes CUDA. The CUDA 12 default
# (CUDA_MODULE_LOADING=LAZY) loads each GPU kernel's module on its first launch via
# cuLibraryGetModule; in the PaddleOCR-VL pipeline the VLM runs in a background worker
# thread, and that first lazy load deadlocks the thread at 0% GPU on some driver builds
# (reproduced on 595.71.05) — a document parse then hangs forever with no recovery. EAGER
# front-loads all kernel modules at CUDA-context init on the main thread, before the
# pipeline spawns its worker thread, which avoids the deadlock (dense page: hang -> ~8s).
# Set here because `config` is imported before `worker.models` (which imports paddle);
# setdefault keeps any explicit override the operator sets.
os.environ.setdefault("CUDA_MODULE_LOADING", "EAGER")

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
# Hard cap on VL generated tokens per region (matches the pipeline's own default). Bounds
# worst-case decode so a degenerate/repeating page can't run to the 8192-token ceiling.
VL_MAX_NEW_TOKENS = _int("PDF_OCR_VL_MAX_NEW_TOKENS", 4096)
# Optional VL recognition backend override (passed to PaddleOCRVL as vl_rec_backend);
# None leaves the pipeline's default. Set to force a specific inference backend.
VL_REC_BACKEND = os.environ.get("PDF_OCR_VL_REC_BACKEND") or None

# DOCX backend: 'native' (res.save_to_word + docxcompose) or 'pandoc' (fallback).
DOCX_BACKEND = os.environ.get("PDF_OCR_DOCX_BACKEND", "native")
PANDOC_TIMEOUT = _int("PDF_OCR_PANDOC_TIMEOUT", 120)

POLL_S = _int("PDF_OCR_POLL_MS", 200) / 1000
STALE_S = _int("PDF_OCR_STALE_S", 120)
MAX_ATTEMPTS = _int("PDF_OCR_MAX_ATTEMPTS", 3)
JOB_MAX_AGE_S = _int("PDF_OCR_JOB_MAX_AGE", 7200)
GC_INTERVAL_S = _int("PDF_OCR_GC_INTERVAL", 600)

# Supervisor watchdog: a child that stops advancing a job's heartbeat (set_progress bumps
# jobs.heartbeat_at per page) for this long is presumed wedged and gets killed + requeued,
# so one stuck page can never freeze the whole worker. Generous enough to tolerate a slow
# dense page; a genuine hang is unbounded, so it always trips.
JOB_IDLE_TIMEOUT_S = _int("PDF_OCR_JOB_IDLE_TIMEOUT", 300)
# Post-recognition phases (VL table-merge/restructure, native docx save, pandoc) do opaque
# CPU work with no per-page heartbeat, so the strict timeout above would false-kill a slow-
# but-alive save. Once a job reports a save-phase progress status the watchdog uses this
# looser bound instead. The CUDA-hang risk is confined to recognition, which stays strict.
SAVE_IDLE_TIMEOUT_S = _int("PDF_OCR_SAVE_IDLE_TIMEOUT", 1800)
# How often the supervisor wakes while a child is busy — to refresh its heartbeat, run
# GC/reap, and evaluate the watchdog — instead of blocking indefinitely on child output.
WATCHDOG_TICK_S = _int("PDF_OCR_WATCHDOG_TICK_MS", 2000) / 1000
# First-ever run downloads ~2 GB of weights; give model construction a generous ceiling
# so a genuinely slow download isn't mistaken for a hung child.
MODEL_LOAD_TIMEOUT_S = _int("PDF_OCR_MODEL_LOAD_TIMEOUT", 900)
# Periodically recover jobs an earlier crashed worker left in 'processing' (belt over the
# once-at-startup reap); never touches the job the live child is actively working.
REAP_INTERVAL_S = _int("PDF_OCR_REAP_INTERVAL", 60)
# Grace after a cancel request for the child to self-cancel between pages before the
# supervisor kills it (mid-page generation is otherwise uninterruptible).
CANCEL_GRACE_S = _int("PDF_OCR_CANCEL_GRACE_S", 15)
