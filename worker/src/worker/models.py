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
    """PP-OCRv6 (default) for searchable-PDF OCR. HPI (ONNX Runtime/OpenVINO) on GPU."""
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
    if on_gpu and config.ENABLE_HPI:
        kwargs["enable_hpi"] = True
    log.info("building PaddleOCR (%s, hpi=%s)", config.OCR_VERSION, kwargs.get("enable_hpi", False))
    try:
        return PaddleOCR(**kwargs)
    except Exception as e:
        # HPI needs the optional ultra-infer package; fall back to plain inference.
        if kwargs.pop("enable_hpi", None):
            log.warning("HPI unavailable (%s); rebuilding OCR without it", e)
            return PaddleOCR(**kwargs)
        raise


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
