"""Dataset source adapters.

The viewer browses more than one raw dataset layout. Each layout is handled by a
Source subclass exposing the same methods, so the API routes and the frontend are
format-agnostic:

    list_tasks()                    -> [task, ...]
    list_episodes(task)             -> [episode, ...]   (newest first)
    episode_detail(task, episode)   -> {instruction, status, cameras[], robot, ...}
    video_path(task, episode, cam)  -> local Path to a decoded MP4
    episode_stat(task, episode)     -> compact record for analytics
    overview() / stats()            -> dataset-wide aggregates

RaidenSource: <prefix>/<task>/<episode>/{metadata.json, cameras/*.svo2, robot_data.npz}
YamMcapSource: <prefix>/<task>/episode_<uuid>/output.mcap  (one Foxglove-protobuf MCAP)
"""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import cache, calib_overlay, fk, robot_data, s3, svo, yam


def _ee_traces(npz_path, calibration) -> dict | None:
    """End-effector trajectories (via FK) + per-scene-camera projection params, so
    the frontend can draw a future-EE trace on the video, synced to playback.

    EE points are computed in the ``left_arm_base`` frame (the calibration frame):
    the left arm's FK is already there; the right arm's FK is mapped through
    ``bimanual_transform.right_base_to_left_base``. Sampled at ~video rate to keep
    the payload small; the frontend indexes by playback time.
    """
    import numpy as np

    data = np.load(npz_path, allow_pickle=True)
    keys = set(data.files)
    if "timestamps" not in keys:
        return None
    ts = data["timestamps"].astype(np.float64)
    dur = (ts[-1] - ts[0]) / 1e9
    if dur <= 0:
        return None
    n = len(ts)

    # Sample ~30 Hz (video rate) rather than the ~98 Hz robot rate.
    target_hz = 30.0
    stride = max(1, int(round(n / dur / target_hz)))
    idx = np.arange(0, n, stride)
    times = ((ts[idx] - ts[0]) / 1e9).round(3).tolist()

    # Only the LEFT arm is emitted. FK is verified correct (matches raiden's own
    # i2rt.Kinematics grasp_site to ~8mm), but the RIGHT arm — whose FK is in its
    # own base frame and must be mapped via bimanual_transform then projected
    # through the scene-camera extrinsic — does not land on the gripper. Raiden's
    # own visualizer only plots the right EE in a 3D world view, never projected
    # onto the scene image, so there's no reference for that projection. Rather
    # than draw a wrong overlay, the right arm is gated off until validated.
    # (In practice YAM episodes are largely single-active-arm anyway.)
    arms = []
    for side, jkey in (("left", "follower_l_joint_pos"),):
        if jkey not in keys:
            continue
        ee = fk.ee_trajectory(data[jkey][idx])  # (M,3) in the arm's base frame
        arms.append({"side": side, "xyz": np.round(ee, 4).tolist()})

    if not arms:
        return None

    # Per-scene-camera projection: scaled K + extrinsic (R,t). Frontend applies
    # X_cam = R^T (X_base - t); uv = K X_cam. K is scaled to the decoded frame
    # size (left eye = full width here, so image_size already matches).
    cams = {}
    for cname, c in (calibration.get("cameras") or {}).items() if calibration else []:
        ext = c.get("extrinsics")
        intr = c.get("intrinsics", {})
        if not ext or "camera_matrix" not in intr:
            continue
        cams[cname] = {
            "K": intr["camera_matrix"],
            "R": ext["rotation_matrix"],
            "t": ext["translation_vector"],
            "image_size": intr.get("image_size"),
        }

    return {"time": times, "duration_s": round(dur, 3), "arms": arms, "cameras": cams}


