"""Disk cache for downloaded .svo2 files and transcoded .mp4 clips.

Keyed by S3 ETag so a re-uploaded object transparently invalidates. A simple
size-based LRU eviction keeps the cache bounded. Writes go to a unique temp
file and are moved into place with an atomic ``os.replace``, so a concurrent
build can never serve a half-written file.
"""

import json
import os
import threading
import time
from pathlib import Path

from . import config

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    """Return a per-key lock so concurrent requests for the same clip wait
    rather than decoding the same file multiple times."""
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


def path_for(name: str) -> Path:
    return config.CACHE_DIR / name


# --- durable (remote) tier -------------------------------------------------
# A decoded clip costs a 200-880 MB download plus an ffmpeg pass to produce, and the
# local CACHE_DIR is per-host: it dies on every container redeploy and is not shared
# between tasks. Publishing derivatives to S3 makes that work permanent. Everything
# below no-ops when config.DERIVED_BUCKET is empty.

_derived: dict[str, object] = {}


def _derived_client():
    """boto3 client for the derived-artifact bucket, created lazily and cached."""
    if "c" not in _derived:
        import boto3

        _derived["c"] = boto3.client("s3", region_name=config.AWS_REGION)
    return _derived["c"]


def remote_enabled() -> bool:
    return bool(config.DERIVED_BUCKET)


def _remote_key(cache_name: str) -> str:
    return f"{config.DERIVED_PREFIX}/{cache_name}" if config.DERIVED_PREFIX else cache_name


def _remote_head(cache_name: str) -> bool:
    if not remote_enabled():
        return False
    try:
        _derived_client().head_object(
            Bucket=config.DERIVED_BUCKET, Key=_remote_key(cache_name)
        )
        return True
    except Exception:
        return False


def fetch_remote(cache_name: str, dest: Path) -> bool:
    """Download a previously-derived artifact into ``dest``. False on any miss.

    Downloads to a unique temp name and moves it into place, so a failed or partial
    transfer can never be served as a complete file.
    """
    if not remote_enabled():
        return False
    tmp = dest.with_suffix(dest.suffix + f".rtmp{os.getpid()}_{int(time.time()*1000)%100000}")
    try:
        _derived_client().download_file(
            Bucket=config.DERIVED_BUCKET, Key=_remote_key(cache_name), Filename=str(tmp)
        )
    except Exception:
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(dest)
    return True


def push_remote(cache_name: str, src: Path) -> None:
    """Best-effort upload. Caching is an optimisation, so a failure here must never
    fail the request that produced the artifact."""
    if not remote_enabled():
        return
    try:
        _derived_client().upload_file(
            Filename=str(src), Bucket=config.DERIVED_BUCKET, Key=_remote_key(cache_name)
        )
    except Exception:
        pass


def exists(cache_name: str) -> bool:
    """True if the artifact is available locally, pulling it from the derived tier
    if that is where it lives.

    Use this instead of ``path_for(name).exists()``: a bare local probe reports a
    miss for something the derived tier already holds, and the YAM adapter answers
    that miss by re-downloading a whole 200-880 MB MCAP.
    """
    dest = path_for(cache_name)
    if dest.exists() and dest.stat().st_size > 0:
        dest.touch()
        return True
    return fetch_remote(cache_name, dest)


def remote_url(cache_name: str) -> str | None:
    """Presigned GET for a derived artifact, or None if the tier is off or empty.

    Lets a caller hand the bytes straight to the browser instead of streaming them
    back through the app.
    """
    if not _remote_head(cache_name):
        return None
    return _derived_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": config.DERIVED_BUCKET, "Key": _remote_key(cache_name)},
        ExpiresIn=config.DERIVED_URL_TTL,
    )


