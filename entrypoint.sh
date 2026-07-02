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

# --- Optional HPI acceleration (ONNX Runtime / OpenVINO) — one-time, needs GPU ---
# The worker falls back to plain inference automatically if this is absent.
HPI_MARKER=/root/.paddlex/hpi_installed
if [ "$GPU_OK" = "1" ] && [ "${PDF_OCR_ENABLE_HPI:-1}" = "1" ] && [ ! -f "$HPI_MARKER" ]; then
  echo "Installing High-Performance Inference deps (one-time)..."
  if /opt/venv/bin/paddleocr install_hpi_deps gpu >/dev/null 2>&1; then
    touch "$HPI_MARKER"
    echo "HPI installed."
  else
    echo "HPI install failed; continuing without it."
  fi
fi

# Model weights (~2 GB) download on first job and are cached in the /root/.paddlex volume.
exec supervisord -c /app/supervisord.conf
