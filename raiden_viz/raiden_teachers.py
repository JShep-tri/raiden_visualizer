"""Raiden teleop-by-teacher daily breakdown.

The raiden episode metadata carries ``teacher_name`` (who teleoperated) and a
capture ``timestamp`` — so we can show how much raiden data each teacher
contributed per day, with a per-teacher toggle. This is raiden-specific: only the
raiden .svo2 format records a teacher, so this scanner runs against sources whose
kind is ``raiden`` (raiden, yam_russet, rollouts).

Cost: one small metadata.json GET per episode (raiden datasets are ~thousands of
episodes, not the 100k+ of the MCAP sets), so a full scan is affordable but still
run in a BACKGROUND thread + disk-cached, matching ContribBuilder/CatalogBuilder.

The per-episode metadata reads reuse each episode's cached stat where the source
already has one; otherwise a direct metadata.json GET. Day bucketing uses the
CAPTURE timestamp (when it was teleoperated), which is what "data added per day"
means for teleop — distinct from the S3-upload-time calendar in contrib.py.
"""

from __future__ import annotations

import threading
from collections import defaultdict

from . import cache, s3

_TEACHER_KINDS = {"raiden"}   # only the raiden format records teacher_name


def _cache_key(sid: str) -> str:
    # v2 adds the per-task rollup; bumping re-scans rather than serving a cache
    # that predates it (the scan is the same one the day chart already pays for).
    return f"teachers_v2_{sid}.json"


def supports(spec: dict) -> bool:
    return spec.get("kind") in _TEACHER_KINDS


def build_teachers(spec: dict, src) -> dict:
    """Scan every raiden episode's metadata → per-(day, teacher) and per-(task,
    teacher) rollups.

    Returns {days: {"YYYY-MM-DD": {teacher: {episodes, seconds}}}, tasks: {task:
    {teacher: {episodes, seconds}}}, teachers: [...], totals per teacher}. Day =
    capture date (metadata timestamp). Episodes missing a teacher are bucketed under
    "unknown"; missing a timestamp are skipped from the day rollup but still counted
    in the task rollup and per-teacher totals.

    The task rollup rides along on this scan (which already reads every episode's
    metadata) so the overview can narrow its Tasks card to the robot teachers'
    tasks exactly, rather than guessing from the sampled stats pass."""
    prefix = spec["prefix"]
    bucket = spec.get("bucket")

    days: dict[str, dict] = defaultdict(lambda: defaultdict(lambda: {"episodes": 0, "seconds": 0.0}))
    tasks: dict[str, dict] = defaultdict(lambda: defaultdict(lambda: {"episodes": 0, "seconds": 0.0}))
    by_teacher: dict[str, dict] = defaultdict(lambda: {"episodes": 0, "seconds": 0.0})
    n_eps = undated = 0

    for task in src.list_tasks():
        for ep in src.list_episodes(task):
            md = s3.try_get_json(f"{prefix}/{task}/{ep}/metadata.json", bucket=bucket)
            if md is None:
                continue
            n_eps += 1
            teacher = (md.get("teacher_name") or "unknown").strip() or "unknown"
            dur = md.get("duration_s") or 0.0
            by_teacher[teacher]["episodes"] += 1
            by_teacher[teacher]["seconds"] += dur
            t = tasks[task][teacher]
            t["episodes"] += 1
            t["seconds"] += dur
            ts = md.get("timestamp")
            if not ts or len(ts) < 10:
                undated += 1
                continue
            day = ts[:10]  # ISO 'YYYY-MM-DDTHH:MM:SS' -> date
            d = days[day][teacher]
            d["episodes"] += 1
            d["seconds"] += dur

    # Materialize defaultdicts to plain dicts for JSON.
    days_out = {day: {t: dict(v) for t, v in per.items()} for day, per in days.items()}
    tasks_out = {task: {t: dict(v) for t, v in per.items()} for task, per in tasks.items()}
    teachers = sorted(by_teacher.keys(), key=lambda t: by_teacher[t]["episodes"], reverse=True)
    day_keys = sorted(days_out.keys())
    return {
        "id": spec["id"], "label": spec["label"], "kind": spec["kind"],
        "days": days_out,
        "tasks": tasks_out,
        "teachers": teachers,
        "totals_by_teacher": {t: {"episodes": v["episodes"], "seconds": round(v["seconds"], 1)}
                              for t, v in by_teacher.items()},
        "totals": {"episodes": n_eps, "undated": undated},
        "span": {"first": day_keys[0] if day_keys else None,
                 "last": day_keys[-1] if day_keys else None},
        "built_ok": True, "building": False,
    }


class TeacherBuilder:
    """Builds + caches per-source teacher-by-day rollups in the background.
    Same lifecycle as ContribBuilder (memory→disk cache, one daemon per source,
    building=true stub while scanning)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._running: dict[str, bool] = {}

    def get(self, sid: str) -> dict | None:
        with self._lock:
            d = self._data.get(sid)
        if d is not None:
            return d
        disk = cache.get_json(_cache_key(sid))
        if disk is not None and not disk.get("__invalidated__"):
            with self._lock:
                self._data[sid] = disk
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
            result = build_teachers(spec, src)
        except Exception as e:
            result = {"id": sid, "label": spec.get("label", sid), "kind": spec.get("kind"),
                      "days": {}, "tasks": {}, "teachers": [], "totals_by_teacher": {},
                      "totals": {"episodes": 0, "undated": 0}, "span": {"first": None, "last": None},
                      "built_ok": False, "building": False, "error": str(e)}
        cache.put_json(_cache_key(sid), result)
        with self._lock:
            self._data[sid] = result
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
            self._data.pop(sid, None)
        cache.put_json(_cache_key(sid), {"__invalidated__": True})
