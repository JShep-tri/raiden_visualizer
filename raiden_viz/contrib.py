"""Contribution calendar: how much data landed on S3 each day, per dataset.

A GitHub-style contribution graph for uploads. The signal is each S3 object's
``LastModified`` timestamp (already carried on ``S3Object``): bucket every object
under a source's prefix by upload day and sum bytes + file counts. We also count
*episodes* per day where the source's format has a one-file-per-episode marker
(raiden: metadata.json, yam/MCAP: the episode's mcap) — LeRobot packs many
episodes per file, so those report bytes/files only (episodes omitted, not faked).

Cost mirrors the catalog's full scan: a recursive list of every object under the
prefix. So, like the catalog, it runs in a BACKGROUND thread and caches the
per-day rollup to disk; an unbuilt source returns building=true and the frontend
polls until ready. The rollup is tiny (one row per day), so merging sources and
serving the calendar is instant once built.
"""

from __future__ import annotations

import threading
from collections import defaultdict

from . import cache, s3

# Primary per-episode file marker by source kind — counting these gives an
# episodes-per-day series. Kinds absent here (lerobot*) pack many episodes per
# file, so we report bytes/files only for them (episodes stay 0 = "not counted").
_EPISODE_MARKER = {
    "raiden": "metadata.json",
    "yam": None,   # filled per-source from spec["mcap_name"] (output.mcap / episode.mcap)
}

def _cache_key(sid: str) -> str:
    return f"contrib_v1_{sid}.json"


def _episode_suffix(spec: dict) -> str | None:
    """The key suffix that marks exactly one episode for this source, or None if
    the format packs episodes (so per-episode counting isn't meaningful)."""
    kind = spec.get("kind")
    if kind == "raiden":
        return "/metadata.json"
    if kind == "yam":
        return "/" + spec["mcap_name"]
    return None  # lerobot / lerobot_single: packed parquet, no 1:1 episode file


def build_days(spec: dict, src) -> dict:
    """Recursively list a source's objects and roll them up by upload day.

    Returns {days: {"YYYY-MM-DD": {files, bytes, episodes}}, totals, span}. Uses
    S3 LastModified; objects with no timestamp are skipped from the day rollup
    (still counted in totals so the numbers reconcile)."""
    prefix = spec["prefix"]
    bucket = spec.get("bucket")
    ep_suffix = _episode_suffix(spec)

    objs = s3.list_keys(prefix, bucket=bucket)
    days: dict[str, dict] = defaultdict(lambda: {"files": 0, "bytes": 0, "episodes": 0})
    total_files = total_bytes = total_eps = undated = 0
    for o in objs:
        total_files += 1
        total_bytes += o.size or 0
        is_ep = bool(ep_suffix and o.key.endswith(ep_suffix))
        if is_ep:
            total_eps += 1
        if not o.last_modified:
            undated += 1
            continue
        day = o.last_modified[:10]  # ISO8601 -> YYYY-MM-DD
        d = days[day]
        d["files"] += 1
        d["bytes"] += o.size or 0
        if is_ep:
            d["episodes"] += 1

    day_list = sorted(days.keys())
    return {
        "id": spec["id"], "label": spec["label"], "kind": spec["kind"],
        "days": days,
        "counts_episodes": ep_suffix is not None,
        "totals": {"files": total_files, "bytes": total_bytes,
                   "episodes": total_eps, "undated": undated},
        "span": {"first": day_list[0] if day_list else None,
                 "last": day_list[-1] if day_list else None},
        "built_ok": True, "building": False,
    }


class ContribBuilder:
    """Builds + caches per-source daily upload rollups in the background.

    Same lifecycle as CatalogBuilder: memory cache in front of a disk cache, one
    daemon thread per source, a building=true stub while the recursive listing
    runs. Reusing that shape keeps the landing page instant for large datasets."""

    def __init__(self):
        self._lock = threading.Lock()
        self._days: dict[str, dict] = {}
        self._running: dict[str, bool] = {}

    def get(self, sid: str) -> dict | None:
        with self._lock:
            d = self._days.get(sid)
        if d is not None:
            return d
        disk = cache.get_json(_cache_key(sid))
        if disk is not None and not disk.get("__invalidated__"):
            with self._lock:
                self._days[sid] = disk
            return disk
        return None

    def is_running(self, sid: str) -> bool:
        with self._lock:
            return self._running.get(sid, False)

    def _build(self, spec: dict, src) -> None:
        sid = spec["id"]
        with self._lock:
            if self._running.get(sid):
                return
            self._running[sid] = True
        try:
            result = build_days(spec, src)
        except Exception as e:  # a failed scan must not wedge the calendar
            result = {"id": sid, "label": spec.get("label", sid), "kind": spec.get("kind"),
                      "days": {}, "counts_episodes": False,
                      "totals": {"files": 0, "bytes": 0, "episodes": 0, "undated": 0},
                      "span": {"first": None, "last": None},
                      "built_ok": False, "building": False, "error": str(e)}
        cache.put_json(_cache_key(sid), result)
        with self._lock:
            self._days[sid] = result
            self._running[sid] = False

    def start(self, spec: dict, src, force: bool = False) -> None:
        sid = spec["id"]
        if self.is_running(sid):
            return
        cached = self.get(sid)
        if not force and cached is not None and not cached.get("building"):
            return
        threading.Thread(target=self._build, args=(spec, src), daemon=True).start()

    def invalidate(self, sid: str) -> None:
        with self._lock:
            self._days.pop(sid, None)
        cache.put_json(_cache_key(sid), {"__invalidated__": True})
