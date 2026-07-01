"""FastAPI application: browse raw Raiden datasets on S3 and stream decoded video."""

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import cache, config, robot_data, s3, svo

app = FastAPI(title="Raiden Dataset Viewer", version="0.1.0")

_STATIC = Path(__file__).resolve().parent.parent / "static"


def _episode_prefix(task: str, episode: str) -> str:
    return f"{config.S3_PREFIX}/{task}/{episode}"


@app.get("/api/tasks")
def list_tasks():
    """Task folders directly under the dataset root."""
    return {"tasks": s3.list_dirs(config.S3_PREFIX)}


@app.get("/api/tasks/{task}/episodes")
def list_episodes(task: str):
    """Episode folders under a task, newest first (names are timestamped)."""
    episodes = s3.list_dirs(f"{config.S3_PREFIX}/{task}")
    return {"task": task, "episodes": sorted(episodes, reverse=True)}


@app.get("/api/tasks/{task}/episodes/{episode}")
def episode_detail(task: str, episode: str):
    """Full metadata for one episode: metadata.json, calibration, cameras, robot stats."""
    prefix = _episode_prefix(task, episode)

    metadata = s3.get_json(f"{prefix}/metadata.json")

    calibration = None
    if s3.try_head(f"{prefix}/calibration_results.json"):
        calibration = s3.get_json(f"{prefix}/calibration_results.json")

    # Enumerate camera .svo2 files and flag the tiny stub recordings (a
    # right_wrist_camera that only holds a header => no usable video).
    cameras = []
    for obj in s3.list_files(f"{prefix}/cameras"):
        if not obj.key.endswith(".svo2"):
            continue
        name = obj.key.rsplit("/", 1)[-1][: -len(".svo2")]
        cameras.append({
            "name": name,
            "size_mb": round(obj.size / 1024 / 1024, 1),
            "has_video": obj.size > 100_000,  # stub header files are ~1.5 KB
        })
    cameras.sort(key=lambda c: c["name"])

    robot = None
    robot_obj = s3.try_head(f"{prefix}/robot_data.npz")
    if robot_obj:
        npz = cache.get_or_create(
            f"{robot_obj.etag}_robot.npz",
            lambda dst: s3.download(robot_obj.key, dst),
        )
        robot = robot_data.summarize(npz)

    return {
        "task": task,
        "episode": episode,
        "metadata": metadata,
        "calibration": calibration,
        "cameras": cameras,
        "robot": robot,
    }


@app.get("/api/tasks/{task}/episodes/{episode}/video")
def episode_video(task: str, episode: str, camera: str, eye: str = Query("left")):
    """Decode (and cache) one camera+eye to MP4, then stream it."""
    prefix = _episode_prefix(task, episode)
    key = f"{prefix}/cameras/{camera}.svo2"

    obj = s3.try_head(key)
    if obj is None:
        raise HTTPException(404, f"camera not found: {camera}")
    if obj.size < 100_000:
        raise HTTPException(422, f"camera '{camera}' has no recorded video (stub file)")

    # Cache the raw .svo2 (keyed by etag), then the decoded mp4 (keyed by etag+eye).
    svo_local = cache.get_or_create(
        f"{obj.etag}_{camera}.svo2",
        lambda dst: s3.download(key, dst),
    )

    def _produce(dst: Path):
        svo.decode_to_mp4(svo_local, dst, eye=eye)

    try:
        mp4 = cache.get_or_create(f"{obj.etag}_{camera}_{eye}.mp4", _produce)
    except ValueError as e:
        raise HTTPException(422, str(e))

    return FileResponse(mp4, media_type="video/mp4", filename=f"{camera}_{eye}.mp4")


@app.get("/api/health")
def health():
    return {"ok": True, "bucket": config.S3_BUCKET, "prefix": config.S3_PREFIX}


@app.exception_handler(Exception)
async def _unhandled(_request, exc: Exception):
    return JSONResponse(status_code=500, content={"error": str(exc)})


# Serve the frontend at the root. Mounted last so /api/* wins.
app.mount("/", StaticFiles(directory=str(_STATIC), html=True), name="static")
