"""FastAPI application: browse raw robot datasets on S3 and stream decoded video.

Multiple dataset formats are supported via source adapters (see sources.py); the
routes are source-scoped: /api/sources/{sid}/...
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import cache, catalog, clips, config, contrib, raiden_teachers, sources

logger = logging.getLogger("raiden_viz")


def _warm_catalog() -> None:
    """Start every source's deep build so a cold cache is not a user's problem.

    start_deep is idempotent (it no-ops on a running or fresh card) and returns
    immediately, spawning one daemon thread per source — so this cannot delay
    startup or the health check. Cards restored from the derived tier are already
    good, so this is a no-op for them until their TTL expires.
    """
    try:
        available = sources.get_sources(config.SOURCES)
        for spec in config.SOURCES:
            src = available.get(spec["id"])
            if src is not None:
                _CATALOG.start_deep(spec["id"], src)
        logger.info("catalog warmup started for %d source(s)", len(available))
    except Exception:
        # Warmup is an optimisation. A container that cannot warm its cache must
        # still serve, so this can never be fatal.
        logger.exception("catalog warmup failed")


def _configure_logging() -> None:
    """Give the raiden_viz loggers a handler.

    Nothing else configures logging in this app, so the logger has no handlers and
    Python's lastResort fallback applies — WARNING and above only. logger.exception
    therefore reached CloudWatch, but every INFO line was silently dropped, which is
    a poor trade in a container whose only window is its logs.

    Configured on the `raiden_viz` logger rather than the root logger so uvicorn's
    own handlers are left alone, and called from the lifespan rather than at import
    so merely importing this module mutates no global logging state.
    """
    log = logging.getLogger("raiden_viz")
    log.setLevel(config.LOG_LEVEL)
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
        log.addHandler(handler)


def _warm_scans() -> None:
    """Start every source's full per-episode scan at boot.

    The scan backs the episode filter. Nothing started it before this: a user
    clicked #filter-scan-btn and then watched it finish, which on the largest source
    is ~51 minutes. Deploying at a quiet hour only helps if the scan runs then too.

    scan_start is idempotent and returns immediately, spawning its own workers, so
    this cannot delay startup or the health check.

    Each source is guarded SEPARATELY: one source that cannot be listed must not
    stop the others, and none of it may be fatal to startup.
    """
    try:
        available = sources.get_sources(config.SOURCES)
    except Exception:
        logger.exception("scan warmup failed to resolve sources")
        return
    started = 0
    for spec in config.SOURCES:
        src = available.get(spec["id"])
        if src is None:
            continue
        try:
            src.scan_start()
            started += 1
        except Exception:
            logger.exception("scan warmup failed for %s", spec["id"])
    logger.info("scan warmup started for %d source(s)", started)


@asynccontextmanager
async def _lifespan(_app):
    _configure_logging()          # before the warmup, so its log line is visible
    if config.WARM_CATALOG_ON_START:
        _warm_catalog()
    # After the catalog: its cards are what the landing page needs first, and since
    # #6 sampled them they finish in seconds, where a scan runs for ~an hour.
    if config.WARM_SCANS_ON_START:
        _warm_scans()
    yield


app = FastAPI(title="YAM Datasets Viewer", version="0.3.0", lifespan=_lifespan)

_STATIC = Path(__file__).resolve().parent.parent / "static"
_CATALOG = catalog.CatalogBuilder()
_CLIPS = clips.ClipJobs()

# Adapter failures the frontend should tell apart: a camera that is not there at all
# versus one whose .svo2 is a stub header with no recorded video in it. Anything else
# is a genuine decode fault and reports as 500.
_CLIP_ERROR_STATUS = {"FileNotFoundError": 404, "ValueError": 422}
_CONTRIB = contrib.ContribBuilder()
_TEACHERS = raiden_teachers.TeacherBuilder()


def _src(sid: str):
    try:
        return sources.get_source(config.SOURCES, sid)
    except KeyError:
        raise HTTPException(404, f"unknown source: {sid}")


@app.get("/api/sources")
def list_sources():
    """The datasets this viewer can browse. Only sources actually readable on
    this host are listed (access-gated ones auto-hide where creds are missing)."""
    available = sources.get_sources(config.SOURCES)  # access-filtered registry
    return {"sources": [{"id": s["id"], "label": s["label"], "kind": s["kind"]}
                        for s in config.SOURCES if s["id"] in available]}


@app.get("/api/health")
def health():
    return {"ok": True, "sources": [s["id"] for s in config.SOURCES]}


@app.get("/api/catalog")
def get_catalog():
    """Landing-page data: one summary card per readable dataset + cross-dataset
    aggregates. NEVER computes inline (even task/episode counts hit S3 hard across
    227k+ episodes) — it serves cards from cache and kicks off any missing, stale or
    previously-FAILED one in the background (start_deep throttles the retries). An uncached card returns building=true
    with just id/label/kind; the frontend polls until every card is ready. This
    keeps the landing page instant regardless of dataset size."""
    available = sources.get_sources(config.SOURCES)
    cards = []
    for spec in config.SOURCES:
        if spec["id"] not in available:
            continue  # access-gated + unreadable here
        src = available[spec["id"]]
        card = _CATALOG.get_card(spec["id"])
        # start_deep owns EVERY skip decision — already running, finished and still
        # fresh, failed but inside its cooldown — so call it unconditionally and let
        # it no-op. Gating it on `building` here was a bug that made two recovery
        # paths unreachable: a FAILED card has building=false, so it was served
        # forever and never retried (and the frontend stops polling once nothing is
        # building, so it never re-asked either), and a good-but-stale card is also
        # building=false, which made the TTL refresh dead code.
        _CATALOG.start_deep(spec["id"], src)
        # Serve whatever is cached — a finished card, a phase-1 card with counts, or
        # a bare stub on a first build. Overlay the LIVE label/kind from config so a
        # rename shows immediately without wiping the (label-frozen) deep cache.
        base = card or {"id": spec["id"], "building": True}
        cards.append({**base, "label": spec["label"], "kind": spec["kind"]})
    agg = {
        "num_datasets": len(cards),
        "total_episodes": sum(c.get("num_episodes") or 0 for c in cards),
        "total_tasks": sum(c.get("num_tasks") or 0 for c in cards),
        "total_hours": round(sum(c.get("total_hours") or 0 for c in cards), 1),
        "with_annotations": sum(1 for c in cards if c.get("annotations") == "yes"),
        "building": sum(1 for c in cards if c.get("building")),
    }
    return {"aggregate": agg, "datasets": cards, "region": config.AWS_REGION}


@app.post("/api/catalog/{sid}/rebuild")
def rebuild_catalog(sid: str):
    """Force a rebuild of one dataset's deep summary (invalidates cache, re-scans)."""
    src = _src(sid)
    _CATALOG.invalidate(sid)
    _CATALOG.start_deep(sid, src, force=True)
    return {"ok": True, "building": True}


