"""FastAPI application: browse raw robot datasets on S3 and stream decoded video.

Multiple dataset formats are supported via source adapters (see sources.py); the
routes are source-scoped: /api/sources/{sid}/...
"""

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import catalog, config, contrib, sources

app = FastAPI(title="YAM Datasets Viewer", version="0.3.0")

_STATIC = Path(__file__).resolve().parent.parent / "static"
_CATALOG = catalog.CatalogBuilder()
_CONTRIB = contrib.ContribBuilder()


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
    227k+ episodes) — it serves cards from the on-disk cache and kicks off any
    missing/stale ones in the background. An uncached card returns building=true
    with just id/label/kind; the frontend polls until every card is ready. This
    keeps the landing page instant regardless of dataset size."""
    available = sources.get_sources(config.SOURCES)
    cards = []
    for spec in config.SOURCES:
        if spec["id"] not in available:
            continue  # access-gated + unreadable here
        src = available[spec["id"]]
        card = _CATALOG.get_card(spec["id"])
        if card is not None and not card.get("building"):
            # Overlay the LIVE label/kind from config so a rename shows immediately
            # without wiping the (label-frozen) deep-summary cache.
            cards.append({**card, "label": spec["label"], "kind": spec["kind"]})
        else:
            _CATALOG.start_deep(spec["id"], src)    # (re)build in background
            # serve the phase-1 card if we have it (counts), else a bare stub
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


@app.get("/api/sources/{sid}/overview")
def overview(sid: str):
    ov = _src(sid).overview()
    ov["region"] = config.AWS_REGION
    return ov


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


@app.get("/api/sources/{sid}/tasks/{task}/episodes/{episode}")
def episode_detail(sid: str, task: str, episode: str):
    try:
        return _src(sid).episode_detail(task, episode)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.get("/api/sources/{sid}/tasks/{task}/episodes/{episode}/video")
def episode_video(sid: str, task: str, episode: str, camera: str, eye: str = Query("left")):
    """Decode (and cache) one camera to MP4, then stream it."""
    try:
        mp4 = _src(sid).video_path(task, episode, camera, eye)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return FileResponse(mp4, media_type="video/mp4", filename=f"{camera}_{eye}.mp4")


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
    return JSONResponse(status_code=500, content={"error": str(exc)})


# Serve the frontend at the root. Mounted last so /api/* wins.
app.mount("/", StaticFiles(directory=str(_STATIC), html=True), name="static")
