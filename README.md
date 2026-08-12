# YAM Datasets Viewer

A lightweight web viewer for **YAM robot datasets** stored on S3 — teleop
demonstrations, policy rollouts, and public releases — across several on-disk
formats. Browse a catalog of every dataset, drill into tasks and episodes, play
the camera recordings, and inspect metadata, annotations, calibration, and robot
trajectories, all from a browser with no local downloads.

![screenshot](docs/screenshot.png)

## What it browses

The viewer speaks to multiple **sources** (each an S3 root in a known layout),
handled by a per-format adapter. Adding another dataset in a supported layout is
a one-line config entry.

| Source | Label | Format | Layout |
| --- | --- | --- | --- |
| `raiden` | Raiden | ZED `.svo2` | `<prefix>/<task>/<episode>/{metadata.json, cameras/*.svo2, robot_data.npz}` |
| `yam` | XDOF | Foxglove MCAP | `<prefix>/<task>/<episode>/output.mcap` |
| `yam_russet` | YAM (russet) | ZED `.svo2` | raiden layout (russet-recorded teleop) |
| `rollouts` | Raiden Rollouts | ZED `.svo2` | raiden layout + `rollout_info` in metadata (policy rollouts) |
| `abc130k` | ABC-130k (train) | MCAP | `<prefix>/<task>/<episode>/episode.mcap` |
| `abc130k_val` | ABC-130k (val) | MCAP | ABC-130k val split |
| `worldengine` | WorldEngine | LeRobot v3.0 | `<prefix>/<task>/{meta,data,videos}` (one dataset per task folder) |
| `molmoact2_yam` | MolmoAct2 Bimanual YAM | LeRobot v3.0 | `<prefix>/{meta,data,videos}` (single dataset, grouped by internal task) |

Four adapter kinds cover these:

- **`raiden`** — ZED `.svo2` recordings + `robot_data.npz` + `metadata.json`
  (optionally `calibration_results.json`).
- **`yam`** — one Foxglove-protobuf MCAP per episode (H.264/H.265 video +
  RobotState/GripperState + optional `/subtask-annotation`).
- **`lerobot`** — LeRobot v3.0, one self-contained dataset **per task folder**
  under the prefix (packed parquet timeseries + AV1 video).
- **`lerobot_single`** — LeRobot v3.0, a **single** dataset at the prefix root;
  its tens of thousands of episodes are grouped by their internal task label.

For each episode the viewer renders the video, a metadata panel, subtask
annotations (where present), per-signal robot trajectory plots, and — for
calibrated `.svo2` sources — a calibration summary with an alignment overlay.

## The catalog (landing page)

The home page is a **dataset catalog**: cross-dataset aggregate stats (total
datasets / episodes / tasks / hours / annotated) plus one card per dataset
showing its format, episode & task counts, total duration, camera names, and
whether it carries subtask annotations. A **Compare datasets** bar chart toggles
between Episodes / Hours / Tasks across all datasets. Click a card to open that
dataset's browser.

Per-dataset "deep" summaries (durations, per-task counts, sample-probed
annotation status) are computed in the background and cached to disk, so the
landing page loads instantly and fills in as scans finish — even for the large
sources (ABC-130k is ~130k episodes).

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
2560×720 → two 1280×720 eyes), and muxes to a web-streamable MP4. So the only
dependencies are `mcap` (pip) and `ffmpeg` — **the proprietary ZED SDK is not
needed**. The `yam` MCAP source decodes similarly (stream-copy H.264,
transcode H.265→H.264); LeRobot sources transcode **AV1→H.264**, trimmed to each
episode's `[from_ts, to_ts]` window within a shared packed video file.

Decoded clips are cached on disk (keyed by the S3 ETag) so repeat views are
instant.

## Running