@app.get("/api/contrib")
def get_contrib():
    """Upload contribution calendar: how much data landed on S3 each day, merged
    across datasets (GitHub-style graph). Like /api/catalog it NEVER scans inline —
    it serves each source's cached daily rollup and kicks missing ones off in the
    background, returning building=true until every source's recursive listing is
    done. The merged per-day series (files/bytes/episodes) is assembled here from
    the cheap cached rollups, so the response is instant once built."""
    available = sources.get_sources(config.SOURCES)
    merged: dict[str, dict] = {}
    per_dataset = []
    building = 0
    tot_files = tot_bytes = tot_eps = 0
    for spec in config.SOURCES:
        if spec["id"] not in available:
            continue  # access-gated + unreadable here
        src = available[spec["id"]]
        roll = _CONTRIB.get(spec["id"])
        if roll is None or roll.get("building"):
            _CONTRIB.start(spec, src)
            building += 1
            per_dataset.append({"id": spec["id"], "label": spec["label"], "building": True})
            continue
        t = roll.get("totals", {})
        tot_files += t.get("files", 0)
        tot_bytes += t.get("bytes", 0)
        tot_eps += t.get("episodes", 0)
        per_dataset.append({
            "id": spec["id"], "label": spec["label"], "kind": roll.get("kind"),
            "counts_episodes": roll.get("counts_episodes", False),
            "totals": t, "span": roll.get("span"), "built_ok": roll.get("built_ok", True),
            # Per-dataset day rollup so the frontend can re-merge for any subset
            # (e.g. "just raiden") with no extra request. Rollups are tiny (one row
            # per active day), so shipping them all is cheap.
            "days": roll.get("days") or {},
        })
        for day, v in (roll.get("days") or {}).items():
            m = merged.setdefault(day, {"files": 0, "bytes": 0, "episodes": 0})
            m["files"] += v.get("files", 0)
            m["bytes"] += v.get("bytes", 0)
            m["episodes"] += v.get("episodes", 0)
    day_keys = sorted(merged.keys())
    return {
        "days": merged,
        "datasets": per_dataset,
        "building": building,
        "totals": {"files": tot_files, "bytes": tot_bytes, "episodes": tot_eps,
                   "days_active": len(day_keys)},
        "span": {"first": day_keys[0] if day_keys else None,
                 "last": day_keys[-1] if day_keys else None},
        "region": config.AWS_REGION,
    }


