# SDCodex — CivitAI browser, dataset manager & gallery-dl task runner
#
# The app is a single-process Flask application: APScheduler, the in-process
# DownloadManager worker and all gallery-dl/yt-dlp task subprocesses live in the
# one process. It therefore MUST run as exactly ONE gunicorn worker; concurrency
# comes from gunicorn threads, not multiple workers.
#
# All persistent state (SQLite DB, model downloads, saved gallery images, tasks,
# config/kiosks, quick-download output, HuggingFace model cache, rembg output)
# lives under /data and the app's static/ folders, which docker-compose mounts
# as volumes (named volumes or host bind mounts).

FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=5001 \
    TASKS_DIR=/data/tasks \
    CONFIG_DIR=/data/config \
    DOWNLOADS_DIR=/data/downloads \
    DATABASE_URL=sqlite:////data/db/sdcodex.db \
    HF_HOME=/data/huggingface \
    REMBG_OUTPUT=/data/rembg_output

WORKDIR /app

# Runtime system deps: ffmpeg gives yt-dlp post-processing (merge/thumbs),
# ca-certificates for gallery-dl/yt-dlp HTTPS. Everything else ships as
# manylinux wheels (Pillow bundles its own image libs).
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python deps first (layer cache: only reinstall when requirements change).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir gunicorn gallery-dl yt-dlp

# Application code
COPY . .

EXPOSE 5001

# Ensure volume mount points exist (SQLite needs the /data/db directory), then
# serve on 0.0.0.0:$PORT. --timeout 0 keeps long-lived streaming endpoints
# (task log streams) from being cut off.
CMD mkdir -p "$TASKS_DIR" "$CONFIG_DIR" "$DOWNLOADS_DIR" /data/db "$HF_HOME" "$REMBG_OUTPUT" /app/plugins \
    && exec gunicorn \
        --bind 0.0.0.0:"$PORT" \
        --workers 1 \
        --threads 8 \
        --timeout 0 \
        run:app

LABEL \
    org.opencontainers.image.title="SDCodex" \
    org.opencontainers.image.description="SDCodex is a web application built with Flask for exploring, organizing, and managing Stable Diffusion models. It provides a user-friendly interface to browse models by type, base model, and tags, alongside an integrated image gallery system." \
    org.opencontainers.image.url="https://github.com/nakedlittlezombie/SDCodex" \
    org.opencontainers.image.source="https://github.com/nakedlittlezombie/SDCodex" \
    org.opencontainers.image.documentation="https://github.com/nakedlittlezombie/SDCodex#readme" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.version="1.0.0" \
    org.opencontainers.image.authors="nakedlittlezombie@gmail.com"        