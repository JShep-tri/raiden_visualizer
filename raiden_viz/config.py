"""Runtime configuration, read from environment with sensible defaults."""

import os
from pathlib import Path

# Default S3 root under which task/episode folders live. Overridable so the same
# viewer can point at other raw dataset roots.
#   s3://<bucket>/<prefix>/<task>/<episode>/{metadata.json, cameras/*.svo2, robot_data.npz}
S3_BUCKET = os.environ.get("RAIDEN_S3_BUCKET", "tri-ml-datasets-uw2")
S3_PREFIX = os.environ.get("RAIDEN_S3_PREFIX", "raiden_datasets/raw").strip("/")
AWS_REGION = os.environ.get("RAIDEN_AWS_REGION", "us-west-2")

# Datasets the viewer can browse. Each has a distinct on-disk format handled by a
# dedicated source adapter (see sources.py). "kind" selects the adapter.
#   raiden:  <prefix>/<task>/<episode>/{metadata.json, cameras/*.svo2, robot_data.npz}
#   yam:     <prefix>/<task>/episode_<uuid>/<mcap_name>  (one Foxglove-protobuf MCAP)
#   lerobot: <prefix>/<task>/{meta,data,videos}  (LeRobot v3.0: packed parquet + AV1)
SOURCES = [
    {"id": "raiden", "label": "Raiden", "kind": "raiden", "bucket": S3_BUCKET, "prefix": S3_PREFIX},
    {"id": "yam", "label": "XDOF", "kind": "yam", "bucket": S3_BUCKET,
     "prefix": "yam_raw/2026_03_30_zed", "mcap_name": "output.mcap"},
    # YAM teleop recorded on the russet station, uploaded from ~/data/raw. Same
    # raiden .svo2 layout (metadata.json + cameras/*.svo2 + robot_data.npz), so it
    # uses the raiden adapter.
    {"id": "yam_russet", "label": "YAM (russet)", "kind": "raiden", "bucket": S3_BUCKET, "prefix": "yam_datasets/raw"},
    # rfm_rl policy ROLLOUTS recorded on russet (rfm_rl_rollout). Identical
    # raiden .svo2 layout (metadata.json + cameras/*.svo2 + robot_data.npz);
    # metadata.json carries an extra rollout_info block that the raiden adapter
    # ignores. See raiden-rfm-flow-policy work.
    {"id": "rollouts", "label": "Raiden Rollouts", "kind": "raiden", "bucket": S3_BUCKET, "prefix": "raiden_datasets/rollouts"},
    # ABC-130k: the full open-source YAM dataset (xdof/Amazon). Same Foxglove-MCAP
    # format as the yam source but with episode.mcap files, newer topic names
    # (/<cam>, -state) and H.265 video — all handled by the yam adapter/decoder.
    {"id": "abc130k", "label": "ABC-130k (train)", "kind": "yam", "bucket": S3_BUCKET,
     "prefix": "vla_foundry_datasets_test/raw_datasets_bot/abc-130k/data/train", "mcap_name": "episode.mcap"},
    # The public yam_public/ABC-130k mirror carries a val split (same episode.mcap
    # layout + content as the train mirror above, 189 tasks) that our train-only
    # source doesn't expose. Same yam adapter.
    {"id": "abc130k_val", "label": "ABC-130k (val)", "kind": "yam", "bucket": S3_BUCKET,
     "prefix": "yam_public/ABC-130k/data/val", "mcap_name": "episode.mcap"},
    # The xdof VENDOR bucket copy of the zed collection — carries inline
    # /subtask-annotation labels (which the tri-ml mirror lacks). Readable only via
    # the manip-cluster SSO profile (see BUCKET_PROFILES).
    {"id": "xdof_zed", "label": "YAM (xdof zed)", "kind": "yam", "bucket": "xdof-yam-data",
     "prefix": "2026_03_30_zed", "mcap_name": "output.mcap", "requires_access": True,
     # Per-episode sidecar JSON (camera intrinsics/distortion + episode fields) that
     # ships alongside the MCAPs under this prefix, keyed by the episode uuid. The
     # MCAP carries no intrinsics, so this is the only calibration for xdof.
     "metadata_prefix": "metadata_202507"},
    # WorldEngine: the public YAM dataset — a set of LeRobot v3.0 datasets (one per
    # task folder under this prefix). Packed parquet timeseries + AV1 video, handled
    # by the lerobot adapter. Each task's episode instructions + subtask labels come
    # from the parquet, so no sidecar is needed. (Formerly at yam_public/bimanual-dataset
    # with id yam_bimanual; renamed + relocated to yam_public/WorldEngine 2026-07.)
    {"id": "worldengine", "label": "WorldEngine", "kind": "lerobot",
     "bucket": S3_BUCKET, "prefix": "yam_public/WorldEngine"},
    # MolmoAct2 bimanual YAM: a SINGLE LeRobot v3.0 dataset at the prefix root
    # (meta/data/videos directly under it, no per-task subfolders), ~32k episodes
    # across 34 internal tasks. Handled by the single-root lerobot adapter, which
    # groups episodes by their dataset task label. 3 cams (left/right/top), AV1.
    {"id": "molmoact2_yam", "label": "MolmoAct2 Bimanual YAM", "kind": "lerobot_single",
     "bucket": S3_BUCKET, "prefix": "yam_public/MolmoAct2-BimanualYAM"},
]