class Source:
    def __init__(self, spec: dict):
        self.id = spec["id"]
        self.label = spec["label"]
        self.bucket = spec["bucket"]
        self.prefix = spec["prefix"].strip("/")

    # ---- browsing (shared) ----
    def list_tasks(self) -> list[str]:
        return s3.list_dirs(self.prefix, bucket=self.bucket)

    def list_episodes(self, task: str) -> list[str]:
        eps = s3.list_dirs(f"{self.prefix}/{task}", bucket=self.bucket)
        return sorted(eps, reverse=True)

    # ---- per-source ----
    def episode_detail(self, task: str, episode: str) -> dict:
        raise NotImplementedError

    def video_path(self, task: str, episode: str, camera: str, eye: str) -> Path:
        raise NotImplementedError

    def episode_stat(self, task: str, episode: str) -> dict | None:
        raise NotImplementedError

    # ---- aggregates (shared, built on the above) ----
    def overview(self) -> dict:
        tasks = self.list_tasks()
        per_task, total, stations = [], 0, set()
        for task in tasks:
            eps = self.list_episodes(task)
            total += len(eps)
            latest = eps[0] if eps else None
            for ep in eps:
                m = re.match(r"^([A-Za-z][\w-]*)_\d{4}-\d{2}-\d{2}T", ep)
                if m:
                    stations.add(m.group(1))
            per_task.append({"task": task, "episodes": len(eps), "latest": latest})
        per_task.sort(key=lambda t: t["episodes"], reverse=True)
        return {
            "source": self.id, "bucket": self.bucket, "prefix": self.prefix,
            "num_tasks": len(tasks), "num_episodes": total,
            "stations": sorted(stations), "tasks": per_task,
        }

    # Per-source cap on how many episodes analytics will sample. Reading stats is
    # cheap per episode but datasets can have tens of thousands of episodes, so we
    # sample (evenly per task) and report what was covered rather than stalling.
    STATS_MAX = 1200

    def _stat_pairs(self) -> tuple[list[tuple[str, str]], int]:
        """(task, episode) pairs to sample for stats, plus the true total count."""
        by_task = {t: self.list_episodes(t) for t in self.list_tasks()}
        total = sum(len(v) for v in by_task.values())
        if total <= self.STATS_MAX:
            return [(t, e) for t, eps in by_task.items() for e in eps], total
        # Evenly subsample within each task, proportional to its size.
        pairs = []
        for t, eps in by_task.items():
            share = max(1, round(self.STATS_MAX * len(eps) / total))
            step = max(1, len(eps) // share)
            pairs.extend((t, eps[i]) for i in range(0, len(eps), step))
        return pairs, total

    def stats(self) -> dict:
        pairs, total = self._stat_pairs()
        episodes = []
        with ThreadPoolExecutor(max_workers=32) as pool:
            for fut in [pool.submit(self._safe_stat, t, e) for t, e in pairs]:
                rec = fut.result()
                if rec:
                    episodes.append(rec)
        episodes.sort(key=lambda e: e.get("timestamp") or "")
        return {
            "num_episodes": len(episodes),
            "total_episodes": total,
            "sampled": len(pairs) < total,
        "episodes": episodes,
        }

    def _safe_stat(self, task, episode):
        try:
            return self.episode_stat(task, episode)
        except Exception:
            return None


class RaidenSource(Source):
    def _ep_prefix(self, task, episode):
        return f"{self.prefix}/{task}/{episode}"

    def episode_detail(self, task, episode):
        prefix = self._ep_prefix(task, episode)
        metadata = s3.get_json(f"{prefix}/metadata.json", bucket=self.bucket)

        calibration = None
        if s3.try_head(f"{prefix}/calibration_results.json", bucket=self.bucket):
            calibration = s3.get_json(f"{prefix}/calibration_results.json", bucket=self.bucket)

        cameras = []
        for obj in s3.list_files(f"{prefix}/cameras", bucket=self.bucket):
            if not obj.key.endswith(".svo2"):
                continue
            name = obj.key.rsplit("/", 1)[-1][: -len(".svo2")]
            cameras.append({
                "name": name,
                "size_mb": round(obj.size / 1024 / 1024, 1),
                "has_video": obj.size > 100_000,  # stub header files are ~1.5 KB
                "eyes": ["left", "right"],  # side-by-side stereo
            })
        cameras.sort(key=lambda c: c["name"])

        robot = None
        ee_traces = None
        robot_obj = s3.try_head(f"{prefix}/robot_data.npz", bucket=self.bucket)
        if robot_obj:
            npz = cache.get_or_create(
                f"{robot_obj.etag}_robot.npz",
                lambda dst: s3.download(robot_obj.key, dst, bucket=self.bucket),
            )
            robot = robot_data.summarize(npz)
            ee_traces = _ee_traces(npz, calibration)

        return {
            "source": self.id, "task": task, "episode": episode,
            "instruction": metadata.get("task_instruction") or metadata.get("task_name"),
            "status": metadata.get("status"),
            "metadata": metadata, "calibration": calibration,
            "cameras": cameras, "robot": robot, "annotations": [],
            "ee_traces": ee_traces,
        }

    def video_path(self, task, episode, camera, eye):
        key = f"{self._ep_prefix(task, episode)}/cameras/{camera}.svo2"
        obj = s3.try_head(key, bucket=self.bucket)
        if obj is None:
            raise FileNotFoundError(f"camera not found: {camera}")
        if obj.size < 100_000:
            raise ValueError(f"camera '{camera}' has no recorded video (stub file)")
        svo_local = cache.get_or_create(
            f"{obj.etag}_{camera}.svo2",
            lambda dst: s3.download(key, dst, bucket=self.bucket),
        )
        return cache.get_or_create(
            f"{obj.etag}_{camera}_{eye}.mp4",
            lambda dst: svo.decode_to_mp4(svo_local, dst, eye=eye),
        )

    def calib_overlay_path(self, task, episode, camera):
        """Render a calibration-check overlay (arm-base axis triads projected onto
        a still frame of `camera`). Only works for scene-type cameras that carry
        extrinsics in the base frame. Returns a cached PNG path, or raises."""
        prefix = self._ep_prefix(task, episode)
        calib_key = f"{prefix}/calibration_results.json"
        if s3.try_head(calib_key, bucket=self.bucket) is None:
            raise FileNotFoundError("no calibration for this episode")
        calib = s3.get_json(calib_key, bucket=self.bucket)
        cam_calib = (calib.get("cameras") or {}).get(camera)
        if not cam_calib or not cam_calib.get("extrinsics"):
            raise ValueError(f"camera '{camera}' has no scene extrinsics to visualize")

        mp4 = self.video_path(task, episode, camera, "left")
        obj = s3.head(f"{prefix}/cameras/{camera}.svo2", bucket=self.bucket)
        bt = (calib.get("bimanual_transform") or {}).get("right_base_to_left_base")

        def _produce(dst: Path):
            frame = dst.with_suffix(".frame.png")
            calib_overlay.extract_frame(mp4, frame, frame_index=0)
            ok = calib_overlay.draw_overlay(frame, cam_calib, bt, dst)
            frame.unlink(missing_ok=True)
            if not ok:
                raise ValueError("could not render calibration overlay")

        return cache.get_or_create(f"{obj.etag}_{camera}_calib.png", _produce)

    def episode_stat(self, task, episode):
        md = s3.get_json(f"{self._ep_prefix(task, episode)}/metadata.json", bucket=self.bucket)
        return {
            "task": task, "episode": episode,
            "duration_s": md.get("duration_s"), "robot_frames": md.get("robot_frames"),
            "robot_hz": md.get("robot_hz"), "num_cameras": len(md.get("cameras", [])),
            "status": md.get("status"), "station": md.get("station_name"),
            "timestamp": md.get("timestamp"),
        }


class YamMcapSource(Source):
    """One output.mcap per episode. Download the big MCAP once, extract all small
    artifacts (per-camera mp4 + robot/instruction json), cache them, drop the MCAP."""

    def _mcap_key(self, task, episode):
        return f"{self.prefix}/{task}/{episode}/output.mcap"

    def _mine(self, obj) -> dict:
        """Download the raw MCAP to a TEMP file, extract everything (all camera
        MP4s + robot/instruction JSON), cache those small artifacts keyed by ETag,
        then delete the big MCAP. Idempotent: skips work already cached.

        The raw MCAP is 200-880 MB and must never linger in the cache, so it's a
        temp file (not a cache.get_or_create artifact) that's removed in finally."""
        meta_json = cache.path_for(f"yam_{obj.etag}_meta.json")
        if meta_json.exists():
            ex = json.loads(meta_json.read_text())
            # Ensure the per-camera MP4s exist too (a partial prior run may have
            # written meta but not videos).
            if all(cache.path_for(f"yam_{obj.etag}_{c}.mp4").exists() for c in ex["cameras"]):
                return ex

        # Make room for the big MCAP + its extracted MP4s before downloading, so
        # a near-full cache can't wedge mid-download.
        cache.evict(headroom_gb=min(2.0, obj.size / 1024**3 * 1.5))
        tmp = cache.path_for(f"yam_{obj.etag}.mcap.tmp{os.getpid()}")
        try:
            s3.download(obj.key, tmp, bucket=self.bucket)
            probe = yam.probe(tmp)
            # Extract every camera to a cached MP4 in this single pass.
            for cam in probe["cameras"]:
                mp4 = cache.path_for(f"yam_{obj.etag}_{cam}.mp4")
                if not mp4.exists():
                    cache.get_or_create(
                        f"yam_{obj.etag}_{cam}.mp4",
                        lambda dst, _c=cam: yam.extract_camera_mp4(tmp, _c, dst),
                    )
            mr = yam.extract_meta_and_robot(tmp)
            ex = {"etag": obj.etag, "cameras": probe["cameras"], **mr}
            meta_json.write_text(json.dumps(ex))
            return ex
        finally:
            tmp.unlink(missing_ok=True)

    def _head(self, task, episode):
        obj = s3.try_head(self._mcap_key(task, episode), bucket=self.bucket)
        if obj is None:
            raise FileNotFoundError(f"no output.mcap for {task}/{episode}")
        return obj

    def episode_detail(self, task, episode):
        ex = self._mine(self._head(task, episode))
        cameras = [{"name": c, "has_video": True, "eyes": ["left"]} for c in ex["cameras"]]
        return {
            "source": self.id, "task": task, "episode": episode,
            "instruction": ex.get("instruction"), "status": None,
            "metadata": {"task_instruction": ex.get("instruction"),
                         "num_annotations": len(ex.get("annotations") or [])},
            "calibration": None, "cameras": cameras,
            "robot": ex.get("robot"), "annotations": ex.get("annotations") or [],
        }

    def video_path(self, task, episode, camera, eye):
        obj = self._head(task, episode)
        mp4 = cache.path_for(f"yam_{obj.etag}_{camera}.mp4")
        if not mp4.exists():
            # Mining extracts all cameras at once (single MCAP download), then
            # drops the MCAP — so this only downloads on a true cold miss.
            self._mine(obj)
        if not mp4.exists():
            raise FileNotFoundError(f"camera {camera!r} not found in {task}/{episode}")
        return mp4

    def episode_stat(self, task, episode):
        # Analytics must not trigger 200-880 MB downloads per episode. The episode
        # duration lives in the MCAP Statistics record in the summary section at
        # the END of the file, so a small tail range-read gets it cheaply. The MCAP
        # last-modified time is the closest available wallclock stamp (uuid ids
        # carry no timestamp).
        obj = s3.try_head(self._mcap_key(task, episode), bucket=self.bucket)
        if obj is None:
            return None
        base = {
            "task": task, "episode": episode, "num_cameras": None,
            "duration_s": None, "robot_frames": None, "robot_hz": None,
            "status": None, "station": None, "timestamp": obj.last_modified,
        }
        # Read a tail window; widen once if the summary didn't fit.
        for window in (2_000_000, 16_000_000):
            start = max(0, obj.size - window)
            tail = s3.get_range(obj.key, start, obj.size - 1, bucket=self.bucket)
            st = yam.stats_from_tail(tail, obj.size)
            if st.get("duration_s") is not None:
                base.update(st)
                break
        return base


_KINDS = {"raiden": RaidenSource, "yam": YamMcapSource}
_SOURCES: dict[str, Source] = {}


def get_sources(specs) -> dict[str, Source]:
    global _SOURCES
    if not _SOURCES:
        _SOURCES = {s["id"]: _KINDS[s["kind"]](s) for s in specs}
    return _SOURCES


def get_source(specs, sid: str) -> Source:
    src = get_sources(specs).get(sid)
    if src is None:
        raise KeyError(sid)
    return src
