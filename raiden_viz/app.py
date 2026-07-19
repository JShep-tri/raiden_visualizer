"""FastAPI application: browse raw robot datasets on S3 and stream decoded video.

Multiple dataset formats are supported via source adapters (see sources.py); the
routes are source-scoped: /api/sources/{sid}/...
"""

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, sources

app = FastAPI(title="Raiden Dataset Viewer", version="0.2.0")

_STATIC = Path(__file__).resolve().parent.parent / "static"


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
