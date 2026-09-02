"""Catalog: cross-dataset aggregate + per-dataset summary cards for the landing page.

The landing page answers "what datasets do we have, how big, which have
annotations" without making the operator drill into each source. Building that
has two cost tiers:

  * CHEAP facts (format/kind, task count, total episodes, cameras) come from each
    source's overview() — one S3 listing pass, fast enough to compute live.
  * DEEP facts (durations, annotation presence, per-task stats) need reading
    episode records. For the huge sources (32k+ episodes) that's a full scan, so
    it's run in a BACKGROUND thread and cached to disk; the card shows "computing"
    until ready, then serves the cached summary. Annotation presence is decided by
    sample-PROBING a handful of episodes (a source's format may support subtask
    labels but carry none), not by a format capability flag alone.

One CatalogBuilder holds the in-memory state; results are also persisted via
cache.put_json so they survive a restart.
"""

from __future__ import annotations

import logging
import threading
import time

from . import cache

log = logging.getLogger(__name__)

# Which source formats CAN carry subtask annotations at all (probe only these;
# for others we can state "not supported" without reading anything).
_ANNOTATABLE_KINDS = {"yam", "lerobot", "lerobot_single"}

# How many episodes to sample-probe per source when checking for annotations.
_ANNOTATION_PROBE_N = 8

# How long a FAILED card is left alone before start_deep will retry it. A failure
# is usually transient (a missing cross-account grant that later lands, a throttle),
# so a failed card must not be permanent — but the frontend refetches /api/catalog
# every 4s while anything is building, so retrying on every poll would hammer S3
# for as long as the failure lasts.
_RETRY_COOLDOWN_S = 60

# Disk-cache key for a source's deep summary, versioned so a schema change
# invalidates old blobs.
def _cache_key(sid: str) -> str:
    return f"catalog_v1_{sid}.json"


def cheap_card(spec: dict, src) -> dict:
    """Fast per-dataset facts from one overview() pass — no episode reads."""
    ov = src.overview()
    # Cameras aren't in overview(); most sources expose them via info/spec. Best
    # effort: LeRobot sources know their cameras from info.json; others leave blank
    # (filled in by the deep pass from a sampled episode_detail).
    return {
        "id": spec["id"], "label": spec["label"], "kind": spec["kind"],
        "bucket": src.bucket, "prefix": src.prefix,
        "num_tasks": ov.get("num_tasks"), "num_episodes": ov.get("num_episodes"),
        "stations": ov.get("stations", []),
    }


def _probe_annotations(src, cheap: dict) -> dict:
    """Sample a few episodes and report whether annotations are actually present.

    Returns {annotations: "yes"|"none"|"unsupported"|"unknown", probed, found}.
    Only sources whose kind can carry annotations are probed; others are
    'unsupported'. A source that supports them but shows none across the sample is
    'none' (informative — capable but unlabeled)."""
    if src.spec.get("kind") not in _ANNOTATABLE_KINDS:
        return {"annotations": "unsupported", "probed": 0, "found": 0}
    # Gather a spread of (task, episode) pairs to sample.
    pairs = []
    try:
        for task in src.list_tasks():
            eps = src.list_episodes(task)
            if eps:
                pairs.append((task, eps[0]))
            if len(pairs) >= _ANNOTATION_PROBE_N:
                break
    except Exception:
        return {"annotations": "unknown", "probed": 0, "found": 0}
    found = 0
    probed = 0
    for task, ep in pairs[:_ANNOTATION_PROBE_N]:
        try:
            d = src.episode_detail(task, ep)
            probed += 1
            if d.get("annotations"):
                found += 1
        except Exception:
            continue
    if probed == 0:
        return {"annotations": "unknown", "probed": 0, "found": 0}
    return {"annotations": "yes" if found else "none", "probed": probed, "found": found}


def _sample_cameras(src) -> list[str]:
    """Camera names from one sampled episode (overview doesn't carry them)."""
    try:
        for task in src.list_tasks():
            eps = src.list_episodes(task)
            if eps:
                d = src.episode_detail(task, eps[0])
                return [c["name"] for c in d.get("cameras", [])]
    except Exception:
        pass
    return []


