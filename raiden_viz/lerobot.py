"""Read LeRobot v3.0 datasets (packed parquet timeseries + AV1 video).

A LeRobot v3.0 dataset is a directory tree:

    <root>/meta/info.json                          # schema: fps, features, path templates
    <root>/meta/tasks.parquet                       # task_index -> natural-language instruction
    <root>/meta/episodes/chunk-*/file-*.parquet     # per-episode: which data/video file + time slice
    <root>/data/chunk-*/file-*.parquet              # packed timeseries (observation.state, action, ...)
    <root>/videos/<video_key>/chunk-*/file-*.mp4    # packed per-camera video (AV1)

Unlike the raiden/yam layouts (one folder or one MCAP per episode), MANY episodes
can share a single parquet/mp4 file; the ``meta/episodes`` parquet maps each
``episode_index`` to its (chunk, file) and its row/timestamp slice within those
shared files. In the yam_public bimanual dataset each file happens to hold exactly
one episode, but the slice-aware code here handles the packed case too.

Video is AV1 (not browser-decodable), so every clip is transcoded to H.264 —
trimmed to the episode's ``[from_timestamp, to_timestamp]`` window in the shared mp4.
"""

import io
import subprocess
from collections import OrderedDict
from pathlib import Path

import numpy as np
import pyarrow.compute as pc
import pyarrow.parquet as pq

# LeRobot names video features "observation.images.<cam>"; the short name is what
# the viewer shows and routes on, the full key is what the parquet columns use.
VIDEO_PREFIX = "observation.images."


def read_table(raw: bytes):
    """Read a parquet table from an in-memory byte blob (meta + data parquets are
    small — hundreds of KB — so they're fetched to memory rather than the cache)."""
    return pq.read_table(io.BytesIO(raw))


def parse_info(info: dict) -> dict:
    """Pull the fields the viewer needs from ``meta/info.json``."""
    feats = info.get("features", {}) or {}
    video_keys: dict[str, str] = {}
    for key, feat in feats.items():
        if feat.get("dtype") == "video":
            short = key[len(VIDEO_PREFIX):] if key.startswith(VIDEO_PREFIX) else key
            video_keys[short] = key
    return {
        "fps": info.get("fps"),
        "total_episodes": int(info.get("total_episodes") or 0),
        "robot_type": info.get("robot_type"),
        "cameras": sorted(video_keys),
        "video_keys": video_keys,
        "data_path": info.get("data_path", "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet"),
        "video_path": info.get("video_path", "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4"),
        "state_names": ((feats.get("observation.state") or {}).get("names")) or [],
        "action_names": ((feats.get("action") or {}).get("names")) or [],
    }


def parse_tasks(table) -> dict:
    """``meta/tasks.parquet`` -> {task_index: instruction text}."""
    cols = table.column_names
    n = table.num_rows
    idxs = table.column("task_index").to_pylist() if "task_index" in cols else list(range(n))
    text_col = next((c for c in ("task", "__index_level_0__") if c in cols), None)
    texts = table.column(text_col).to_pylist() if text_col else [None] * n
    return {i: t for i, t in zip(idxs, texts)}


def parse_episodes(table, video_keys: dict) -> dict:
    """``meta/episodes`` parquet -> {episode_index: row} where row holds the data
    file coordinates plus, per camera, the shared-mp4 file and its time window."""
    cols = set(table.column_names)

    def col(name, default=None):
        return table.column(name).to_pylist() if name in cols else None

    idxs = col("episode_index") or list(range(table.num_rows))
    lengths = col("length") or [None] * len(idxs)
    d_chunk = col("data/chunk_index") or [0] * len(idxs)
    d_file = col("data/file_index") or [0] * len(idxs)
    ep_tasks = col("tasks") or [None] * len(idxs)

    # Pre-list each camera's four columns once (avoid per-row scalar access).
    vcols = {}
    for short, full in video_keys.items():
        base = f"videos/{full}"
        if f"{base}/chunk_index" not in cols:
            continue
        vcols[short] = {
            "chunk": col(f"{base}/chunk_index"),
            "file": col(f"{base}/file_index"),
            "from_ts": col(f"{base}/from_timestamp"),
            "to_ts": col(f"{base}/to_timestamp"),
        }

    out = {}
    for i, idx in enumerate(idxs):
        vids = {}
        for short, c in vcols.items():
            vids[short] = {
                "chunk": c["chunk"][i],
                "file": c["file"][i],
                "from_ts": (c["from_ts"][i] if c["from_ts"] else 0.0) or 0.0,
                "to_ts": c["to_ts"][i] if c["to_ts"] else None,
            }
        out[idx] = {
            "episode_index": idx,
            "length": lengths[i],
            "data_chunk": d_chunk[i],
            "data_file": d_file[i],
            "tasks": ep_tasks[i],
            "videos": vids,
        }
    return out


def filter_episode(table, idx: int):
    """Rows of a (possibly multi-episode) data parquet belonging to one episode."""
    if "episode_index" not in table.column_names:
        return table
    return table.filter(pc.equal(table.column("episode_index"), idx))


