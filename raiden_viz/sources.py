"""Dataset source adapters.

The viewer browses more than one raw dataset layout. Each layout is handled by a
Source subclass exposing the same methods, so the API routes and the frontend are
format-agnostic:

    list_tasks()                    -> [task, ...]
    list_episodes(task)             -> [episode, ...]   (oldest first)
    episode_detail(task, episode)   -> {instruction, status, cameras[], robot, ...}
    episode_facts(task)             -> {episode: {timestamp, status}}  (cheap)
    video_path(task, episode, cam)  -> local Path to a decoded MP4
    episode_stat(task, episode)     -> compact record for analytics
    overview() / stats()            -> dataset-wide aggregates

RaidenSource: <prefix>/<task>/<episode>/{metadata.json, cameras/*.svo2, robot_data.npz}
YamMcapSource: <prefix>/<task>/episode_<uuid>/output.mcap  (one Foxglove-protobuf MCAP)
"""

import hashlib
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import cache, calib_overlay, fk, lerobot, robot_data, s3, svo, yam

# In-flight/finished background stat scans, keyed by a per-source scan id. Held in
# memory (a scan is cheap to restart and its per-episode records are disk-cached).
_SCANS: dict[str, dict] = {}
_SCANS_GUARD = threading.Lock()


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
        self.spec = spec

    # ---- browsing (shared) ----
    def list_tasks(self) -> list[str]:
        return s3.list_dirs(self.prefix, bucket=self.bucket)

    def list_episodes(self, task: str) -> list[str]:
        # Oldest first, matching how the raiden recorder numbers episodes on disk
        # (0000, 0001, ... in capture order) so viewer index == recording index.
        # Both naming schemes sort chronologically as strings: zero-padded counters
        # (0000/0001) and station_<ISO-timestamp> dirs.
        eps = s3.list_dirs(f"{self.prefix}/{task}", bucket=self.bucket)
        return sorted(eps)

    # ---- per-source ----
    def episode_detail(self, task: str, episode: str) -> dict:
        raise NotImplementedError

    def episode_facts(self, task: str) -> dict:
        """Cheap per-episode facts for the browse list: {episode: {timestamp, status}}.

        Must stay listing-cheap (no per-episode GETs unless they're small): this is
        called on every task switch to label the sidebar. Sources with nothing cheap
        to offer return {} and the sidebar just shows indices."""
        return {}

    def episode_time(self, task: str, episode: str) -> str | None:
        """Capture wallclock (ISO8601) of ONE episode, as cheaply as the format
        allows — overview() calls it twice per task (first + last) to date the task.
        None where the format carries no timestamp."""
        return None

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
            latest = eps[-1] if eps else None  # lists are oldest-first
            for ep in eps:
                m = re.match(r"^([A-Za-z][\w-]*)_\d{4}-\d{2}-\d{2}T", ep)
                if m:
                    stations.add(m.group(1))
            per_task.append({"task": task, "episodes": len(eps), "latest": latest,
                             "_eps": eps})
        per_task.sort(key=lambda t: t["episodes"], reverse=True)
        self._add_collection_span(per_task)
        return {
            "source": self.id, "bucket": self.bucket, "prefix": self.prefix,
            "num_tasks": len(tasks), "num_episodes": total,
            "stations": sorted(stations), "tasks": per_task,
        }

    # How many episodes to try inward from each end of a task before giving up on
    # dating it. Episode lists are chronological, so the first/last episode normally
    # answers it in one read — but an episode can be missing its metadata (an aborted
    # recording leaves the directory behind), which would otherwise blank the date.
    SPAN_PROBE_DEPTH = 3

    def _add_collection_span(self, per_task: list[dict]) -> None:
        """Stamp each task row with when it was collected: ``collected_start`` /
        ``collected_end`` (ISO8601, None where the format has no timestamps).

        Episode lists are chronological, so a task's span is its first and last
        episode — a couple of cheap reads per task instead of one per episode. Reads
        run in parallel across tasks; a source without timestamps leaves every value
        None (episode_time returns None) and the frontend hides the sort.
        """
        jobs = []
        for row in per_task:
            eps = row.pop("_eps")
            d = self.SPAN_PROBE_DEPTH
            for key, candidates in (("collected_start", eps[:d]),
                                    ("collected_end", eps[::-1][:d])):
                row[key] = None
                if candidates:
                    jobs.append((row, key, candidates))
        if not jobs:
            return
        with ThreadPoolExecutor(max_workers=32) as pool:
            times = pool.map(lambda j: self._first_time(j[2], j[0]["task"]), jobs)
        for (row, key, _), ts in zip(jobs, times):
            row[key] = ts

    def _first_time(self, episodes: list[str], task: str) -> str | None:
        """The first readable timestamp among ``episodes``, in order (they're ordered
        outermost-first, so this walks inward from one end of the task)."""
        for ep in episodes:
            ts = self._safe_episode_time(task, ep)
            if ts:
                return ts
        return None

    def _safe_episode_time(self, task, episode) -> str | None:
        """episode_time that never breaks the overview: one unreadable episode just
        leaves that end of the span unknown."""
        try:
            return self.episode_time(task, episode)
        except Exception:
            return None

    # Per-source cap on how many episodes a *quick* (non-full) stats pass samples.
    # Reading one stat is cheap, but datasets can have tens of thousands of
    # episodes, so the default charts pass samples (evenly per task) and reports
    # coverage. A filter needs every episode, so it requests a full scan instead.
    STATS_MAX = 1200

    def _stat_pairs(self, full: bool) -> tuple[list[tuple[str, str]], int]:
        """(task, episode) pairs to read for stats, plus the true total count.
        ``full`` reads every episode; otherwise sample down to STATS_MAX."""
        by_task = {t: self.list_episodes(t) for t in self.list_tasks()}
        total = sum(len(v) for v in by_task.values())
        if full or total <= self.STATS_MAX:
            return [(t, e) for t, eps in by_task.items() for e in eps], total
        # Evenly subsample within each task, proportional to its size.
        pairs = []
        for t, eps in by_task.items():
            share = max(1, round(self.STATS_MAX * len(eps) / total))
            step = max(1, len(eps) // share)
            pairs.extend((t, eps[i]) for i in range(0, len(eps), step))
        return pairs, total

    def stats(self, full: bool = False) -> dict:
        """Synchronous stats pass (used for the charts). For a full scan of a huge
        source prefer scan_start()/scan_snapshot(), which stream progress."""
        pairs, total = self._stat_pairs(full)
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
            "scanned": len(episodes),
            "sampled": len(pairs) < total,
            "episodes": episodes,
        }

    # ---- cached per-episode stats + background full scan (for filtering) ----

    def _stat_cache_key(self, task, episode) -> str | None:
        """Cache filename for one episode's stat record, keyed by the cheap head's
        etag so a re-uploaded episode invalidates. None if the object is missing."""
        obj = self._stat_head(task, episode)
        if obj is None:
            return None
        return f"stat_{self.id}_{obj.etag}.json"

    def _stat_head(self, task, episode):
        """The small object whose etag identifies this episode's content (the
        metadata.json for raiden, the MCAP for yam). Overridden per source."""
        raise NotImplementedError

    def _safe_stat(self, task, episode):
        """episode_stat with disk memoization: stat records are deterministic per
        content etag, so a full scan is paid once and every later filter is instant."""
        key = self._stat_cache_key(task, episode)
        if key:
            hit = cache.get_json(key)
            if hit is not None:
                return hit
        try:
            rec = self.episode_stat(task, episode)
        except Exception:
            return None
        if rec and key:
            cache.put_json(key, rec)
        return rec

    def _scan_id(self) -> str:
        return hashlib.sha1(f"{self.id}:{self.bucket}:{self.prefix}".encode()).hexdigest()[:12]

    def scan_start(self) -> dict:
        """Begin (or resume) a background full scan of every episode's stats.
        Returns an immediate snapshot; poll scan_snapshot() for progress. Idempotent:
        a scan already running/finished for this source is reused."""
        sid = self._scan_id()
        with _SCANS_GUARD:
            st = _SCANS.get(sid)
            if st and (st["running"] or st["done"]):
                return self._snapshot(st)
            pairs, total = self._stat_pairs(full=True)
            st = {"running": True, "done": False, "total": total,
                  "episodes": [], "error": None, "lock": threading.Lock()}
            _SCANS[sid] = st
        t = threading.Thread(target=self._run_scan, args=(sid, pairs, st), daemon=True)
        t.start()
        return self._snapshot(st)

    def _run_scan(self, sid, pairs, st):
        err = None
        try:
            with ThreadPoolExecutor(max_workers=32) as pool:
                for fut in [pool.submit(self._safe_stat, t, e) for t, e in pairs]:
                    rec = fut.result()
                    if rec:
                        with st["lock"]:
                            st["episodes"].append(rec)
        except Exception as e:  # never leave a scan stuck "running"
            err = str(e)
        finally:
            # Publish the terminal flags UNDER the same lock that guards episodes,
            # so a poll that observes done=True is guaranteed to also see the final
            # (complete) episode list — never a torn read that drops the last records.
            with st["lock"]:
                st["error"] = err
                st["running"] = False
                st["done"] = True

    def scan_snapshot(self) -> dict | None:
        """Current progress of an in-flight/finished scan, or None if none started."""
        with _SCANS_GUARD:
            st = _SCANS.get(self._scan_id())
        return self._snapshot(st) if st else None

    def _snapshot(self, st) -> dict:
        # Read the flags and the episode list together under the lock so they can't
        # disagree (see _run_scan's terminal publish).
        with st["lock"]:
            eps = list(st["episodes"])
            running, done, error = st["running"], st["done"], st["error"]
        return {
            "running": running, "done": done,
            "total_episodes": st["total"], "scanned": len(eps),
            "error": error, "episodes": eps,
        }


