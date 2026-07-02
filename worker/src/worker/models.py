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


def set_device() -> bool:
    """Select the runtime device; returns True if on GPU."""
    dev = config.DEVICE.strip().lower()
    if dev == "cpu" or not paddle.is_compiled_with_cuda():
        paddle.set_device("cpu")
        return False
    target = "gpu:0" if dev in ("", "auto", "gpu", "cuda") else dev
    paddle.set_device(target)
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

    on_gpu = config.DEVICE != "cpu" and paddle.is_compiled_with_cuda()
    kwargs: dict[str, Any] = {
        "ocr_version": config.OCR_VERSION,
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
        "text_recognition_batch_size": config.TEXT_REC_BATCH_SIZE,
        "text_det_limit_side_len": 960,
        "text_det_thresh": 0.25,
        "text_det_box_thresh": 0.5,
        "device": config.DEVICE,
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

    kwargs: dict[str, Any] = {
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "device": config.DEVICE,
    }
    backend = config.__dict__.get("VL_REC_BACKEND")
    if backend:
        kwargs["vl_rec_backend"] = backend
    log.info("building PaddleOCR-VL")
    return PaddleOCRVL(**kwargs)


def warmup_ocr(ocr: Any) -> None:
    try:
        blank = np.full((320, 320, 3), 255, dtype=np.uint8)
        ocr.predict(blank)
        log.info("OCR warmup done")
    except Exception as e:
        log.warning("OCR warmup skipped: %s", e)
