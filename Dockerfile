FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04

# Prevent interactive prompts
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Asia/Taipei

# Install system dependencies
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    pandoc \
    curl \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Install PaddlePaddle GPU first (requires special index)
# PaddleOCR-VL-1.5 requires PaddlePaddle 3.2.1+
RUN pip3 install --no-cache-dir --break-system-packages \
    paddlepaddle-gpu==3.2.1 \
    -i https://www.paddlepaddle.org.cn/packages/stable/cu126/

# Copy and install other requirements
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

# Copy application code
COPY app.py /app/app.py
COPY s2t_dict.py /app/s2t_dict.py
COPY static/ /app/static/
COPY entrypoint.sh /app/entrypoint.sh
# Normalize line endings and ensure executable bit for cross-platform checkouts
RUN sed -i 's/\r$//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh

# Create output directory
RUN mkdir -p /tmp/pdf_ocr_output

# Expose port
EXPOSE 5000

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:5000/api/health || exit 1

# Use entrypoint to install HPI at runtime (requires GPU)
ENTRYPOINT ["/bin/sh", "/app/entrypoint.sh"]
