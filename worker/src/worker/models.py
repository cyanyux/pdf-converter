"""Model construction, warmup, and GPU telemetry for a single model family.

Each model child loads exactly one family (PP-OCR or VL). The supervisor tears
the child down to switch families, so the OS reclaims VRAM (no reliance on
paddle's empty_cache). VL input resolution is capped by the caller at render
time, since the pipeline exposes no max_pixels knob.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any

import numpy as np
import paddle

from . import config

log = logging.getLogger("worker.models")


def _norm_device() -> str:
    """Normalized device string (mirrors paddle's own case-insensitive parsing)."""
    return config.DEVICE.strip().lower()


def _is_cpu(dev: str) -> bool:
    """True for 'cpu' / 'cpu:N' — matches how DEVICE selects the CPU path."""
    return dev == "cpu" or dev.startswith("cpu")


def _gpu_target(dev: str) -> str:
    """Concrete GPU device string for a normalized, non-CPU DEVICE."""
    return "gpu:0" if dev in ("", "auto", "gpu", "cuda") else dev


def set_device() -> bool:
    """Select the runtime device; returns True if on GPU."""
    dev = _norm_device()
    if _is_cpu(dev) or not paddle.is_compiled_with_cuda():  # type: ignore[attr-defined]
        paddle.set_device("cpu")  # type: ignore[attr-defined]
        return False
    paddle.set_device(_gpu_target(dev))  # type: ignore[attr-defined]
    return True


def gpu_info(active_model: str | None) -> dict[str, Any]:
    """Whole-GPU VRAM via nvidia-smi — reflects the child's real usage, not the
    supervisor's (empty) CUDA context."""
    info: dict[str, Any] = {"device": config.DEVICE, "active_model": active_model}
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            name, total, used = (p.strip() for p in out.stdout.strip().splitlines()[0].split(","))
            info["name"] = name
            info["vram_total_mb"] = int(total)
            info["vram_used_mb"] = int(used)
    except Exception:
        pass
    return info


def build_ocr() -> Any:
    """PP-OCRv6 (default) for searchable-PDF OCR.

    GPU engine selection (config.OCR_ENGINE):
      - 'onnxruntime': paddle2onnx + ONNX Runtime. ~1.14x faster than Paddle on PP-OCRv6
        (measured on an RTX 3060), with identical recognized text. Falls back to Paddle
        if onnxruntime-gpu / paddle2onnx are unavailable.
      - 'paddle': native Paddle Inference; honours config.ENABLE_HPI (HPI auto-selects a
        backend, but resolves to Paddle for PP-OCRv6 — it only helps older models such as
        PP-OCRv5_server).
    """
    from paddleocr import PaddleOCR

    dev = _norm_device()
    on_gpu = not _is_cpu(dev) and paddle.is_compiled_with_cuda()  # type: ignore[attr-defined]
    device = "cpu" if _is_cpu(dev) else _gpu_target(dev)
    kwargs: dict[str, Any] = {
        "ocr_version": config.OCR_VERSION,
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
        "text_recognition_batch_size": config.TEXT_REC_BATCH_SIZE,
        "text_det_limit_side_len": 960,
        "text_det_thresh": 0.25,
        "text_det_box_thresh": 0.5,
        "device": device,
    }

    # Preferred: direct ONNX Runtime engine (the only backend that speeds up PP-OCRv6).
    if on_gpu and config.OCR_ENGINE == "onnxruntime":
        try:
            log.info("building PaddleOCR (%s, engine=onnxruntime)", config.OCR_VERSION)
            return PaddleOCR(engine="onnxruntime", **kwargs)
        except Exception as e:
            # Needs onnxruntime-gpu + paddle2onnx; fall through to Paddle if missing.
            log.warning("ONNX Runtime engine unavailable (%s); falling back to Paddle", e)

    # Optional HPI on the Paddle path (helps PP-OCRv5, not v6).
    if on_gpu and config.ENABLE_HPI:
        try:
            log.info("building PaddleOCR (%s, hpi=True)", config.OCR_VERSION)
            return PaddleOCR(enable_hpi=True, **kwargs)
        except Exception as e:
            log.warning("HPI unavailable (%s); rebuilding OCR without it", e)

    log.info("building PaddleOCR (%s, engine=paddle)", config.OCR_VERSION)
    return PaddleOCR(**kwargs)


def build_vl() -> Any:
    """PaddleOCR-VL v1.6 for document parsing (markdown / word)."""
    from paddleocr import PaddleOCRVL

    # Normalize DEVICE exactly as set_device()/build_ocr() do, so VL and PP-OCR resolve the
    # same device string for every accepted PDF_OCR_DEVICE spelling ('gpu', 'cuda', 'auto',
    # '', ' gpu:0 '); passing raw config.DEVICE would desync VL from the PP-OCR path.
    dev = _norm_device()
    kwargs: dict[str, Any] = {
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "device": "cpu" if _is_cpu(dev) else _gpu_target(dev),
    }
    if config.VL_REC_BACKEND:
        kwargs["vl_rec_backend"] = config.VL_REC_BACKEND
    log.info("building PaddleOCR-VL")
    return PaddleOCRVL(**kwargs)


def warmup_ocr(ocr: Any) -> None:
    try:
        blank = np.full((320, 320, 3), 255, dtype=np.uint8)
        ocr.predict(blank)
        log.info("OCR warmup done")
    except Exception as e:
        log.warning("OCR warmup skipped: %s", e)
