"""Thin S3 helpers for browsing and fetching raw Raiden dataset objects."""

import json
from dataclasses import dataclass

import boto3
from botocore.config import Config

from . import config


@dataclass
class S3Object:
    key: str
    size: int
    etag: str
    last_modified: str | None = None  # ISO8601, from S3 LastModified


# boto3 clients cached per (bucket) — some buckets (e.g. the vendor xdof-yam-data)
# are only readable via a specific SSO profile, configured in config.BUCKET_PROFILES.
_clients: dict[str, object] = {}

# Connection-pool size. botocore defaults to 10, which starves the stat scans badly:
# stats()/scan use ThreadPoolExecutor(max_workers=32), the catalog builds every
# source concurrently, and clients are cached per BUCKET — so all sources sharing
# one bucket share one client. Peak demand is therefore 32 x (sources on that
# bucket), ~256 today. At the default 10 the surplus threads queue and urllib3
# discards connections, paying a fresh TLS handshake per retry — brutal when the
# bucket is cross-region from the task (us-west-2 vs us-east-1).
_MAX_POOL_CONNECTIONS = 64


def _client(bucket: str | None = None):
    b = _bucket(bucket)
    if b not in _clients:
        profile = config.BUCKET_PROFILES.get(b)
        session = boto3.Session(profile_name=profile) if profile else boto3.Session()
        _clients[b] = session.client(
            "s3",
            region_name=config.AWS_REGION,
            config=Config(
                retries={"max_attempts": 3, "mode": "standard"},
                max_pool_connections=_MAX_POOL_CONNECTIONS,
            ),
        )
    return _clients[b]


def _bucket(bucket: str | None) -> str:
    return bucket or config.S3_BUCKET


def list_dirs(prefix: str, bucket: str | None = None) -> list[str]:
    """List immediate subdirectory names under an S3 prefix (one level)."""
    prefix = prefix.rstrip("/") + "/"
    paginator = _client(bucket).get_paginator("list_objects_v2")
    names: list[str] = []
    for page in paginator.paginate(Bucket=_bucket(bucket), Prefix=prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            sub = cp["Prefix"][len(prefix):].strip("/")
            if sub:
                names.append(sub)
    return sorted(names)


def head(key: str, bucket: str | None = None) -> S3Object:
    r = _client(bucket).head_object(Bucket=_bucket(bucket), Key=key)
    lm = r.get("LastModified")
    return S3Object(
        key=key, size=r["ContentLength"], etag=r["ETag"].strip('"'),
        last_modified=lm.isoformat() if lm else None,
    )


def try_head(key: str, bucket: str | None = None) -> S3Object | None:
    try:
        return head(key, bucket=bucket)
    except Exception:
        return None


def get_json(key: str, bucket: str | None = None) -> dict:
    r = _client(bucket).get_object(Bucket=_bucket(bucket), Key=key)
    return json.loads(r["Body"].read())


def try_get_json(key: str, bucket: str | None = None) -> dict | None:
    """get_json, but return None if the object is missing (not every episode has
    an optional sidecar)."""
    try:
        return get_json(key, bucket=bucket)
    except Exception:
        return None


def get_bytes(key: str, bucket: str | None = None) -> bytes:
    """Fetch an object's full body into memory (used for the small LeRobot meta +
    data parquets — hundreds of KB — that don't warrant the on-disk clip cache)."""
    r = _client(bucket).get_object(Bucket=_bucket(bucket), Key=key)
    return r["Body"].read()


def download(key: str, dest, bucket: str | None = None) -> None:
    _client(bucket).download_file(_bucket(bucket), key, str(dest))


def get_range(key: str, start: int, end: int, bucket: str | None = None) -> bytes:
    """Fetch bytes [start, end] (inclusive) of an object via HTTP Range."""
    r = _client(bucket).get_object(Bucket=_bucket(bucket), Key=key, Range=f"bytes={start}-{end}")
    return r["Body"].read()


def list_files(prefix: str, bucket: str | None = None) -> list[S3Object]:
    """List file objects (not dirs) directly under a prefix."""
    prefix = prefix.rstrip("/") + "/"
    paginator = _client(bucket).get_paginator("list_objects_v2")
    out: list[S3Object] = []
    for page in paginator.paginate(Bucket=_bucket(bucket), Prefix=prefix, Delimiter="/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key == prefix:
                continue
            out.append(S3Object(key=key, size=obj["Size"], etag=obj["ETag"].strip('"')))
    return out


def list_keys(prefix: str, bucket: str | None = None, suffix: str | None = None) -> list[S3Object]:
    """Recursively list every object under a prefix (no delimiter), optionally
    filtered by key suffix. Used to walk LeRobot's chunk-*/file-* subtrees."""
    prefix = prefix.rstrip("/") + "/"
    paginator = _client(bucket).get_paginator("list_objects_v2")
    out: list[S3Object] = []
    for page in paginator.paginate(Bucket=_bucket(bucket), Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/") or (suffix and not key.endswith(suffix)):
                continue
            out.append(S3Object(key=key, size=obj["Size"], etag=obj["ETag"].strip('"'),
                                last_modified=obj["LastModified"].isoformat() if obj.get("LastModified") else None))
    return out
