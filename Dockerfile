# syntax=docker/dockerfile:1

# ---------- Stage 1: build SPA + bundle server (Vite+ toolchain image) ----------
# The official Vite+ image ships the `vp` CLI + native toolchain; `vp` provisions the
# exact Node.js from .node-version and manages pnpm, so one image builds the whole repo.
FROM ghcr.io/voidzero-dev/vite-plus:0.2.2 AS build
WORKDIR /app
# Manifests + lockfile first so the install layer caches across source-only changes.
# (The image runs as the non-root `vp` user, hence --chown on every COPY.)
COPY --chown=vp:vp pnpm-workspace.yaml pnpm-lock.yaml package.json .node-version ./
COPY --chown=vp:vp packages/shared/package.json ./packages/shared/
COPY --chown=vp:vp apps/server/package.json ./apps/server/
COPY --chown=vp:vp apps/spa/package.json ./apps/spa/
RUN vp install --frozen-lockfile
# Source, then build: apps/spa/dist (static SPA) + apps/server/dist (self-contained bundle).
COPY --chown=vp:vp tsconfig.json vite.config.ts ./
COPY --chown=vp:vp packages ./packages
COPY --chown=vp:vp apps ./apps
RUN vp run -r build
# Export the toolchain-provisioned Node.js so the runtime stage stays vp-free.
RUN cp "$(vp env which node | head -1)" /tmp/node

# ---------- Stage 2: runtime (CUDA 12.6 + Python 3.12 + Node from build) ----------
FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04 AS runtime
ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Taipei \
    DISABLE_MODEL_SOURCE_CHECK=True \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    PATH="/opt/venv/bin:${PATH}"

# System deps (Ubuntu 24.04 ships Python 3.12). No nodesource/gnupg: the single Node.js
# binary is copied from the build stage below (Vite+ resolved it from .node-version),
# keeping the Node version identical build↔runtime. libatomic1 is a runtime dep of the
# Node 26 binary (Node 24 did not need it).
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip pandoc curl ca-certificates \
      libgl1 libglib2.0-0 libgomp1 libatomic1 supervisor \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /tmp/node /usr/local/bin/node

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
    CUDA_MODULE_LOADING=EAGER \
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
