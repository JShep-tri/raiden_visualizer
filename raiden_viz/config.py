"""Runtime configuration, read from environment with sensible defaults."""

import os
from pathlib import Path

# Default S3 root under which task/episode folders live. Overridable so the same
# viewer can point at other raw dataset roots.
#   s3://<bucket>/<prefix>/<task>/<episode>/{metadata.json, cameras/*.svo2, robot_data.npz}
S3_BUCKET = os.environ.get("RAIDEN_S3_BUCKET", "tri-ml-datasets-uw2")
S3_PREFIX = os.environ.get("RAIDEN_S3_PREFIX", "raiden_datasets/raw").strip("/")
AWS_REGION = os.environ.get("RAIDEN_AWS_REGION", "us-west-2")

# Local cache for downloaded .svo2 files and transcoded .mp4 clips. Decoding is
# expensive, so results are memoized on disk keyed by the S3 object's ETag+size.
# Honors CACHE_DIR (the shared convention used by other TRI viewers, e.g. AnyFile
# maps a persistent EBS volume there) with RAIDEN_CACHE_DIR taking precedence.
CACHE_DIR = Path(
    os.environ.get("RAIDEN_CACHE_DIR")
    or os.environ.get("CACHE_DIR")
    or "/tmp/raiden_viz_cache"
)

HOST = os.environ.get("RAIDEN_HOST", "0.0.0.0")
PORT = int(os.environ.get("RAIDEN_PORT", "8080"))

# Cap on cache size (GB); oldest clips are evicted past this. 0 disables eviction.
CACHE_MAX_GB = float(os.environ.get("RAIDEN_CACHE_MAX_GB", "20"))

CACHE_DIR.mkdir(parents=True, exist_ok=True)
