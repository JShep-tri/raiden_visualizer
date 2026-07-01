FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# ffmpeg is REQUIRED: the .svo2 -> mp4 decoder shells out to the ffmpeg and
# ffprobe binaries directly (this is the key difference from AnyFile, which
# leans on moviepy's bundled build). Without this the video endpoint 500s.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# uv for fast, reproducible installs (matches the TRI AnyFile setup).
RUN pip install --no-cache-dir uv

# Install deps first (cached layer), then copy the app.
COPY pyproject.toml ./
COPY raiden_viz ./raiden_viz
COPY static ./static
COPY gunicorn.conf.py ./
RUN uv pip install --system .

EXPOSE 8080

# gunicorn + UvicornWorker: multi-worker ASGI serving with graceful restarts.
CMD ["gunicorn", "raiden_viz.app:app", "-c", "gunicorn.conf.py"]