@app.post("/api/contrib/{sid}/rebuild")
def rebuild_contrib(sid: str):
    """Force a re-scan of one dataset's daily upload rollup."""
    src = _src(sid)
    _CONTRIB.invalidate(sid)
    _CONTRIB.start(src.spec, src, force=True)
    return {"ok": True, "building": True}


@app.get("/api/raiden_teachers")
def get_raiden_teachers():
    """Raiden teleop-by-teacher daily breakdown for the per-teacher bar chart.

    Only raiden-format sources record ``teacher_name`` (raiden / yam_russet /
    rollouts), so we scan just those and merge their per-(day, teacher) rollups.
    Like the other calendars this serves cached rollups instantly and scans
    missing ones in the background (building=true until done). Days are keyed by
    CAPTURE date (metadata timestamp) — "how much teleop we collected per day"."""
    available = sources.get_sources(config.SOURCES)
    merged: dict[str, dict] = {}          # day -> {teacher -> {episodes, seconds}}
    teacher_totals: dict[str, dict] = {}  # teacher -> {episodes, seconds}
    building = 0
    considered = 0
    for spec in config.SOURCES:
        if spec["id"] not in available or not raiden_teachers.supports(spec):
            continue
        considered += 1
        src = available[spec["id"]]
        roll = _TEACHERS.get(spec["id"])
        if roll is None or roll.get("building"):
            _TEACHERS.start(spec, src)
            building += 1
            continue
        for day, per in (roll.get("days") or {}).items():
            md = merged.setdefault(day, {})
            for teacher, v in per.items():
                m = md.setdefault(teacher, {"episodes": 0, "seconds": 0.0})
                m["episodes"] += v.get("episodes", 0)
                m["seconds"] += v.get("seconds", 0.0)
        for teacher, v in (roll.get("totals_by_teacher") or {}).items():
            tt = teacher_totals.setdefault(teacher, {"episodes": 0, "seconds": 0.0})
            tt["episodes"] += v.get("episodes", 0)
            tt["seconds"] += v.get("seconds", 0.0)
    teachers = sorted(teacher_totals.keys(),
                      key=lambda t: teacher_totals[t]["episodes"], reverse=True)
    day_keys = sorted(merged.keys())
    return {
        "days": merged,
        "teachers": teachers,
        "totals_by_teacher": {t: {"episodes": v["episodes"], "seconds": round(v["seconds"], 1)}
                              for t, v in teacher_totals.items()},
        "building": building,
        "sources_considered": considered,
        "span": {"first": day_keys[0] if day_keys else None,
                 "last": day_keys[-1] if day_keys else None},
    }


@app.post("/api/raiden_teachers/{sid}/rebuild")
def rebuild_raiden_teachers(sid: str):
    """Force a re-scan of one raiden source's teacher-by-day rollup."""
    src = _src(sid)
    _TEACHERS.invalidate(sid)
    _TEACHERS.start(src.spec, src, force=True)
    return {"ok": True, "building": True}


@app.get("/api/sources/{sid}/overview")
def overview(sid: str):
    ov = _src(sid).overview()
    ov["region"] = config.AWS_REGION
    return ov


@app.get("/api/sources/{sid}/task-teachers")
def task_teachers(sid: str):
    """Who teleoperated each task, for the Tasks card's robot-teacher filter:
    {task: {teacher: {episodes, seconds}}} plus the robot-teacher roster.

    Reuses the teacher scan behind the per-day chart (cached, built in the
    background), so this is exact — every episode, not the sampled stats pass.
    Sources whose format records no teacher return supported=false, and
    building=true until a first scan finishes."""
    src = _src(sid)
    if not raiden_teachers.supports(src.spec):
        return {"supported": False, "building": False, "tasks": {},
                "robot_teachers": config.ROBOT_TEACHERS}
    roll = _TEACHERS.get(sid)
    if roll is None or roll.get("building"):
        _TEACHERS.start(src.spec, src)
        return {"supported": True, "building": True, "tasks": {},
                "robot_teachers": config.ROBOT_TEACHERS}
    return {"supported": True, "building": False, "tasks": roll.get("tasks") or {},
            "robot_teachers": config.ROBOT_TEACHERS}


@app.get("/api/sources/{sid}/stats")
def stats(sid: str, full: bool = Query(False)):
    """Per-episode stat records for the charts. ``full=true`` reads every episode
    synchronously (small sources only); for large sources use the scan endpoints,
    which stream progress."""
    return _src(sid).stats(full=full)


@app.post("/api/sources/{sid}/scan")
def scan_start(sid: str):
    """Kick off (or resume) a cached background full scan of every episode's cheap
    stats — the data behind the episode filter. Returns an immediate snapshot."""
    return _src(sid).scan_start()


