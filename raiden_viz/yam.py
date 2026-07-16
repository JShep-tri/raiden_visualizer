"""Decode YAM-family episode MCAPs into the viewer's common shapes.

Each episode is a single MCAP (200-900 MB) using Foxglove protobuf schemas. Two
topic conventions are supported (both are YAM two-arm stations):

  original ("yam_raw"/russet)          ABC-130k
  ------------------------------------ -----------------------------------
  /<cam>/image-raw  CompressedVideo    /<cam>            CompressedVideo
  /<arm>-proprio    RobotState         /<arm>-state      RobotState
  /<eef>-proprio    GripperState       /<ee>-state       GripperState
  /instruction      Instructions       /instruction      Instructions
  /subtask-annotation  Annotation      (none)

Video is H.264 or H.265 (read from the CompressedVideo ``format`` field). The
MCAP is downloaded once; we extract every camera to MP4 and the robot
trajectories to a compact JSON, cache those small artifacts, and delete the big
MCAP. Frames come from the Foxglove protobuf ``data`` field (field 3); we
concatenate them and hand the elementary stream to ffmpeg (stream-copy), no
stereo crop.
"""

import json
import subprocess
from pathlib import Path

import numpy as np
from mcap.reader import make_reader

# A camera topic is one whose schema is foxglove.CompressedVideo. Rather than
# match on name suffixes (which differ across conventions), we detect cameras by
# schema and derive a clean name from the topic.
_CAMERA_SCHEMA = "foxglove.CompressedVideo"


def is_camera_topic(topic: str, schema_name: str | None) -> bool:
    return schema_name == _CAMERA_SCHEMA


def camera_name(topic: str) -> str:
    """/top-left-camera/image-raw -> top_left_camera ; /top-left-camera -> top_left_camera"""
    t = topic.strip("/")
    if t.endswith("/image-raw"):
        t = t[: -len("/image-raw")]
    return t.replace("-", "_").replace("/", "_")


def is_proprio_topic(topic: str) -> bool:
    """Actual robot state, both conventions: '<arm>-proprio' or '<x>-state'.
    Excludes '-leader' (teleop command) and '-action' (commanded) channels."""
    if topic.endswith("-leader") or topic.endswith("-action"):
        return False
    return topic.endswith("-proprio") or topic.endswith("-state")


def _read_varint(b: bytes, o: int) -> tuple[int, int]:
    r = s = 0
    while True:
        x = b[o]; o += 1; r |= (x & 0x7F) << s
        if not x & 0x80:
            return r, o
        s += 7


def _cv_fields(buf: bytes) -> tuple[bytes, str | None]:
    """Pull (data bytes=field 3, format string=field 4) from a
    foxglove.CompressedVideo message. Parses just enough protobuf wire format."""
    o = 0
    data = b""
    fmt = None
    while o < len(buf):
        tag, o = _read_varint(buf, o)
        fn, wt = tag >> 3, tag & 7
        if wt == 2:
            ln, o = _read_varint(buf, o)
            val = buf[o:o + ln]; o += ln
            if fn == 3:
                data = val
            elif fn == 4:
                fmt = val.decode("utf8", "replace")
        elif wt == 0:
            _, o = _read_varint(buf, o)
        elif wt == 5:
            o += 4
        elif wt == 1:
            o += 8
        else:
            break
    return data, fmt


def _cv_h264_payload(buf: bytes) -> bytes:
    return _cv_fields(buf)[0]


def probe(mcap_path: Path) -> dict:
    """List channels + message counts without decoding payloads. Cameras are
    detected by their foxglove.CompressedVideo schema (topic naming varies)."""
    with open(mcap_path, "rb") as f:
        summary = make_reader(f).get_summary()
        cams, topics = [], {}
        counts = summary.statistics.channel_message_counts if summary.statistics else {}
        for cid, ch in summary.channels.items():
            topics[ch.topic] = counts.get(cid, 0)
            sch = summary.schemas.get(ch.schema_id)
            if is_camera_topic(ch.topic, sch.name if sch else None):
                cams.append(camera_name(ch.topic))
    return {"cameras": sorted(cams), "topics": topics}


