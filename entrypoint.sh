#!/bin/sh
set -e

# Allow pip to install system-wide packages (required for Ubuntu 24.04)
export PIP_BREAK_SYSTEM_PACKAGES=1
export DISABLE_MODEL_SOURCE_CHECK=True
export PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True
export PDF_OCR_ENTRYPOINT_STARTED_AT="$(date +%s)"

mkdir -p /root/.cache /root/.paddlex

hpi_python_package_installed() {
    pip show ultra-infer-gpu-python >/dev/null 2>&1
}

# Detect whether the container can actually initialize the GPU runtime.
GPU_READY=0
GPU_NAME=""
GPU_DRIVER_VERSION=""
if command -v nvidia-smi >/dev/null 2>&1; then
    GPU_INFO="$(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null | head -n 1 || true)"
    if [ -n "$GPU_INFO" ]; then
        GPU_NAME="$(printf '%s' "$GPU_INFO" | cut -d',' -f1 | sed 's/[[:space:]]*$//')"
        GPU_DRIVER_VERSION="$(printf '%s' "$GPU_INFO" | cut -d',' -f2- | sed 's/^[[:space:]]*//')"
        GPU_READY=1
        echo "NVIDIA runtime check passed: ${GPU_NAME}, driver ${GPU_DRIVER_VERSION}"
    else
        echo "NVIDIA runtime check failed; GPU-dependent startup may fail until the runtime is fixed"
    fi
else
    echo "NVIDIA runtime check failed; GPU-dependent startup may fail until the runtime is fixed"
fi

export PDF_OCR_GPU_READY="$GPU_READY"
export PDF_OCR_GPU_NAME="$GPU_NAME"
export PDF_OCR_GPU_DRIVER_VERSION="$GPU_DRIVER_VERSION"

# Install HPI plugin on first run (requires working GPU access)
HPI_MARKER="/root/.paddlex/hpi_installed"
export PDF_OCR_HPI_MARKER="$HPI_MARKER"
export PDF_OCR_HPI_INSTALL_STATUS="skipped_no_gpu"

if [ -f "$HPI_MARKER" ]; then
    if hpi_python_package_installed; then
        export PDF_OCR_HPI_INSTALL_STATUS="cached"
    else
        echo "HPI marker found but runtime package is missing; reinstalling HPI dependencies"
        rm -f "$HPI_MARKER"
        export PDF_OCR_HPI_INSTALL_STATUS="stale_marker_reinstalling"
    fi
fi

if [ ! -f "$HPI_MARKER" ] && [ "$GPU_READY" = "1" ]; then
    export PDF_OCR_HPI_INSTALL_STATUS="installing"
    echo "Installing High-Performance Inference plugin..."
    if paddlex --install hpi-gpu --no_deps -y; then
        touch "$HPI_MARKER"
        export PDF_OCR_HPI_INSTALL_STATUS="installed"
        echo "HPI installation completed successfully"
    else
        export PDF_OCR_HPI_INSTALL_STATUS="failed"
        echo "HPI installation failed, continuing without HPI"
    fi
elif [ "$GPU_READY" = "1" ]; then
    :
fi

# Start the application with gunicorn (single worker for GPU)
exec gunicorn \
    --bind 0.0.0.0:5000 \
    --workers 1 \
    --threads 4 \
    --timeout 600 \
    --worker-tmp-dir /dev/shm \
    app:app