def build_robot(table, info: dict, max_points: int = 600) -> dict:
    """Build the viewer's robot-trajectory shape (matching robot_data.summarize)
    from a single episode's rows: split ``observation.state`` / ``action`` into
    per-arm joint + gripper signals so the plots stay readable."""
    cols = table.column_names
    n = table.num_rows
    if n == 0:
        return {"keys": [], "signals": {}, "time": [], "summary": {"num_steps": 0}}

    fps = info.get("fps") or 0
    if "timestamp" in cols:
        ts = np.asarray(table.column("timestamp").to_pylist(), dtype=np.float64)
        ts = ts - ts[0]
    elif fps:
        ts = np.arange(n, dtype=np.float64) / fps
    else:
        ts = np.arange(n, dtype=np.float64)
    dur = float(ts[-1]) if n else 0.0

    stride = max(1, n // max_points)
    idx = np.arange(0, n, stride)

    signals: dict[str, dict] = {}

    def add(name, arr):
        arr = np.asarray(arr, dtype=np.float64)
        if arr.ndim == 1:
            arr = arr[:, None]
        signals[name] = {
            "dims": int(arr.shape[1]),
            "series": np.round(arr[idx], 5).tolist(),
            "min": round(float(arr.min()), 5),
            "max": round(float(arr.max()), 5),
        }

    _emit_split(table, cols, "observation.state", info.get("state_names"), "pos", add)
    _emit_split(table, cols, "action", info.get("action_names"), "cmd", add)

    summary = {"num_steps": n}
    if dur > 0:
        summary["duration_s"] = round(dur, 3)
        summary["hz"] = round(n / dur, 1)
    return {
        "keys": list(signals),
        "signals": signals,
        "time": np.round(ts[idx], 4).tolist(),
        "summary": summary,
    }


def _emit_split(table, cols, column: str, names, kind: str, add) -> None:
    """Split a wide vector column into per-arm joint/gripper signals by its
    feature ``names`` (e.g. left_waist..left_gripper, right_...). Falls back to a
    single signal when names are missing or don't match the width."""
    if column not in cols:
        return
    arr = np.asarray(table.column(column).to_pylist(), dtype=np.float64)
    if arr.ndim == 1:
        arr = arr[:, None]
    if not names or arr.shape[1] != len(names):
        add("joint_pos" if kind == "pos" else "joint_cmd", arr)
        return
    groups: "OrderedDict[str, list]" = OrderedDict()
    for j, nm in enumerate(names):
        nm = str(nm).lower()
        side = "l_" if nm.startswith("left") else "r_" if nm.startswith("right") else ""
        part = "gripper" if nm.endswith("gripper") else "joint"
        groups.setdefault(f"{side}{part}_{kind}", []).append(j)
    for name, jcols in groups.items():
        add(name, arr[:, jcols])


def subtasks_to_annotations(table) -> list:
    """The ``subtask`` string column changes over the episode; emit one timestamped
    annotation at each transition into a (non-empty) subtask, relative to episode
    start — the same shape as YAM's /subtask-annotation entries."""
    cols = table.column_names
    if "subtask" not in cols:
        return []
    subs = table.column("subtask").to_pylist()
    if "timestamp" in cols:
        ts = table.column("timestamp").to_pylist()
        t0 = ts[0] if ts else 0.0
    else:
        ts = list(range(len(subs)))
        t0 = 0.0
    out, prev = [], None
    for t, s in zip(ts, subs):
        if s != prev:
            if s:  # skip the '' gaps between subtasks
                out.append({"t": round(float(t) - float(t0), 3), "text": s})
            prev = s
    return out


def instruction_for(table, task_map: dict, row: dict):
    """Human-readable instruction for an episode: its task_index (from the data
    rows) resolved through tasks.parquet, falling back to the episode's task label."""
    if task_map and "task_index" in table.column_names and table.num_rows:
        ti = table.column("task_index")[0].as_py()
        if ti in task_map and task_map[ti]:
            return task_map[ti]
    if len(task_map) == 1:
        return next(iter(task_map.values()))
    tasks = row.get("tasks")
    if isinstance(tasks, (list, tuple)) and tasks:
        return ", ".join(str(t) for t in tasks)
    return None


def transcode(src: Path, dst: Path, from_ts: float = 0.0, to_ts=None, fps=None) -> dict:
    """Transcode one camera's clip (AV1) to browser-safe H.264, trimmed to the
    episode's window in the shared mp4. ``-ss``/``-to`` after ``-i`` are absolute,
    frame-accurate input timestamps; clips are short so the extra decode is cheap."""
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src)]
    if from_ts and from_ts > 0:
        cmd += ["-ss", f"{from_ts:.6f}"]
    if to_ts is not None:
        cmd += ["-to", f"{to_ts:.6f}"]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", "-f", "mp4", str(dst),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return {"from_ts": from_ts, "to_ts": to_ts}