def get_or_create(cache_name: str, produce, remote: bool = True) -> Path:
    """Return cached file at ``cache_name``, invoking ``produce(path)`` to
    build it on a miss. ``produce`` must write the file at the given path.

    Three tiers, cheapest first: local disk, the derived S3 bucket, then an actual
    decode. A fresh decode is published to the derived tier so the next container --
    or the next deploy -- skips it entirely.

    ``remote=False`` keeps an artifact local: the derived tier is neither read nor
    written for it. Use it for anything that is not expensive to PRODUCE -- notably
    a downloaded source object, which is already durable in its own bucket, so
    publishing it duplicates source data to buy back only a re-download.
    """
    dest = path_for(cache_name)
    if dest.exists() and dest.stat().st_size > 0:
        dest.touch()  # bump mtime for LRU
        return dest

    lock = _lock_for(cache_name)
    with lock:
        # Re-check inside the lock (another thread may have produced it).
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        # Remote tier before decoding: a download is minutes cheaper than an ffmpeg
        # pass over an 880 MB MCAP, and survives redeploys.
        if remote and fetch_remote(cache_name, dest):
            _maybe_evict()
            return dest
        # Unique temp name (pid + time) so concurrent cold misses never clobber
        # each other's partial writes before the atomic replace below.
        tmp = dest.with_suffix(dest.suffix + f".tmp{os.getpid()}_{int(time.time()*1000)%100000}")
        produce(tmp)
        tmp.replace(dest)
        if remote:
            push_remote(cache_name, dest)
    _maybe_evict()
    return dest


def _read_json(dest: Path) -> dict | None:
    """Local read, bumping mtime so the LRU keeps hot records. None on miss/corruption."""
    try:
        if dest.exists() and dest.stat().st_size > 0:
            dest.touch()
            return json.loads(dest.read_text())
    except (OSError, ValueError):
        return None
    return None


def get_json(cache_name: str, remote: bool = False) -> dict | None:
    """Read a small cached JSON blob (e.g. a per-episode stat record), or None on
    miss/corruption.

    ``remote`` opts this blob into the derived tier, for the few blobs worth
    surviving a container. CACHE_DIR is task-local and dies with the task, so a
    local-only blob is recomputed on every deploy and every unplanned restart. Pass
    it for blobs that are cheap to store and expensive to recompute (the catalog
    cards); leave it off for the high-volume per-episode stat records, where the
    round trips would cost more than the recompute.
    """
    dest = path_for(cache_name)
    d = _read_json(dest)
    if d is not None:
        return d
    # Local miss — fall back to the derived tier and re-seed the local copy, so
    # only the first read after a restart pays for the download.
    if remote and fetch_remote(cache_name, dest):
        return _read_json(dest)
    return None


def put_json(cache_name: str, value: dict, remote: bool = False) -> None:
    """Write a small JSON blob atomically. Best-effort: caching a stat record must
    never break the request that produced it. See get_json for ``remote``."""
    dest = path_for(cache_name)
    try:
        tmp = dest.with_suffix(dest.suffix + f".tmp{os.getpid()}_{int(time.time()*1000)%100000}")
        tmp.write_text(json.dumps(value))
        tmp.replace(dest)
    except OSError:
        return          # nothing on disk to push
    if remote:
        push_remote(cache_name, dest)


def evict(headroom_gb: float = 0.0) -> None:
    """Evict oldest cached files until usage is under (cap - headroom).

    Pass headroom_gb before a large download so there's room for it. In-flight
    ``.tmp`` files are ignored (not counted, not evicted)."""
    if config.CACHE_MAX_GB <= 0:
        return
    files = [p for p in config.CACHE_DIR.glob("*") if p.is_file() and ".tmp" not in p.name]
    total = sum(p.stat().st_size for p in files)
    limit = max(0, (config.CACHE_MAX_GB - headroom_gb)) * (1024**3)
    if total <= limit:
        return
    for p in sorted(files, key=lambda x: x.stat().st_mtime):  # oldest first
        try:
            total -= p.stat().st_size
            p.unlink()
        except OSError:
            continue
        if total <= limit:
            break


def _maybe_evict() -> None:
    evict()
