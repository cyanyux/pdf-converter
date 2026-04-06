#!/bin/sh
set -e

# Allow pip to install system-wide packages (required for Ubuntu 24.04)
export PIP_BREAK_SYSTEM_PACKAGES=1
export DISABLE_MODEL_SOURCE_CHECK=True
export PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True

# Detect whether the container can actually initialize the GPU runtime.
GPU_READY=0
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    GPU_READY=1
    echo "NVIDIA runtime check passed"
else
    echo "NVIDIA runtime check failed; GPU-dependent startup may fail until the runtime is fixed"
fi

# Install HPI plugin on first run (requires working GPU access)
HPI_MARKER="/root/.paddlex/hpi_installed"
if [ "$GPU_READY" = "1" ] && [ ! -f "$HPI_MARKER" ]; then
    echo "Installing High-Performance Inference plugin..."
    if paddlex --install hpi-gpu --no_deps -y; then
        mkdir -p /root/.paddlex
        touch "$HPI_MARKER"
        echo "HPI installation completed successfully"
    else
        echo "HPI installation failed, continuing without HPI"
    fi
fi

# Start the application with gunicorn (single worker for GPU)
exec gunicorn --bind 0.0.0.0:5000 --workers 1 --threads 4 --timeout 600 app:app
