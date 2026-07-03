#!/bin/sh
set -e

export PIP_BREAK_SYSTEM_PACKAGES=1
mkdir -p /app/data/uploads /app/data/outputs /root/.paddlex /root/.cache

# --- GPU detection ---
GPU_OK=0
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
  echo "GPU: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits | head -n1) MiB"
  GPU_OK=1
else
  echo "WARNING: no usable GPU. Set PDF_OCR_DEVICE=cpu to run on CPU (much slower)."
fi

# --- Optional HPI deps (ultra-infer) — one-time, needs GPU, off by default ---
# The default engine is ONNX Runtime (PDF_OCR_ENGINE=onnxruntime), which does NOT need
# HPI. HPI only helps the 'paddle' engine on older models (e.g. PP-OCRv5_server); enable
# it with PDF_OCR_ENABLE_HPI=1. The worker falls back to plain inference if it's absent.
HPI_MARKER=/root/.paddlex/hpi_installed
if [ "$GPU_OK" = "1" ] && [ "${PDF_OCR_ENABLE_HPI:-0}" = "1" ] && [ ! -f "$HPI_MARKER" ]; then
  echo "Installing High-Performance Inference deps (one-time)..."
  if /opt/venv/bin/paddleocr install_hpi_deps gpu >/dev/null 2>&1; then
    touch "$HPI_MARKER"
    echo "HPI installed."
  else
    echo "HPI install failed; continuing without it."
  fi
fi

# --- Fail closed on missing auth ---
# The container is internet-exposed (cloudflared tunnel), so refuse to start an
# unauthenticated server unless the operator explicitly opts out for local-only use.
if [ -z "${API_KEY:-}" ] && [ "${PDF_OCR_ALLOW_NO_AUTH:-0}" != "1" ]; then
  echo "FATAL: API_KEY is not set. The container serves an internet-exposed API and refuses" >&2
  echo "       to start unauthenticated. Set API_KEY, or set PDF_OCR_ALLOW_NO_AUTH=1 to" >&2
  echo "       explicitly allow an open server (local/trusted networks only)." >&2
  exit 1
fi

# Model weights (~2 GB) download on first job and are cached in the /root/.paddlex volume.
exec supervisord -c /app/supervisord.conf
