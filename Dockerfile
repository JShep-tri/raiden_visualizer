# syntax=docker/dockerfile:1
# Single-stage: the frontend in static/ is hand-written with no build step, and the
# app mounts it itself (app.py: app.mount("/", StaticFiles(...))). There is nothing
# to compile, so unlike the other TRI web-app images there is no node stage here.
FROM python:3.12-slim-bookworm AS app
WORKDIR /app

# svo.py and yam.py shell out to BOTH ffmpeg and ffprobe; Debian's ffmpeg package
# ships both. libgl1/libglib2.0-0 are opencv-python-headless's runtime deps, needed
# by calib_overlay.py.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install deps first, keyed on pyproject.toml, so this layer caches across
# source-only changes.
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
        "fastapi>=0.110" "uvicorn[standard]>=0.29" "boto3>=1.34" "mcap>=1.1" \
        "numpy>=1.24" "protobuf>=4.25" "opencv-python-headless>=4.8" "pyarrow>=15"

COPY raiden_viz ./raiden_viz
COPY static ./static
COPY README.md ./
RUN pip install --no-cache-dir --no-deps .

# Non-root. The cache dir must be writable by this uid: config.py runs
# CACHE_DIR.mkdir(parents=True, exist_ok=True) at import time, so an unwritable path
# fails the container at startup rather than at first request.
ARG UID=1001
RUN adduser --disabled-password --gecos "" --home /nonexistent \
        --shell /sbin/nologin --no-create-home --uid "${UID}" appuser \
    && mkdir -p /cache && chown appuser:appuser /cache
USER appuser

ENV PYTHONUNBUFFERED=1 \
    RAIDEN_HOST=0.0.0.0 \
    RAIDEN_PORT=8080 \
    RAIDEN_CACHE_DIR=/cache \
    RAIDEN_CACHE_MAX_GB=8

EXPOSE 8080
CMD ["uvicorn", "raiden_viz.app:app", "--host", "0.0.0.0", "--port", "8080"]