def stats_from_tail(tail: bytes, total_size: int) -> dict:
    """Parse duration + per-topic counts from just the MCAP tail (summary section),
    so analytics can cover every episode without downloading the whole file.

    MCAP layout: ... [Summary section] [SummaryOffset section] [Footer(op=0x02,len=20:
    summary_start(u64), summary_offset_start(u64), crc(u32))] [magic(8)]. The Statistics
    record (op=0x0B) in the summary holds message_start_time / message_end_time (ns)."""
    import struct

    if len(tail) < 8 + 29 or tail[-8:] != b"\x89MCAP0\r\n":
        return {}
    fo = len(tail) - 8 - 29  # footer op byte offset within tail
    if tail[fo] != 0x02:
        return {}
    summary_start, _soff = struct.unpack_from("<QQ", tail, fo + 9)
    tail_start = total_size - len(tail)
    p = summary_start - tail_start
    if p < 0:
        return {}  # tail window didn't reach the summary; caller can widen it

    end = fo
    start_ns = end_ns = None
    proprio_arm_count = 0
    while p < end - 9:
        op = tail[p]
        (rlen,) = struct.unpack_from("<Q", tail, p + 1)
        p += 9
        rec = tail[p:p + rlen]
        p += rlen
        if op == 0x0B and len(rec) >= 42:  # Statistics record
            # message_count(u64), schema_count(u16), channel_count(u32),
            # attachment_count(u32), metadata_count(u32), chunk_count(u32),
            # message_start_time(u64), message_end_time(u64), ...
            o = 8 + 2 + 4 + 4 + 4 + 4
            start_ns, end_ns = struct.unpack_from("<QQ", rec, o)
    # message_start_time can legitimately be 0 (YAM episodes start at t=0), so
    # guard on end_ns rather than truthiness of start.
    dur = round((end_ns - start_ns) / 1e9, 3) if end_ns is not None else None
    return {"duration_s": dur}


# Map the CompressedVideo `format` string to ffmpeg's raw-elementary demuxer.
_FMT_TO_FFMPEG = {"h264": "h264", "h265": "hevc", "hevc": "hevc"}


def extract_camera_mp4(mcap_path: Path, camera: str, out_mp4: Path, fps: int = 30) -> dict:
    """Concatenate one camera's compressed frames and mux to MP4."""
    # Match the camera topic by its CompressedVideo schema + derived name.
    with open(mcap_path, "rb") as f:
        summary = make_reader(f).get_summary()
        match = None
        for _sid, ch in summary.channels.items():
            sch = summary.schemas.get(ch.schema_id)
            if is_camera_topic(ch.topic, sch.name if sch else None) and camera_name(ch.topic) == camera:
                match = ch.topic
                break
    if match is None:
        raise ValueError(f"camera {camera!r} not found in {mcap_path.name}")

    raw = out_mp4.with_suffix(".bitstream")
    n = 0
    codec = None
    with open(mcap_path, "rb") as f, open(raw, "wb") as out:
        for _schema, channel, message in make_reader(f).iter_messages(topics=[match]):
            payload, fmt = _cv_fields(message.data)
            if payload:
                if codec is None and fmt:
                    codec = fmt.lower()
                out.write(payload)
                n += 1
    if n == 0:
        raw.unlink(missing_ok=True)
        raise ValueError(f"no frames for camera {camera!r}")

    demuxer = _FMT_TO_FFMPEG.get(codec or "h264", "h264")
    # H.264 is universally playable in browsers, so stream-copy it (fast, lossless).
    # H.265/HEVC is NOT reliably decodable in Chrome/Firefox, so transcode it to
    # H.264 — slower, but otherwise the <video> tag shows "cannot decode".
    if demuxer == "h264":
        vcodec = ["-c:v", "copy"]
    else:
        vcodec = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"]
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", demuxer, "-r", str(fps), "-i", str(raw),
            *vcodec, "-movflags", "+faststart",
            "-f", "mp4", str(out_mp4),
        ],
        check=True, capture_output=True,
    )
    raw.unlink(missing_ok=True)
    return {"frames": n, "codec": codec}