class CatalogBuilder:
    """Builds + caches per-dataset deep summaries in the background."""

    def __init__(self):
        self._lock = threading.Lock()
        self._deep: dict[str, dict] = {}     # sid -> deep summary
        self._running: dict[str, bool] = {}

    def _load_cached(self, sid: str) -> dict | None:
        return cache.get_json(_cache_key(sid))

    def get_card(self, sid: str) -> dict | None:
        """Cached card for a source (memory, then disk), or None if never built.
        The card carries its own ``building`` flag: a phase-1 blob has cheap facts
        + building=true; a finished blob has the deep stats + building=false."""
        with self._lock:
            d = self._deep.get(sid)
        if d is not None:
            return d
        disk = self._load_cached(sid)
        if disk is not None and not disk.get("__invalidated__"):
            with self._lock:
                self._deep[sid] = disk
            return disk
        return None

    def is_running(self, sid: str) -> bool:
        with self._lock:
            return self._running.get(sid, False)

    def build_deep(self, sid: str, src) -> None:
        """Compute the deep summary for one source (blocking). Runs the full stat
        scan (durations/frames per episode), sample-probes annotations + cameras,
        and caches the result. Safe to call in a background thread."""
        with self._lock:
            if self._running.get(sid):
                return
            self._running[sid] = True
        try:
            # Phase 1 — cheap facts (task/episode counts from overview()). Still an
            # S3 listing pass, but far cheaper than the full stat scan. Cache it
            # immediately (building=true) so the card shows counts within seconds
            # while the deep stats compute.
            cheap = cheap_card(src.spec, src)
            phase1 = {**cheap, "building": True}
            cache.put_json(_cache_key(sid), phase1)
            with self._lock:
                self._deep[sid] = phase1
            # Phase 2 — full stats: reuse the source's own scan machinery. stats(full)
            # reads every episode's cheap record (duration/frames/cameras/etc).
            st = src.stats(full=True)
            eps = st.get("episodes", [])
            durs = [e["duration_s"] for e in eps if e.get("duration_s")]
            total_hours = round(sum(durs) / 3600.0, 1) if durs else None
            ann = _probe_annotations(src, cheap)
            cams = _sample_cameras(src)
            # Per-task episode counts (top tasks) from the scan records.
            per_task: dict[str, int] = {}
            for e in eps:
                per_task[e.get("task", "?")] = per_task.get(e.get("task", "?"), 0) + 1
            top_tasks = sorted(per_task.items(), key=lambda kv: kv[1], reverse=True)[:10]
            deep = {
                **cheap,
                "cameras": cams,
                "annotations": ann["annotations"],
                "annotation_probe": ann,
                "scanned": st.get("scanned"),
                "total_episodes_scanned": st.get("total_episodes"),
                "sampled": st.get("sampled", False),
                "total_hours": total_hours,
                "duration_min_s": round(min(durs), 1) if durs else None,
                "duration_max_s": round(max(durs), 1) if durs else None,
                "duration_avg_s": round(sum(durs) / len(durs), 1) if durs else None,
                "top_tasks": [{"task": t, "episodes": n} for t, n in top_tasks],
                "built_ok": True, "building": False,
            }
        except Exception as e:  # a failed build must not wedge the card forever
            # LOG it. The card carries the message, but nothing renders a card's
            # error, so without this line a dead build is invisible in the app logs
            # and the dashboard just looks empty.
            log.exception("catalog: deep build failed for %s", sid)
            # Keep bucket/prefix: they come from the spec, not from the scan that
            # failed, and the card footer renders them. Without them the UI shows
            # "s3://undefined/undefined". getattr-guarded so a broken source object
            # cannot make the error handler itself throw.
            deep = {"id": sid, "label": src.spec.get("label", sid),
                    "kind": src.spec.get("kind"),
                    "bucket": getattr(src, "bucket", None),
                    "prefix": getattr(src, "prefix", None),
                    "built_ok": False, "building": False,
                    "error": str(e), "failed_at": time.time()}
        cache.put_json(_cache_key(sid), deep)
        with self._lock:
            self._deep[sid] = deep
            self._running[sid] = False

    def start_deep(self, sid: str, src, force: bool = False) -> None:
        """Kick off build_deep in a daemon thread. Skips if already running, or if a
        finished-and-SUCCESSFUL card is cached (unless ``force``). A phase-1
        (building=true) card with no running thread — e.g. left over from a restart
        mid-build — is (re)started so it completes, and a FAILED card is retried once
        its cooldown has elapsed so a transient error does not wedge it forever."""
        if self.is_running(sid):
            return
        card = self.get_card(sid)
        if not force and card is not None and not card.get("building"):
            if card.get("built_ok"):
                return                      # finished and good — nothing to do
            # FAILED card. Retry it, but not more often than the cooldown. A card
            # cached with no failed_at (an older schema) retries immediately.
            if time.time() - (card.get("failed_at") or 0) < _RETRY_COOLDOWN_S:
                return
        t = threading.Thread(target=self.build_deep, args=(sid, src), daemon=True)
        t.start()

    def invalidate(self, sid: str) -> None:
        """Drop the cached deep summary (memory + disk) so the next build recomputes."""
        with self._lock:
            self._deep.pop(sid, None)
        cache.put_json(_cache_key(sid), {"__invalidated__": True})
