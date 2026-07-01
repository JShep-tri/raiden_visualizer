"""Gunicorn config for the Raiden dataset viewer (production serving).

FastAPI is ASGI, so we run it under gunicorn's UvicornWorker — a real process
manager with multiple workers and graceful restarts, matching how AnyFile is
hosted at TRI. Settings come from env vars (gunicorn never runs __main__):

    CACHE_DIR / RAIDEN_CACHE_DIR              - decode cache location
    RAIDEN_S3_BUCKET / _PREFIX / _AWS_REGION  - dataset location

Usage:
    gunicorn raiden_viz.app:app -c gunicorn.conf.py
"""

import os

worker_class = "uvicorn.workers.UvicornWorker"
bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:8080")

# Video work is I/O- and ffmpeg-bound; a few workers is plenty. Capped like AnyFile.
workers = min(int(os.environ.get("GUNICORN_WORKERS", 4)), 8)

# A cold clip = S3 download + ffmpeg transcode, which can take several seconds.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", 180))

keepalive = 5
accesslog = "-"
errorlog = "-"