def _pb_pool(reader):
    """Build a protobuf DescriptorPool from the FileDescriptorSets embedded in
    the MCAP's protobuf schema records, and return a name->message-class getter."""
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

    pool = descriptor_pool.DescriptorPool()
    added: set[str] = set()
    for _sid, sch in reader.get_summary().schemas.items():
        if sch.encoding != "protobuf":
            continue
        for fdp in descriptor_pb2.FileDescriptorSet.FromString(sch.data).file:
            if fdp.name not in added:
                try:
                    pool.Add(fdp)
                    added.add(fdp.name)
                except Exception:
                    pass

    def get(name):
        return message_factory.GetMessageClass(pool.FindMessageTypeByName(name))

    return get


def extract_meta_and_robot(mcap_path: Path, max_points: int = 600) -> dict:
    """Decode instruction + bimanual proprio trajectories into the shape
    ``robot_data.summarize`` produces, so the plots render unchanged."""
    with open(mcap_path, "rb") as f:
        reader = make_reader(f)
        get_msg = _pb_pool(reader)
        summary = reader.get_summary()

        proprio_topics, instruction, annotations = [], None, []
        schema_by_topic = {}
        for _sid, ch in summary.channels.items():
            sch = summary.schemas.get(ch.schema_id)
            schema_by_topic[ch.topic] = sch.name if sch else None
            if is_proprio_topic(ch.topic):
                proprio_topics.append(ch.topic)

        # Collect raw per-topic samples.
        series: dict[str, list] = {t: [] for t in proprio_topics}
        times: dict[str, list] = {t: [] for t in proprio_topics}
        f.seek(0)
        for schema, channel, message in make_reader(f).iter_messages(
            topics=proprio_topics + ["/instruction", "/subtask-annotation"]
        ):
            m = get_msg(schema.name).FromString(message.data)
            if channel.topic == "/instruction":
                if instruction is None:
                    instruction = getattr(m, "data", None)
                continue
            if channel.topic == "/subtask-annotation":
                ts = getattr(m, "timestamp", None)
                secs = (ts.seconds + ts.nanos / 1e9) if ts is not None else None
                annotations.append({"t": secs, "text": getattr(m, "data", "")})
                continue
            # proprio: RobotState has repeated position/velocity/torque; GripperState scalar
            series[channel.topic].append(_as_list(m, "position"))
            ts = getattr(m, "timestamp", None)
            times[channel.topic].append((ts.seconds + ts.nanos / 1e9) if ts is not None else 0.0)

    # Assemble into signals keyed by a friendly name; subsample for plotting.
    signals: dict[str, dict] = {}
    n_max, dur = 0, 0.0
    for topic, rows in series.items():
        if not rows:
            continue
        arr = np.asarray(rows, dtype=np.float64)  # (N, dims)
        n_max = max(n_max, arr.shape[0])
        ts = np.asarray(times[topic], dtype=np.float64)
        if ts.size:
            ts = ts - ts[0]
            dur = max(dur, float(ts[-1]))
        stride = max(1, arr.shape[0] // max_points)
        idx = np.arange(0, arr.shape[0], stride)
        name = topic.lstrip("/").replace("-", "_")  # e.g. left_arm_proprio
        signals[name] = {
            "dims": int(arr.shape[1]),
            "series": np.round(arr[idx], 5).tolist(),
            "min": round(float(arr.min()), 5),
            "max": round(float(arr.max()), 5),
            "_time": np.round(ts[idx], 4).tolist() if ts.size else [],
        }

    # Shared time axis: use the longest proprio topic's timestamps.
    time_axis = []
    if signals:
        time_axis = max(signals.values(), key=lambda s: len(s["_time"]))["_time"]
    for s in signals.values():
        s.pop("_time", None)

    summary_stats = {"num_steps": n_max}
    if dur > 0:
        summary_stats["duration_s"] = round(dur, 3)
        summary_stats["hz"] = round(n_max / dur, 1)

    return {
        "instruction": instruction,
        "annotations": annotations,
        "robot": {"keys": list(signals), "signals": signals,
                  "time": time_axis, "summary": summary_stats},
    }


def _as_list(msg, field: str) -> list[float]:
    """Return a message field as a float list, whether it's a repeated array
    (RobotState.position[6]) or a scalar (GripperState.position)."""
    v = getattr(msg, field)
    try:
        return [float(x) for x in v]  # repeated -> iterable of scalars
    except TypeError:
        return [float(v)]  # scalar
