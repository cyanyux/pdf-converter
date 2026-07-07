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
# uv: SOTA Python installer/resolver (parallel downloads + fast resolver, replaces pip).
# Paired with the BuildKit --mount=type=cache below, every wheel is fetched at most once
# across ALL rebuilds — editing a dep line relinks from cache instead of re-downloading.
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /uvx /usr/local/bin/
ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Taipei \
    DISABLE_MODEL_SOURCE_CHECK=True \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    UV_LINK_MODE=copy

# System deps (Ubuntu 24.04 ships Python 3.12). No nodesource/gnupg: the single Node.js
# binary is copied from the build stage below (Vite+ resolved it from .node-version),
# keeping the Node version identical build↔runtime. libatomic1 is a runtime dep of the
# Node 26 binary (Node 24 did not need it). apt caches live in BuildKit cache mounts (so
# the package/list downloads persist across rebuilds); hence no `rm -rf` of the lists.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv curl ca-certificates \
      libgl1 libglib2.0-0 libgomp1 libatomic1 supervisor
COPY --from=build /tmp/node /usr/local/bin/node

# One venv, created by uv from the system 3.12 interpreter. --seed puts pip in the venv so
# the optional runtime `paddleocr install_hpi_deps` (entrypoint.sh) still works.
RUN uv venv --seed --python /usr/bin/python3.12 /opt/venv

# PaddlePaddle GPU (cu126) in its own layer — the big ~1 GB wheel (6.8 GB installed) stays
# cached across app-dependency changes, and the uv cache mount means it downloads at most
# once even if this layer is ever invalidated.
# --index-strategy unsafe-best-match: paddlepaddle-gpu==3.3.1 lives ONLY on the cu126 mirror,
# but the name also exists on PyPI at other versions. uv's default (first-index) guard would
# lock to PyPI and fail; unsafe-best-match considers both indexes (both official/trusted here),
# restoring pip's merge-all-indexes behavior.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv/bin/python --index-strategy unsafe-best-match paddlepaddle-gpu==3.3.1 \
      --index-url https://www.paddlepaddle.org.cn/packages/stable/cu126/ \
      --extra-index-url https://pypi.org/simple/
# PaddleOCR 3.7 stack (PP-OCRv6 searchable-PDF + PaddleOCR-VL doc-parse) + doc utilities.
# opencc: Simplified->Traditional (Taiwan) conversion for zh-TW output (text_utils._s2tw). paddlex
# declares it ONLY under its base/speech extras, and we install the doc-parser extra, so it is NOT
# pulled transitively — it must be listed explicitly or zh-TW jobs fail with "No module named 'opencc'".
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv/bin/python \
      "paddleocr[doc-parser]==3.7.0" pymupdf==1.28.0 python-docx==1.2.0 \
      docxcompose beautifulsoup4==4.15.0 "numpy<2.4" opencc==1.4.0
# ONNX Runtime engine for PP-OCR (paddle2onnx export + onnxruntime-gpu): ~1.14x faster than
# native Paddle on PP-OCRv6, identical output. Separate layer so the big paddle layers stay
# cached. Selected via PDF_CONVERTER_ENGINE=onnxruntime (default); auto-falls back to Paddle.
# Pinned to the 1.23 line: it targets CUDA 12.x (matches the base image); 1.24+ links CUDA 13.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv/bin/python onnxruntime-gpu==1.23.0 paddle2onnx==2.0.2rc3
# Docling (born-digital markdown, CPU/text-faithful). CRITICAL: install CPU-only torch FIRST,
# from the PyTorch CPU index, so docling reuses it and its ~5 GB of CUDA nvidia-* wheels NEVER
# enter the image (the docling child runs GPU-hidden and must never touch CUDA anyway). Two
# separate layers, both cache-mounted like the paddle blocks above:
#   1. torch+torchvision from the CPU index (own --index-url so no CUDA build is even considered).
#   2. docling==2.110.0 from PyPI, which sees torch already satisfied and pulls no torch/nvidia.
# --index-strategy unsafe-best-match mirrors the paddle block's house style (merge indexes); the
# CPU index is the only source of these +cpu wheels while transitive deps resolve from PyPI.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv/bin/python --index-strategy unsafe-best-match \
      torch torchvision \
      --index-url https://download.pytorch.org/whl/cpu \
      --extra-index-url https://pypi.org/simple/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv/bin/python docling==2.110.0

WORKDIR /app
COPY db ./db
COPY worker/src ./worker/src
COPY supervisord.conf entrypoint.sh ./
COPY --from=build /app/apps/spa/dist ./apps/spa/dist
COPY --from=build /app/apps/server/dist ./apps/server/dist
RUN chmod +x entrypoint.sh && mkdir -p /app/data/uploads /app/data/outputs

ENV PDF_CONVERTER_ROOT=/app \
    PDF_CONVERTER_DEVICE=gpu:0 \
    CUDA_MODULE_LOADING=EAGER \
    PYTHONPATH=/app/worker/src \
    PDF_CONVERTER_STATIC=/app/apps/spa/dist \
    PDF_CONVERTER_DB=/app/data/pdf-converter.db \
    PDF_CONVERTER_SCHEMA=/app/db/schema.sql \
    PDF_CONVERTER_UPLOADS=/app/data/uploads \
    PDF_CONVERTER_OUTPUTS=/app/data/outputs \
    HOST=0.0.0.0 \
    PORT=5000

EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:5000/api/v1/health?worker=required" || exit 1
ENTRYPOINT ["/app/entrypoint.sh"]
