"""Parse robot_data.npz into JSON-serializable trajectory summaries."""

from pathlib import Path

import numpy as np


def summarize(npz_path: Path, max_points: int = 600) -> dict:
    """Load robot_data.npz and return per-signal series + summary stats.

    Series are subsampled to at most ``max_points`` for lightweight plotting.
    """
    data = np.load(npz_path, allow_pickle=True)
    keys = list(data.files)

    out: dict = {"keys": keys, "signals": {}, "summary": {}}

    ts = None
    if "timestamps" in keys:
        ts = data["timestamps"].astype(np.float64)
        ts = (ts - ts[0]) / 1e9  # nanoseconds -> seconds from start
        out["summary"]["num_steps"] = int(ts.shape[0])
        out["summary"]["duration_s"] = round(float(ts[-1]), 3)
        if ts[-1] > 0:
            out["summary"]["hz"] = round(float(ts.shape[0] / ts[-1]), 1)

    n = ts.shape[0] if ts is not None else _infer_len(data, keys)
    stride = max(1, n // max_points) if n else 1
    idx = np.arange(0, n, stride) if n else np.array([], dtype=int)
    out["time"] = (ts[idx].tolist() if ts is not None else idx.tolist())

    # Group signals for tidy plotting: joints (multi-dim) and grippers (scalar).
    for k in keys:
        if k == "timestamps":
            continue
        arr = np.asarray(data[k])
        if arr.ndim == 1:
            arr = arr[:, None]
        if arr.ndim != 2 or arr.shape[0] != n:
            continue
        sub = arr[idx]
        out["signals"][k] = {
            "dims": int(arr.shape[1]),
            "series": np.round(sub.astype(np.float64), 5).tolist(),
            "min": round(float(np.min(arr)), 5),
            "max": round(float(np.max(arr)), 5),
        }

    return out


def _infer_len(data, keys) -> int:
    for k in keys:
        arr = np.asarray(data[k])
        if arr.ndim >= 1:
            return int(arr.shape[0])
    return 0
