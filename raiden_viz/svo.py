"""Decode ZED .svo2 recordings to browser-playable MP4 without the ZED SDK.

A .svo2 file is really an MCAP container. The per-camera image channel
(topic ".../side_by_side") carries the stereo image as an H.264 Annex-B
elementary stream, one MCAP message per frame. Each message payload is:

    [uint32 total_len][uint32 h264_len][h264_len bytes of Annex-B ...][trailing]

We concatenate the H.264 payloads across frames, hand the raw elementary
stream to ffmpeg, crop to a single eye, and mux to MP4. No proprietary SDK.
"""

import struct
import subprocess
from pathlib import Path

from mcap.reader import make_reader

# Header layout of each side_by_side message payload.
_HDR = struct.Struct("<II")  # (total_len, h264_len)

EYES = ("left", "right")


def probe_channels(svo_path: Path) -> dict:
    """Return channel/topic info and detected image geometry for a .svo2 file."""
    with open(svo_path, "rb") as f:
        reader = make_reader(f)
        summary = reader.get_summary()
        channels = {}
        image_topic = None
        for cid, ch in summary.channels.items():
            sch = summary.schemas.get(ch.schema_id)
            count = 0
            if summary.statistics:
                count = summary.statistics.channel_message_counts.get(cid, 0)
            channels[ch.topic] = {
                "encoding": ch.message_encoding,
                "schema": sch.name if sch else None,
                "messages": count,
            }
            if ch.topic.endswith("side_by_side"):
                image_topic = ch.topic
    return {"channels": channels, "image_topic": image_topic}


def _extract_h264(svo_path: Path, out_h264: Path) -> int:
    """Write the concatenated H.264 elementary stream; return frame count."""
    n = 0
    with open(svo_path, "rb") as f, open(out_h264, "wb") as out:
        for _schema, channel, message in make_reader(f).iter_messages():
            if not channel.topic.endswith("side_by_side"):
                continue
            data = message.data
            if len(data) < _HDR.size:
                continue
            _total, h264_len = _HDR.unpack_from(data, 0)
            # Slice exactly the declared H.264 bytes (trailing bytes are ZED
            # side-channel data that would trip the decoder if included).
            out.write(data[_HDR.size : _HDR.size + h264_len])
            n += 1
    return n


def decode_to_mp4(svo_path: Path, out_mp4: Path, eye: str = "left", fps: int = 30) -> dict:
    """Decode one eye of a side-by-side .svo2 stream to an MP4 file.

    Returns a dict with frame count and per-eye dimensions.
    """
    if eye not in EYES:
        raise ValueError(f"eye must be one of {EYES}, got {eye!r}")

    h264_path = out_mp4.with_suffix(".h264")
    n_frames = _extract_h264(svo_path, h264_path)
    if n_frames == 0:
        h264_path.unlink(missing_ok=True)
        raise ValueError(f"No side_by_side image frames found in {svo_path.name}")

    width, height = _stream_dims(h264_path)
    half = width // 2  # side-by-side => each eye is half the width
    x_off = 0 if eye == "left" else half

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "h264", "-i", str(h264_path),
        "-vf", f"crop={half}:{height}:{x_off}:0",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",  # web streaming: moov atom up front
        "-r", str(fps),
        # Force MP4 muxing: the cache writes to a randomized temp filename whose
        # extension ffmpeg can't use to infer the container, so state it explicitly.
        "-f", "mp4",
        str(out_mp4),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    h264_path.unlink(missing_ok=True)
    return {"frames": n_frames, "width": half, "height": height, "eye": eye}


def _stream_dims(h264_path: Path) -> tuple[int, int]:
    """Probe width/height of the raw H.264 elementary stream via ffprobe."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0:s=x",
            str(h264_path),
        ],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    w, h = out.split("x")
    return int(w), int(h)
