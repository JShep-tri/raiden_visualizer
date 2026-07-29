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
    # The public YAM bimanual dataset: a set of LeRobot v3.0 datasets (one per task
    # folder under this prefix). Packed parquet timeseries + AV1 video, handled by
    # the lerobot adapter. Each task's episode instructions + subtask labels come
    # from the parquet, so no sidecar is needed.
    {"id": "yam_bimanual", "label": "YAM Bimanual (public)", "kind": "lerobot",
     "bucket": S3_BUCKET, "prefix": "yam_public/bimanual-dataset"},
    # MolmoAct2 bimanual YAM: a SINGLE LeRobot v3.0 dataset at the prefix root
    # (meta/data/videos directly under it, no per-task subfolders), ~32k episodes
    # across 34 internal tasks. Handled by the single-root lerobot adapter, which
    # groups episodes by their dataset task label. 3 cams (left/right/top), AV1.
    {"id": "molmoact2_yam", "label": "MolmoAct2 Bimanual YAM", "kind": "lerobot_single",
     "bucket": S3_BUCKET, "prefix": "yam_public/MolmoAct2-BimanualYAM"},
]

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

HOST = os.environ.get("RAIDEN_HOST", "0.0.0.0")
PORT = int(os.environ.get("RAIDEN_PORT", "8080"))

# Cap on cache size (GB); oldest clips are evicted past this. 0 disables eviction.
# Kept modest by default: the deploy host's disk is shared with other work, so
# the cache must stay bounded well under free space.
CACHE_MAX_GB = float(os.environ.get("RAIDEN_CACHE_MAX_GB", "8"))

CACHE_DIR.mkdir(parents=True, exist_ok=True)
