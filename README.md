# Raiden Dataset Viewer

A lightweight web viewer for **raw Raiden robot teleop datasets** stored on S3.
Browse tasks and episodes, play the camera recordings, and inspect metadata,
calibration, and robot trajectories — all from a browser, no local downloads.

![screenshot](docs/screenshot.png)

## What it shows

Each episode lives at `s3://<bucket>/<prefix>/<task>/<episode>/` and contains:

| File | Contents |
| --- | --- |
| `metadata.json` | task instruction, teacher, station, duration, robot rate, camera list |
| `calibration_results.json` | per-camera intrinsics + extrinsics + hand-eye calibration |
| `robot_data.npz` | bimanual joint / gripper trajectories (~85 Hz) |
| `cameras/*.svo2` | ZED stereo camera recordings |

The viewer renders the video, a metadata panel, per-signal trajectory plots, and
a calibration summary.

## The `.svo2` decode (no ZED SDK required)

The `.svo2` files are **MCAP containers**, not opaque ZED blobs. The image
channel (`.../side_by_side`) carries an **H.264 Annex-B** elementary stream —
one MCAP message per frame, each payload prefixed with an 8-byte header
(`uint32 total_len`, `uint32 h264_len`):

```
[total_len][h264_len][ h264_len bytes of Annex-B ][ trailing ]
```

`raiden_viz` concatenates the H.264 payloads across frames, hands the raw stream
to `ffmpeg`, crops to a single eye (the stream is side-by-side stereo, e.g.
2560×720 → two 1280×720 eyes), and muxes to a web-streamable MP4. This means the
only dependencies are `mcap` (pip) and `ffmpeg` — **the proprietary ZED SDK is
not needed**.

Decoded clips are cached on disk (keyed by the S3 ETag) so repeat views are
instant.

## Running

Requirements: `ffmpeg` on PATH, AWS credentials with read access to the bucket,
and [`uv`](https://github.com/astral-sh/uv).

```bash
./run.sh                      # serve on 0.0.0.0:8080
RAIDEN_PORT=9000 ./run.sh     # custom port
```

Then open `http://<host-ip>:8080/`. Episode links are shareable via the URL hash
(`#<task>/<episode>`).

## Configuration (environment variables)

| Var | Default | Meaning |
| --- | --- | --- |
| `RAIDEN_S3_BUCKET` | `tri-ml-datasets-uw2` | S3 bucket |
| `RAIDEN_S3_PREFIX` | `raiden_datasets/raw` | prefix under which `<task>/<episode>/` live |
| `RAIDEN_AWS_REGION` | `us-west-2` | bucket region |
| `RAIDEN_HOST` | `0.0.0.0` | bind host |
| `RAIDEN_PORT` | `8080` | bind port |
| `RAIDEN_CACHE_DIR` | `/tmp/raiden_viz_cache` | disk cache for `.svo2` + `.mp4` |
| `RAIDEN_CACHE_MAX_GB` | `20` | cache size cap (LRU eviction; `0` disables) |

## HTTP API

| Endpoint | Returns |
| --- | --- |
| `GET /api/tasks` | task folder names |
| `GET /api/tasks/{task}/episodes` | episode folder names (newest first) |
| `GET /api/tasks/{task}/episodes/{episode}` | metadata + calibration + camera list + robot trajectory summary |
| `GET /api/tasks/{task}/episodes/{episode}/video?camera=&eye=left\|right` | decoded MP4 (transcodes + caches on first request) |
| `GET /api/health` | liveness + configured bucket/prefix |

## Layout

```
raiden_viz/
  app.py          FastAPI routes
  s3.py           S3 browse / fetch helpers
  svo.py          .svo2 (MCAP + H.264) -> MP4 decoder
  robot_data.py   robot_data.npz -> plot-ready series
  cache.py        disk cache with per-key locks + LRU eviction
  config.py       env-var configuration
static/           self-contained frontend (no build step)
```