# Sources to drop entirely at startup, by id. A deployed container cannot reach a
# source that depends on a local SSO profile (see BUCKET_PROFILES): get_sources()
# would filter it anyway, but only after a live S3 probe against a bucket the task
# role has no path to, on every /api/sources call. Naming it here skips that.
DISABLED_SOURCES = {
    s.strip() for s in os.environ.get("RAIDEN_DISABLED_SOURCES", "").split(",") if s.strip()
}
SOURCES = [_s for _s in SOURCES if _s["id"] not in DISABLED_SOURCES]

# Buckets that require a specific AWS profile (SSO) rather than the default
# credentials/instance-role. The xdof vendor bucket is granted to the
# Robotics-LBM-PowerUserAccess role in acct 682769330988 (the 'manip-cluster'
# profile), not to the default puget role or the EC2 instance role.
import json as _json
BUCKET_PROFILES = _json.loads(os.environ.get("RAIDEN_BUCKET_PROFILES", "{}")) or {
    "xdof-yam-data": os.environ.get("RAIDEN_XDOF_PROFILE", "manip-cluster"),
}

# Local cache for downloaded .svo2 files and transcoded .mp4 clips. Decoding is
# expensive, so results are memoized on disk keyed by the S3 object's ETag+size.
# Honors CACHE_DIR (the shared convention used by other TRI viewers, e.g. AnyFile
# maps a persistent EBS volume there) with RAIDEN_CACHE_DIR taking precedence.
CACHE_DIR = Path(
    os.environ.get("RAIDEN_CACHE_DIR")
    or os.environ.get("CACHE_DIR")
    or "/tmp/raiden_viz_cache"
)

# The robot teachers: the people collecting teleop data as their job, as opposed to
# researchers recording ad-hoc episodes. The overview's Tasks card can narrow to just
# the tasks they worked on, so "what did we actually collect for training" isn't
# buried among one-off test tasks. Matched case-insensitively against
# metadata.json's teacher_name. Update here as the roster changes.
ROBOT_TEACHERS = [t.strip() for t in os.environ.get(
    "RAIDEN_ROBOT_TEACHERS", "Fredy,Emma,Derick,Rudy").split(",") if t.strip()]

HOST = os.environ.get("RAIDEN_HOST", "0.0.0.0")
PORT = int(os.environ.get("RAIDEN_PORT", "8080"))

# Cap on cache size (GB); oldest clips are evicted past this. 0 disables eviction.
# Kept modest by default: the deploy host's disk is shared with other work, so
# the cache must stay bounded well under free space.
CACHE_MAX_GB = float(os.environ.get("RAIDEN_CACHE_MAX_GB", "8"))

# Durable second cache tier. Decoding an episode costs a 200-880 MB S3 download plus
# an ffmpeg pass, so the result is worth keeping somewhere that outlives the host:
# the local CACHE_DIR is per-container and dies on every redeploy. Empty bucket =
# tier disabled = exactly the pre-existing local-only behaviour, which is what keeps
# a laptop checkout and the aws-anthony-1 box working unchanged.
DERIVED_BUCKET = os.environ.get("RAIDEN_DERIVED_BUCKET", "")
DERIVED_PREFIX = os.environ.get("RAIDEN_DERIVED_PREFIX", "derived").strip("/")
# Presigned-URL lifetime. Long enough to scrub through a clip, short enough that a
# copied link is not a durable handle to dataset content.
DERIVED_URL_TTL = int(os.environ.get("RAIDEN_DERIVED_URL_TTL", "3600"))

# Build the catalog cards at STARTUP rather than on the first request. Nothing else
# touches /api/catalog — the load balancer only polls /api/health — so without this
# the cold-cache scan is paid by whoever opens the dashboard first, which in a
# deployed environment is a colleague rather than the person who deployed. Set
# RAIDEN_WARM_CATALOG=0 for a laptop checkout, where scanning every source on
# import is not what you want.
# Log level for the raiden_viz loggers. Nothing else configures logging, so without
# app.py's _configure_logging the logger has no handler at all and Python's
# lastResort fallback emits WARNING and above only — which silently dropped every
# INFO line, including the catalog warmup confirmation.
LOG_LEVEL = os.environ.get("RAIDEN_LOG_LEVEL", "INFO").strip().upper()

# How long a source's overview() is reused. It walks every task listing every
# episode — roughly 130 paginated LIST calls cross-region on the largest source —
# and nothing cached it, so every dataset click paid it again. Long enough that
# browsing never re-lists, short enough that new episodes surface promptly.
OVERVIEW_TTL_S = float(os.environ.get("RAIDEN_OVERVIEW_TTL_S", "300"))

# Start every source's FULL per-episode scan at boot. That scan backs the episode
# filter, and nothing started it: a user clicked the button and then watched it —
# ~51 minutes on the largest source. Deploying at a quiet hour only helps if the
# scan happens at that hour too.
#
# OFF by default, unlike the catalog warmup: this is hours of S3 work at every
# boot, which is right for a deployed container and wrong for a laptop checkout.
# Deployed environments opt in via RAIDEN_WARM_SCANS=1.
WARM_SCANS_ON_START = os.environ.get("RAIDEN_WARM_SCANS", "0").strip().lower() in (
    "1", "true", "yes",
)

WARM_CATALOG_ON_START = os.environ.get("RAIDEN_WARM_CATALOG", "1").strip().lower() not in (
    "0", "false", "no", "",
)

CACHE_DIR.mkdir(parents=True, exist_ok=True)
