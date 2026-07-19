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


def get_or_create(cache_name: str, produce) -> Path:
    """Return cached file at ``cache_name``, invoking ``produce(path)`` to
    build it on a miss. ``produce`` must write the file at the given path."""
    dest = path_for(cache_name)
    if dest.exists() and dest.stat().st_size > 0:
        dest.touch()  # bump mtime for LRU
        return dest

    lock = _lock_for(cache_name)
    with lock:
        # Re-check inside the lock (another thread may have produced it).
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        # Unique temp name (pid + time) so concurrent cold misses never clobber
        # each other's partial writes before the atomic replace below.
        tmp = dest.with_suffix(dest.suffix + f".tmp{os.getpid()}_{int(time.time()*1000)%100000}")
        produce(tmp)
        tmp.replace(dest)
    _maybe_evict()
    return dest


def get_json(cache_name: str) -> dict | None:
    """Read a small cached JSON blob (e.g. a per-episode stat record), or None on
    miss/corruption. Bumps mtime so the LRU keeps hot records."""
    dest = path_for(cache_name)
    try:
        if dest.exists() and dest.stat().st_size > 0:
            dest.touch()
            return json.loads(dest.read_text())
    except (OSError, ValueError):
        return None
    return None


def put_json(cache_name: str, value: dict) -> None:
    """Write a small JSON blob atomically. Best-effort: caching a stat record must
    never break the request that produced it."""
    dest = path_for(cache_name)
    try:
        tmp = dest.with_suffix(dest.suffix + f".tmp{os.getpid()}_{int(time.time()*1000)%100000}")
        tmp.write_text(json.dumps(value))
        tmp.replace(dest)
    except OSError:
        pass


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
