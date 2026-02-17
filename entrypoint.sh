#!/bin/sh
set -e

# Allow pip to install system-wide packages (required for Ubuntu 24.04)
export PIP_BREAK_SYSTEM_PACKAGES=1

# Install HPI plugin on first run (requires GPU access)
HPI_MARKER="/root/.paddlex/hpi_installed"
if [ ! -f "$HPI_MARKER" ]; then
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
