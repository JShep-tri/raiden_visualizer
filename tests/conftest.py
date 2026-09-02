"""Shared fixtures.

The S3 double keeps the suite dependency-free: the derived cache tier touches
exactly four boto3 methods, so a hand-rolled fake is smaller and faster than pulling
in moto.
"""

import pytest
from botocore.exceptions import ClientError

from raiden_viz import config


class _Exceptions:
    """Mimics the ``client.exceptions`` namespace boto3 clients expose."""

    ClientError = ClientError


class FakeS3:
    """In-memory stand-in for a boto3 S3 client, scoped to the methods cache.py uses."""

    exceptions = _Exceptions

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.uploads: list[tuple[str, str]] = []  # (key, local path) per upload_file

    @staticmethod
    def _missing(op: str) -> ClientError:
        return ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, op)

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise self._missing("HeadObject")
        return {"ContentLength": len(self.objects[Key])}

    def download_file(self, Bucket, Key, Filename):
        if Key not in self.objects:
            raise self._missing("GetObject")
        with open(Filename, "wb") as f:
            f.write(self.objects[Key])

    def upload_file(self, Filename, Bucket, Key):
        with open(Filename, "rb") as f:
            self.objects[Key] = f.read()
        self.uploads.append((Key, str(Filename)))

    def generate_presigned_url(self, ClientMethod, Params, ExpiresIn):
        return f"https://example.invalid/{Params['Key']}?exp={ExpiresIn}"


@pytest.fixture
def fake_s3():
    return FakeS3()


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Point the module-level CACHE_DIR at a temp dir for the duration of a test."""
    d = tmp_path / "cache"
    d.mkdir()
    monkeypatch.setattr(config, "CACHE_DIR", d)
    return d