@app.get("/api/sources/{sid}/scan")
def scan_poll(sid: str):
    """Progress + accumulated records of an in-flight/finished scan (404 if none)."""
    snap = _src(sid).scan_snapshot()
    if snap is None:
        raise HTTPException(404, "no scan started for this source")
    return snap


@app.get("/api/sources/{sid}/tasks")
def list_tasks(sid: str):
    return {"tasks": _src(sid).list_tasks()}


@app.get("/api/sources/{sid}/tasks/{task}/episodes")
def list_episodes(sid: str, task: str):
    return {"task": task, "episodes": _src(sid).list_episodes(task)}


# Not nested under /episodes/ so it can't be mistaken for an episode named "facts".
@app.get("/api/sources/{sid}/tasks/{task}/episode-facts")
def episode_facts(sid: str, task: str):
    """Timestamp + status per episode, for labelling the browse list. Fetched
    separately from /episodes so the list renders immediately and the labels fill
    in when ready; {} where a source has nothing cheap to report."""
    return {"task": task, "facts": _src(sid).episode_facts(task)}


@app.get("/api/sources/{sid}/tasks/{task}/episodes/{episode}")
def episode_detail(sid: str, task: str, episode: str):
    try:
        return _src(sid).episode_detail(task, episode)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.get("/api/sources/{sid}/tasks/{task}/episodes/{episode}/video")
def episode_video(sid: str, task: str, episode: str, camera: str, eye: str = Query("left")):
    """Decode (and cache) one camera to MP4, then hand it to the browser.

    When the derived-artifact tier is configured the clip is served by redirecting to
    a presigned S3 URL rather than streaming it back through the app: an episode MP4
    is tens to hundreds of MB, and putting that on the app's critical path caps
    concurrency at whatever the single task can push.
    """
    try:
        mp4 = _src(sid).video_path(task, episode, camera, eye)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    # video_path() has already published the clip via cache.get_or_create, so a URL
    # here is the common case once the tier is on. Key off the file NAME: that is the
    # cache key, and it does not match camera/eye for every adapter (the lerobot one
    # folds a time window into it).
    url = cache.remote_url(mp4.name)
    if url:
        return RedirectResponse(url, status_code=302)
    return FileResponse(mp4, media_type="video/mp4", filename=f"{camera}_{eye}.mp4")


@app.get("/api/sources/{sid}/tasks/{task}/episodes/{episode}/video/status")
def episode_video_status(sid: str, task: str, episode: str, camera: str,
                         eye: str = Query("left")):
    """Whether one clip is decoded yet — starting the decode if it has not begun.

    The decode CANNOT happen inside the /video request. It takes minutes (~2.5 for a
    100 MB .svo2: a cross-region download plus an ffmpeg pass) and the load
    balancer's idle timeout is 60s, so the browser's connection is severed long
    first — which a <video> element renders as "Could not decode this stream", a
    decode error for what is actually a timeout, while the decode goes on to finish
    on the server and lands in the derived tier unused.

    So the frontend polls this and only sets video.src once ready, by which point
    /video resolves from cache. /video itself is unchanged and still works directly.
    """
    src = _src(sid)
    key = clips.job_key(sid, task, episode, camera, eye)
    state = _CLIPS.ensure(key, lambda: src.video_path(task, episode, camera, eye))
    if state["error"]:
        raise HTTPException(_CLIP_ERROR_STATUS.get(state["error_type"], 500),
                            state["error"])
    return {"ready": state["ready"], "decoding": state["decoding"]}


@app.get("/api/sources/{sid}/tasks/{task}/episodes/{episode}/calib")
def episode_calib(sid: str, task: str, episode: str, camera: str):
    """Calibration-check overlay: arm-base frames projected onto a still frame."""
    src = _src(sid)
    if not hasattr(src, "calib_overlay_path"):
        raise HTTPException(422, "calibration overlay not supported for this source")
    try:
        png = src.calib_overlay_path(task, episode, camera)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return FileResponse(png, media_type="image/png", filename=f"{camera}_calib.png")


@app.exception_handler(Exception)
async def _unhandled(_request, exc: Exception):
    """Log the detail, return a generic body.

    str(exc) on an unhandled error puts bucket names, key prefixes and AWS principals
    in front of whoever typed the URL -- and the app has no authentication, so that
    audience is everyone on the network.
    """
    logger.exception("unhandled error", exc_info=exc)
    return JSONResponse(status_code=500, content={"error": "internal server error"})


# Serve the frontend at the root. Mounted last so /api/* wins.
app.mount("/", StaticFiles(directory=str(_STATIC), html=True), name="static")