Requirements: `ffmpeg` on PATH, AWS credentials with read access to the bucket,
and [`uv`](https://github.com/astral-sh/uv). LeRobot sources also need `pyarrow`
(declared in `pyproject.toml`).

```bash
./run.sh                      # serve on 0.0.0.0:8080
RAIDEN_PORT=9000 ./run.sh     # custom port
```

Then open `http://<host-ip>:8080/`. Links are shareable via the URL hash
(`#<source>/<task>/<episode>`); the bare root shows the catalog.

## Configuration (environment variables)

| Var | Default | Meaning |
| --- | --- | --- |
| `RAIDEN_S3_BUCKET` | `tri-ml-datasets-uw2` | default S3 bucket for built-in sources |
| `RAIDEN_S3_PREFIX` | `raiden_datasets/raw` | prefix for the default `raiden` source |
| `RAIDEN_AWS_REGION` | `us-west-2` | bucket region |
| `RAIDEN_HOST` | `0.0.0.0` | bind host |
| `RAIDEN_PORT` | `8080` | bind port |
| `RAIDEN_CACHE_DIR` | `/tmp/raiden_viz_cache` | disk cache for decoded video + catalog summaries |
| `RAIDEN_CACHE_MAX_GB` | `8` | cache size cap (LRU eviction; `0` disables) |
| `RAIDEN_BUCKET_PROFILES` | `{}` | JSON map of bucket → AWS profile, for access-gated buckets |

The set of sources is defined in `raiden_viz/config.py` (`SOURCES`). A source
flagged `requires_access` auto-hides on hosts whose credentials can't read its
bucket.

## HTTP API

All dataset browsing is **source-scoped** under `/api/sources/{sid}/…`, where
`sid` is a source id from the table above.

### Top level

| Endpoint | Returns |
| --- | --- |
| `GET /api/sources` | the sources readable on this host (`id`, `label`, `kind`) |
| `GET /api/catalog` | catalog for the landing page: cross-dataset aggregates + one summary card per dataset (counts, format, duration, cameras, annotation status). Serves cached cards instantly; kicks off background builds for any not yet computed (`building: true`) |
| `POST /api/catalog/{sid}/rebuild` | force-recompute one dataset's catalog summary |
| `GET /api/health` | liveness + the configured source ids |

### Per-source (`/api/sources/{sid}`)

| Endpoint | Returns |
| --- | --- |
| `GET …/overview` | dataset-wide summary: task/episode counts, stations, per-task breakdown (incl. each task's `collected_start`/`collected_end`), region |
| `GET …/stats?full=` | per-episode stat records for the charts (`full=true` scans every episode; otherwise a sampled pass with coverage reported) |
| `POST …/scan` | start (or resume) a cached background full scan of every episode's cheap stats — the data behind the episode filter — returning an immediate snapshot |
| `GET …/scan` | progress + accumulated records of an in-flight/finished scan (404 if none started) |
| `GET …/tasks` | task names |
| `GET …/tasks/{task}/episodes` | episode names (oldest first, matching raiden's own 0-based recording order) |
| `GET …/tasks/{task}/episode-facts` | `{episode: {timestamp, status}}` for the browse list; `{}` where a source has nothing cheap to report |
| `GET …/tasks/{task}/episodes/{episode}` | instruction, status, metadata, calibration, camera list, robot trajectory summary, subtask annotations |
| `GET …/tasks/{task}/episodes/{episode}/video?camera=&eye=left\|right` | decoded MP4 (transcodes + caches on first request) |
| `GET …/tasks/{task}/episodes/{episode}/calib?camera=` | calibration-check overlay PNG (raiden-style sources only) |

## UI

- **Catalog** (landing): aggregate stats, a Compare-datasets bar chart
  (Episodes / Hours / Tasks), and a card per dataset. The episode-browser sidebar
  is hidden here and appears once you open a dataset.
- **Dataset overview**: the S3 source path, region, aggregate counts, episode
  length histogram + length-vs-time scatter, an episode filter, and a per-task
  breakdown sortable by episode count, collection time (most recent first), or
  name. Each task row shows the dates it spans; the Collected sort is hidden for
  formats whose metadata carries no capture time (LeRobot).
- **Episode list** (sidebar): 0-based index, oldest first — the same numbering
  raiden records on disk (`0000`, `0001`, …), so an index here is the index there.
  Each row also shows the capture timestamp and a success/failure glyph where the
  source reports them.
- **Episode view**: all cameras in a grid (missing/stub cameras show a graceful
  placeholder), a shared play/scrub transport driving every tile at once, a
  metadata panel, a rollout/policy card (for rollout datasets), robot trajectory
  plots, subtask annotations, and a calibration summary.
- **Synced cursor**: while the videos play, a vertical cursor sweeps across every
  trajectory plot in lockstep with playback time.
- Links are shareable via the URL hash (`#<source>/<task>/<episode>`).

## Layout

```
raiden_viz/
  app.py           FastAPI routes (top-level + source-scoped)
  config.py        env-var config + the SOURCES table
  sources.py       source adapters: RaidenSource, YamMcapSource,
                   LeRobotSource, LeRobotSingleRootSource
  catalog.py       cross-dataset catalog: background build + disk cache
  s3.py            S3 browse / fetch helpers
  svo.py           .svo2 (MCAP + H.264) -> MP4 decoder
  yam.py           YAM MCAP decode (video + robot state + annotations)
  lerobot.py       LeRobot v3.0 parse (parquet timeseries + AV1 video)
  robot_data.py    robot_data.npz -> plot-ready series
  calib_overlay.py calibration-check overlay renderer
  cache.py         disk cache with per-key locks + LRU eviction
static/            self-contained frontend (no build step)
```

## Hosting for others (TRI-internal)

It's deployed on **`aws-anthony-1`** (`10.161.51.218`, an EC2 box in the
TRI-internal AWS VPC) so anyone on the TRI network/VPN can reach it directly:

```
http://10.161.51.218:8080/
```

That box works because it (a) sits on the VPN-routable `10.161.x` network — the
puget dev box is on the compute subnet that laptops/VPN can't route to — and
(b) has an IAM role that already reads `tri-ml-datasets-uw2`, so no credentials
are needed on the host.

**How it's run there** — a `systemd --user` service (survives reboot via linger):

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user status  raiden-viz
systemctl --user restart raiden-viz
```

**Note:** the deploy dir `~/raiden_viz/` on that box is a plain file tree, not a
git checkout of this repo — pushing an update is a manual sync + restart:

```bash
rsync -az --delete --exclude '.venv/' --exclude '.git/' --exclude '__pycache__/' \
  ./ aws-anthony-1:~/raiden_viz/
ssh aws-anthony-1 'export XDG_RUNTIME_DIR=/run/user/$(id -u); systemctl --user restart raiden-viz'
```

(Static frontend edits are served from disk and picked up on refresh; only
Python changes need the restart.)

Port 8080 is open on the instance's security group to internal CIDRs
(`10.0.0.0/8`, `172.16.0.0/12`) only — not the public internet.
