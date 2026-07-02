# syntax=docker/dockerfile:1

# ---------- Stage 1: build SPA + bundle server (Vite+) ----------
FROM node:24-bookworm-slim AS build
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN npm install -g pnpm@11.9.0
COPY pnpm-workspace.yaml package.json vite.config.ts tsconfig.json ./
COPY packages ./packages
COPY apps ./apps
RUN pnpm install
# Builds apps/spa/dist (static SPA) and apps/server/dist (self-contained bundle).
RUN pnpm run build

# ---------- Stage 2: runtime (CUDA 12.6 + Python 3.12 + Node 24) ----------
FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04 AS runtime
ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Taipei \
    DISABLE_MODEL_SOURCE_CHECK=True \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    PATH="/opt/venv/bin:${PATH}"

# System deps + Node 24 (Ubuntu 24.04 ships Python 3.12).
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip pandoc curl ca-certificates gnupg \
      libgl1 libglib2.0-0 libgomp1 supervisor \
 && curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

# PaddlePaddle GPU (cu126) in its own layer — the big 2 GB download stays cached
# across app-dependency changes.
RUN python3 -m venv /opt/venv \
 && pip install --no-cache-dir paddlepaddle-gpu==3.3.1 \
      -i https://www.paddlepaddle.org.cn/packages/stable/cu126/ \
      --extra-index-url https://pypi.org/simple/
# PaddleOCR 3.7 stack + doc2md (Office→Markdown) + doc utilities.
RUN pip install --no-cache-dir \
      "paddleocr[doc-parser,doc2md]==3.7.0" pymupdf==1.28.0 python-docx==1.2.0 \
      docxcompose beautifulsoup4==4.15.0 "numpy<2.4"
# ONNX Runtime engine for PP-OCR (paddle2onnx export + onnxruntime-gpu): ~1.14x faster than
# native Paddle on PP-OCRv6, identical output. Separate layer so the big paddle layers stay
# cached. Selected via PDF_OCR_ENGINE=onnxruntime (default); auto-falls back to Paddle.
# Pinned to the 1.23 line: it targets CUDA 12.x (matches the base image); 1.24+ links CUDA 13.
RUN pip install --no-cache-dir onnxruntime-gpu==1.23.0 paddle2onnx==2.0.2rc3

WORKDIR /app
COPY db ./db
COPY worker/src ./worker/src
COPY supervisord.conf entrypoint.sh ./
COPY --from=build /app/apps/spa/dist ./apps/spa/dist
COPY --from=build /app/apps/server/dist ./apps/server/dist
RUN chmod +x entrypoint.sh && mkdir -p /app/data/uploads /app/data/outputs

ENV PDF_OCR_ROOT=/app \
    PDF_OCR_DEVICE=gpu:0 \
    PYTHONPATH=/app/worker/src \
    PDF_OCR_STATIC=/app/apps/spa/dist \
    PDF_OCR_DB=/app/data/pdf-ocr.db \
    PDF_OCR_SCHEMA=/app/db/schema.sql \
    PDF_OCR_UPLOADS=/app/data/uploads \
    PDF_OCR_OUTPUTS=/app/data/outputs \
    HOST=0.0.0.0 \
    PORT=5000

EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
  CMD curl -f http://localhost:5000/api/v1/health || exit 1
ENTRYPOINT ["/app/entrypoint.sh"]