class RaidenSource(Source):
    def _ep_prefix(self, task, episode):
        return f"{self.prefix}/{task}/{episode}"

    def episode_time(self, task: str, episode: str) -> str | None:
        # The recorder's own capture stamp, from the ~1 KB metadata.json (via the
        # etag-keyed stat cache, so the overview's reads are paid once).
        rec = self._safe_stat(task, episode)
        return rec.get("timestamp") if rec else None

    def episode_facts(self, task: str) -> dict:
        # metadata.json is ~1 KB, so fetching one per episode in parallel is cheap
        # enough to label the whole browse list (largest raiden task is ~270 eps).
        # Reuses the etag-keyed stat cache, so repeat visits are served from disk.
        eps = self.list_episodes(task)
        out = {}
        with ThreadPoolExecutor(max_workers=32) as pool:
            recs = pool.map(lambda e: (e, self._safe_stat(task, e)), eps)
        for ep, rec in recs:
            if rec:
                out[ep] = {"timestamp": rec.get("timestamp"), "status": rec.get("status")}
        return out

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
        # LOCAL ONLY. This is the transcode's input, not its output: a
        # byte-for-byte copy of an object that is already durable in the datasets
        # bucket. Publishing it duplicated source data into the derived tier (~2.8 GB
        # there, about a third of it) to buy back a re-download in the one case where
        # the decoded MP4 expired but its source had not. The MP4 below IS published:
        # that one costs minutes of ffmpeg to reproduce. Same split the yam adapter
        # already makes with its MCAP, which it keeps as a temp file.
        svo_local = cache.get_or_create(
            f"{obj.etag}_{camera}.svo2",
            lambda dst: s3.download(key, dst, bucket=self.bucket),
            remote=False,
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

    def _stat_head(self, task, episode):
        return s3.try_head(f"{self._ep_prefix(task, episode)}/metadata.json", bucket=self.bucket)

    def episode_stat(self, task, episode):
        md = s3.get_json(f"{self._ep_prefix(task, episode)}/metadata.json", bucket=self.bucket)
        return {
            "task": task, "episode": episode,
            "duration_s": md.get("duration_s"), "robot_frames": md.get("robot_frames"),
            "robot_hz": md.get("robot_hz"), "num_cameras": len(md.get("cameras", [])),
            "status": md.get("status"), "station": md.get("station_name"),
            "teacher": md.get("teacher_name"), "control": md.get("control"),
            "timestamp": md.get("timestamp"),
            "has_annotations": None,  # raiden has no subtask annotations
        }


class YamMcapSource(Source):
    """One MCAP per episode. Download the big MCAP once, extract all small
    artifacts (per-camera mp4 + robot/instruction json), cache them, drop the MCAP.

    The per-episode MCAP basename varies by dataset (``output.mcap`` for the
    russet/yam_raw layout, ``episode.mcap`` for ABC-130k), set via spec['mcap_name']."""

    def _mcap_key(self, task, episode):
        name = self.spec.get("mcap_name", "output.mcap")
        return f"{self.prefix}/{task}/{episode}/{name}"

    # Skip the browse-list timestamp listing for tasks bigger than this: the labels
    # are a browsing nicety, not worth paging tens of thousands of keys (ABC-130k's
    # largest task has ~11k episodes) on every task switch.
    FACTS_MAX_EPISODES = 2000

    def episode_time(self, task: str, episode: str) -> str | None:
        # A HEAD on the MCAP: no download, and its LastModified is the closest
        # available wallclock stamp (uuid episode ids carry no time).
        obj = s3.try_head(self._mcap_key(task, episode), bucket=self.bucket)
        return obj.last_modified if obj else None

    def episode_facts(self, task: str) -> dict:
        # uuid-named episodes carry no timestamp, and the MCAP is far too big to
        # open here — but a single recursive listing yields every episode's MCAP
        # LastModified, the closest available wallclock stamp. No status: the MCAP
        # format has no success/failure field.
        # Size-check on the cheaper delimiter listing first, so an oversized task
        # bails without paying for the recursive walk.
        if len(self.list_episodes(task)) > self.FACTS_MAX_EPISODES:
            return {}
        name = self.spec.get("mcap_name", "output.mcap")
        out = {}
        for obj in s3.list_keys(f"{self.prefix}/{task}", bucket=self.bucket, suffix=name):
            ep = obj.key[len(f"{self.prefix}/{task}/"):].rsplit("/", 1)[0]
            out[ep] = {"timestamp": obj.last_modified, "status": None}
        return out

    def _mine(self, obj, task=None, episode=None) -> dict:
        """Download the raw MCAP to a TEMP file, extract everything (all camera
        MP4s + robot/instruction JSON), cache those small artifacts keyed by ETag,
        then delete the big MCAP. Idempotent: skips work already cached.

        The raw MCAP is 200-880 MB and must never linger in the cache, so it's a
        temp file (not a cache.get_or_create artifact) that's removed in finally.

        Subtask annotations may live in a sibling ``annotation.mcap`` (ABC-130k,
        annotated episodes only); when present it's merged into ``annotations``."""
        meta_json = cache.path_for(f"yam_{obj.etag}_meta.json")
        if meta_json.exists():
            ex = json.loads(meta_json.read_text())
            # Ensure the per-camera MP4s exist too (a partial prior run may have
            # written meta but not videos).
            if all(cache.exists(f"yam_{obj.etag}_{c}.mp4") for c in ex["cameras"]):
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
                if not cache.exists(f"yam_{obj.etag}_{cam}.mp4"):
                    cache.get_or_create(
                        f"yam_{obj.etag}_{cam}.mp4",
                        lambda dst, _c=cam: yam.extract_camera_mp4(tmp, _c, dst),
                    )
            mr = yam.extract_meta_and_robot(tmp)
            # If a sibling annotation.mcap exists and the episode.mcap had no inline
            # subtask labels, pull them from there (relative to the episode start).
            if not mr.get("annotations") and task is not None:
                sib = self._annotation_key(task, episode)
                sib_obj = s3.try_head(sib, bucket=self.bucket)
                if sib_obj is not None:
                    atmp = cache.path_for(f"yam_{obj.etag}_ann.mcap.tmp{os.getpid()}")
                    try:
                        s3.download(sib, atmp, bucket=self.bucket)
                        mr["annotations"] = yam.read_annotation_mcap(atmp, mr.get("start_abs", 0.0))
                    finally:
                        atmp.unlink(missing_ok=True)

            ex = {"etag": obj.etag, "cameras": probe["cameras"], **mr}
            meta_json.write_text(json.dumps(ex))
            return ex
        finally:
            tmp.unlink(missing_ok=True)

    def _annotation_key(self, task, episode):
        """Sibling annotation.mcap path next to the episode's MCAP."""
        ep_key = self._mcap_key(task, episode)
        return ep_key.rsplit("/", 1)[0] + "/annotation.mcap"

    def _head(self, task, episode):
        obj = s3.try_head(self._mcap_key(task, episode), bucket=self.bucket)
        if obj is None:
            raise FileNotFoundError(f"no output.mcap for {task}/{episode}")
        return obj

    def _sidecar(self, episode) -> dict:
        """Fetch + split the per-episode metadata sidecar, if this source has one.
        Keyed by the episode uuid (``episode_<uuid>`` -> ``<prefix>/<uuid>.json``).
        Small (~3 KB) and read live per view, so it stays out of the MCAP cache."""
        prefix = self.spec.get("metadata_prefix")
        if not prefix:
            return {}
        uuid = episode[len("episode_"):] if episode.startswith("episode_") else episode
        raw = s3.try_get_json(f"{prefix}/{uuid}.json", bucket=self.bucket)
        return yam.parse_sidecar(raw) if raw else {}

    def episode_detail(self, task, episode):
        ex = self._mine(self._head(task, episode), task, episode)
        cameras = [{"name": c, "has_video": True, "eyes": ["left"]} for c in ex["cameras"]]
        sidecar = self._sidecar(episode)
        sm = sidecar.get("sidecar_meta") or {}
        metadata = {"task_instruction": ex.get("instruction"),
                    "num_annotations": len(ex.get("annotations") or [])}
        if sm.get("duration_s") is not None:
            metadata["duration_s"] = round(sm["duration_s"], 2)
        if sm.get("env_loop_frequency") is not None:
            metadata["control_hz"] = sm["env_loop_frequency"]
        if sm.get("arm_type") is not None:
            metadata["arm_type"] = sm["arm_type"]
        return {
            "source": self.id, "task": task, "episode": episode,
            "instruction": ex.get("instruction"), "status": None,
            "metadata": metadata,
            "calibration": sidecar.get("calibration"), "cameras": cameras,
            "robot": ex.get("robot"), "annotations": ex.get("annotations") or [],
        }

    def video_path(self, task, episode, camera, eye):
        obj = self._head(task, episode)
        mp4 = cache.path_for(f"yam_{obj.etag}_{camera}.mp4")
        # cache.exists() consults the derived S3 tier and pulls the clip down if it
        # is there. A bare local probe would report a miss for something already
        # decoded, and _mine answers a miss by re-downloading the whole 200-880 MB
        # MCAP — the single most expensive thing this app can do.
        if not cache.exists(f"yam_{obj.etag}_{camera}.mp4"):
            # Mining extracts all cameras at once (single MCAP download), then
            # drops the MCAP — so this only downloads on a true cold miss.
            self._mine(obj, task, episode)
        if not cache.exists(f"yam_{obj.etag}_{camera}.mp4"):
            raise FileNotFoundError(f"camera {camera!r} not found in {task}/{episode}")
        return mp4

    def _stat_head(self, task, episode):
        return s3.try_head(self._mcap_key(task, episode), bucket=self.bucket)

    def episode_stat(self, task, episode):
        # Analytics/filtering must not trigger 200-880 MB downloads per episode. The
        # summary section at the END of the MCAP holds duration + per-channel message
        # counts, so a small tail range-read yields every cheap facet (duration,
        # cameras, annotations, robot frames). The MCAP last-modified time is the
        # closest available wallclock stamp (uuid ids carry no timestamp).
        obj = s3.try_head(self._mcap_key(task, episode), bucket=self.bucket)
        if obj is None:
            return None
        base = {
            "task": task, "episode": episode, "num_cameras": None,
            "duration_s": None, "robot_frames": None, "robot_hz": None,
            "status": None, "station": None, "teacher": None, "control": None,
            "timestamp": obj.last_modified,
            "has_annotations": None, "n_annotations": None,
        }
        # Read a tail window; widen once if the summary didn't fit. stats_from_tail
        # returns {} only when the window didn't reach the summary, so break (and
        # merge every parsed facet) as soon as it returns anything — not solely on
        # duration, which would discard cameras/annotations from a summary that
        # happens to lack a Statistics record.
        for window in (4_000_000, 16_000_000):
            start = max(0, obj.size - window)
            tail = s3.get_range(obj.key, start, obj.size - 1, bucket=self.bucket)
            st = yam.stats_from_tail(tail, obj.size)
            if st:
                base.update(st)
                break
        return base


class LeRobotSource(Source):
    """LeRobot v3.0 datasets: ``<prefix>/<task>/{meta,data,videos}``.

    Each task folder is a self-contained LeRobot dataset. Unlike the raiden/yam
    layouts (one folder or one MCAP per episode), many episodes may be PACKED into
    shared parquet/mp4 files; ``meta/episodes`` maps each episode_index to its data
    file, its per-camera video file, and the ``[from_ts, to_ts]`` slice within them.

    Timeseries (observation.state/action, subtask labels) live in the packed data
    parquet; video is AV1 and is transcoded to browser-safe H.264 on demand,
    trimmed to the episode's window. Per-task metadata (info/tasks/episodes) is
    small and read once, then memoized in memory."""

    def __init__(self, spec: dict):
        super().__init__(spec)
        self._meta_cache: dict[str, dict] = {}
        self._meta_lock = threading.Lock()

    # LeRobot indexes episodes by integer; expose them as zero-padded names so the
    # existing string-keyed API routes/frontend work unchanged.
    def _ep_name(self, idx: int) -> str:
        return f"episode_{int(idx):06d}"

    def _ep_index(self, name: str) -> int:
        return int(name.rsplit("_", 1)[-1])

    def _task_root(self, task: str) -> str:
        return f"{self.prefix}/{task}"

    def _load_meta(self, task: str) -> dict:
        root = self._task_root(task)
        info = lerobot.parse_info(s3.get_json(f"{root}/meta/info.json", bucket=self.bucket))
        tasks_tbl = lerobot.read_table(s3.get_bytes(f"{root}/meta/tasks.parquet", bucket=self.bucket))
        task_map = lerobot.parse_tasks(tasks_tbl)
        # meta/episodes may itself be chunked across several parquet files.
        episodes: dict[int, dict] = {}
        for obj in sorted(s3.list_keys(f"{root}/meta/episodes", bucket=self.bucket, suffix=".parquet"),
                          key=lambda o: o.key):
            tbl = lerobot.read_table(s3.get_bytes(obj.key, bucket=self.bucket))
            episodes.update(lerobot.parse_episodes(tbl, info["video_keys"]))
        return {"info": info, "tasks": task_map, "episodes": episodes}

    def _meta(self, task: str) -> dict:
        # Lock across the load so 32 concurrent scan workers don't all fetch the
        # same task's meta; datasets are static, so a plain per-task memo is enough.
        with self._meta_lock:
            m = self._meta_cache.get(task)
            if m is None:
                m = self._load_meta(task)
                self._meta_cache[task] = m
            return m

    def list_episodes(self, task: str) -> list[str]:
        return [self._ep_name(i) for i in sorted(self._meta(task)["episodes"])]

    def _row(self, task: str, episode: str) -> tuple[dict, dict]:
        meta = self._meta(task)
        row = meta["episodes"].get(self._ep_index(episode))
        if row is None:
            raise FileNotFoundError(f"no such episode: {task}/{episode}")
        return meta, row

    def _data_table(self, task: str, meta: dict, row: dict):
        """Load the (possibly multi-episode) data parquet for an episode and filter
        it down to just that episode's rows."""
        key = meta["info"]["data_path"].format(
            chunk_index=row["data_chunk"], file_index=row["data_file"])
        tbl = lerobot.read_table(s3.get_bytes(f"{self._task_root(task)}/{key}", bucket=self.bucket))
        return lerobot.filter_episode(tbl, row["episode_index"])

    def episode_detail(self, task: str, episode: str) -> dict:
        meta, row = self._row(task, episode)
        info = meta["info"]
        tbl = self._data_table(task, meta, row)
        robot = lerobot.build_robot(tbl, info)
        annotations = lerobot.subtasks_to_annotations(tbl)
        instruction = lerobot.instruction_for(tbl, meta["tasks"], row)
        cameras = [{"name": c, "has_video": True, "eyes": ["left"]} for c in info["cameras"]]
        metadata = {"num_annotations": len(annotations)}
        if info.get("robot_type"):
            metadata["arm_type"] = info["robot_type"]
        if info.get("fps"):
            metadata["control_hz"] = info["fps"]
        return {
            "source": self.id, "task": task, "episode": episode,
            "instruction": instruction, "status": None,
            "metadata": metadata, "calibration": None,
            "cameras": cameras, "robot": robot,
            "annotations": annotations,
        }

    def video_path(self, task: str, episode: str, camera: str, eye: str) -> Path:
        meta, row = self._row(task, episode)
        info = meta["info"]
        vid = row.get("videos", {}).get(camera)
        full_key = info["video_keys"].get(camera)
        if vid is None or full_key is None:
            raise FileNotFoundError(f"camera {camera!r} not found in {task}/{episode}")
        vkey = info["video_path"].format(
            video_key=full_key, chunk_index=vid["chunk"], file_index=vid["file"])
        s3key = f"{self._task_root(task)}/{vkey}"
        obj = s3.try_head(s3key, bucket=self.bucket)
        if obj is None:
            raise FileNotFoundError(f"video not found: {s3key}")
        from_ts, to_ts = vid["from_ts"], vid["to_ts"]
        win = f"{from_ts:.3f}-{'end' if to_ts is None else f'{to_ts:.3f}'}"

        def _produce(dst: Path):
            # The shared source mp4 is AV1 (not browser-playable) and up to a few
            # hundred MB. Pull it to a temp file, transcode this episode's window to
            # H.264, and drop the raw — only the trimmed clip is cached.
            cache.evict(headroom_gb=min(2.0, obj.size / 1024**3 * 1.5))
            tmp = cache.path_for(f"lerobot_{obj.etag}_{camera}.src.mp4.tmp{os.getpid()}")
            try:
                s3.download(s3key, tmp, bucket=self.bucket)
                lerobot.transcode(tmp, dst, from_ts, to_ts, info.get("fps"))
            finally:
                tmp.unlink(missing_ok=True)

        return cache.get_or_create(f"lerobot_{obj.etag}_{camera}_{win}.mp4", _produce)

    def _safe_stat(self, task, episode):
        # LeRobot per-episode stats come entirely from the in-memory-cached task
        # meta, so skip the base class's etag-keyed disk cache (one etag per task
        # would collide across episodes) — recomputing is already essentially free.
        try:
            return self.episode_stat(task, episode)
        except Exception:
            return None

    def episode_stat(self, task: str, episode: str) -> dict | None:
        try:
            meta, row = self._row(task, episode)
        except FileNotFoundError:
            return None
        info = meta["info"]
        # Duration from any camera's video window; else from frame count / fps.
        dur = next((round(v["to_ts"] - v.get("from_ts", 0.0), 3)
                    for v in row.get("videos", {}).values() if v.get("to_ts") is not None), None)
        if dur is None and row.get("length") and info.get("fps"):
            dur = round(row["length"] / info["fps"], 3)
        return {
            "task": task, "episode": episode,
            "duration_s": dur, "robot_frames": row.get("length"),
            "robot_hz": info.get("fps"), "num_cameras": len(info["cameras"]),
            "status": None, "station": None, "teacher": None, "control": None,
            "timestamp": None,
            "has_annotations": None,  # would require reading each episode's data parquet
        }


class LeRobotSingleRootSource(LeRobotSource):
    """A LeRobot v3.0 dataset that is a SINGLE dataset at the prefix root
    (``<prefix>/{meta,data,videos}``), rather than one dataset per ``<prefix>/<task>``
    subfolder (which the base LeRobotSource assumes).

    Here the dataset's own ``task_index`` / per-episode ``tasks`` label is what we
    surface as the viewer's "tasks": all meta is loaded ONCE from the root, and its
    tens of thousands of episodes are grouped by task label so the UI browses
    task -> episode as usual. All packing/slicing/transcode logic is inherited —
    only task discovery and path rooting change (every path ignores the task arg
    and points at the single root)."""

    # ---- everything roots at the prefix, regardless of the task arg ----
    def _task_root(self, task: str) -> str:
        return self.prefix

    def _load_all(self) -> dict:
        # Reuse the base loader against the root (its _task_root("") == prefix),
        # then index episodes by task label. Cached under a fixed key.
        meta = super()._load_meta("")
        by_task: dict[str, list[int]] = {}
        for idx, row in meta["episodes"].items():
            tasks = row.get("tasks")
            label = tasks[0] if isinstance(tasks, (list, tuple)) and tasks else (tasks or "unlabeled")
            by_task.setdefault(str(label), []).append(idx)
        for v in by_task.values():
            v.sort()
        meta["by_task"] = by_task
        return meta

    def _meta(self, task: str = "") -> dict:
        # Single shared meta for the whole dataset (task arg is irrelevant to the
        # load); memoized under one key so the ~large episodes parquet is read once.
        with self._meta_lock:
            m = self._meta_cache.get("__root__")
            if m is None:
                m = self._load_all()
                self._meta_cache["__root__"] = m
            return m

    def list_tasks(self) -> list[str]:
        return sorted(self._meta()["by_task"])

    def list_episodes(self, task: str) -> list[str]:
        idxs = self._meta()["by_task"].get(task, [])
        # oldest-first (ascending episode_index) to match the other sources' ordering
        return [self._ep_name(i) for i in sorted(idxs)]

    def _row(self, task: str, episode: str) -> tuple[dict, dict]:
        meta = self._meta()
        row = meta["episodes"].get(self._ep_index(episode))
        if row is None:
            raise FileNotFoundError(f"no such episode: {episode}")
        return meta, row


_KINDS = {"raiden": RaidenSource, "yam": YamMcapSource, "lerobot": LeRobotSource,
          "lerobot_single": LeRobotSingleRootSource}
_SOURCES: dict[str, Source] = {}


def _accessible(src: Source) -> bool:
    """Can this host actually read the source? Used to auto-hide sources whose
    bucket needs creds this host lacks (e.g. the vendor bucket on the EC2 box)."""
    try:
        s3.list_dirs(src.prefix, bucket=src.bucket)
        return True
    except Exception:
        return False


def get_sources(specs) -> dict[str, Source]:
    global _SOURCES
    if not _SOURCES:
        out = {}
        for s in specs:
            src = _KINDS[s["kind"]](s)
            # Sources flagged requires_access only register where readable.
            if s.get("requires_access") and not _accessible(src):
                continue
            out[s["id"]] = src
        _SOURCES = out
    return _SOURCES


def get_source(specs, sid: str) -> Source:
    src = get_sources(specs).get(sid)
    if src is None:
        raise KeyError(sid)
    return src
