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


def _client():
    # Reused across calls; boto3 clients are thread-safe.
    if not hasattr(_client, "_c"):
        _client._c = boto3.client(
            "s3",
            region_name=config.AWS_REGION,
            config=Config(retries={"max_attempts": 3, "mode": "standard"}),
        )
    return _client._c


def list_dirs(prefix: str) -> list[str]:
    """List immediate subdirectory names under an S3 prefix (one level)."""
    prefix = prefix.rstrip("/") + "/"
    paginator = _client().get_paginator("list_objects_v2")
    names: list[str] = []
    for page in paginator.paginate(Bucket=config.S3_BUCKET, Prefix=prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            sub = cp["Prefix"][len(prefix):].strip("/")
            if sub:
                names.append(sub)
    return sorted(names)


def head(key: str) -> S3Object:
    r = _client().head_object(Bucket=config.S3_BUCKET, Key=key)
    return S3Object(key=key, size=r["ContentLength"], etag=r["ETag"].strip('"'))


def try_head(key: str) -> S3Object | None:
    try:
        return head(key)
    except _client().exceptions.ClientError:
        return None
    except Exception:
        return None


def get_json(key: str) -> dict:
    r = _client().get_object(Bucket=config.S3_BUCKET, Key=key)
    return json.loads(r["Body"].read())


def download(key: str, dest) -> None:
    _client().download_file(config.S3_BUCKET, key, str(dest))


def list_files(prefix: str) -> list[S3Object]:
    """List file objects (not dirs) directly under a prefix."""
    prefix = prefix.rstrip("/") + "/"
    paginator = _client().get_paginator("list_objects_v2")
    out: list[S3Object] = []
    for page in paginator.paginate(Bucket=config.S3_BUCKET, Prefix=prefix, Delimiter="/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key == prefix:
                continue
            out.append(S3Object(key=key, size=obj["Size"], etag=obj["ETag"].strip('"')))
    return out
