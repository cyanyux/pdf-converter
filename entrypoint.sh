#!/bin/sh
set -e

export PIP_BREAK_SYSTEM_PACKAGES=1
mkdir -p /app/data/uploads /app/data/outputs /root/.paddlex /root/.cache

# --- Legacy env-var warning ---
# The PDF_OCR_* variables were renamed to PDF_CONVERTER_* (same suffixes, no
# fallback). Warn if any legacy var is still set so a stale deployment config is
# noticed — but never fail; the app just ignores them.
LEGACY=$(env | sed -n 's/^\(PDF_OCR_[A-Z0-9_]*\)=.*/\1/p' | sort)
if [ -n "$LEGACY" ]; then
  echo "WARNING: legacy PDF_OCR_* env vars are set and ignored (renamed to PDF_CONVERTER_*):" >&2
  for v in $LEGACY; do
    echo "  - $v -> $(echo "$v" | sed 's/^PDF_OCR_/PDF_CONVERTER_/')" >&2
  done
fi

# --- GPU detection ---
GPU_OK=0
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
  echo "GPU: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits | head -n1) MiB"
  GPU_OK=1
else
  echo "WARNING: no usable GPU. Set PDF_CONVERTER_DEVICE=cpu to run on CPU (much slower)."
fi

# --- Optional HPI deps (ultra-infer) — one-time, needs GPU, off by default ---
# The default engine is ONNX Runtime (PDF_CONVERTER_ENGINE=onnxruntime), which does NOT need
# HPI. HPI only helps the 'paddle' engine on older models (e.g. PP-OCRv5_server); enable
# it with PDF_CONVERTER_ENABLE_HPI=1. The worker falls back to plain inference if it's absent.
HPI_MARKER=/root/.paddlex/hpi_installed
if [ "$GPU_OK" = "1" ] && [ "${PDF_CONVERTER_ENABLE_HPI:-0}" = "1" ] && [ ! -f "$HPI_MARKER" ]; then
  echo "Installing High-Performance Inference deps (one-time)..."
  if /opt/venv/bin/paddleocr install_hpi_deps gpu >/dev/null 2>&1; then
    touch "$HPI_MARKER"
    echo "HPI installed."
  else
    echo "HPI install failed; continuing without it."
  fi
fi

# --- Auth notice ---
# API_KEY is optional: the intended deployments are local-only or behind an
# access-controlled tunnel/proxy (e.g. Cloudflare Zero Trust), which handles auth
# upstream. The server logs its own warning too (warnIfInsecureBind).
if [ -z "${API_KEY:-}" ]; then
  echo "NOTE: API_KEY not set — API is unauthenticated. Fine behind Zero Trust / on a" >&2
  echo "      trusted network; set API_KEY to require a key on the REST API + MCP." >&2
fi

# Model weights (~2 GB) download on first job and are cached in the /root/.paddlex volume.
exec supervisord -c /app/supervisord.conf
