"""Which video artifacts earn a place in the durable derived tier.

The tier is for things expensive to PRODUCE. A decoded MP4 qualifies: minutes of
ffmpeg over a large download. The .svo2 it was decoded from does not — it is a
byte-for-byte copy of an object that already lives in the datasets bucket, pulled
only as the transcode's input. Publishing it duplicated ~2.8 GB of source data into
the derived bucket, about a third of its contents, to buy back a re-download in the
one case where the MP4 expired but its source did not.

The yam adapter already treats its MCAP this way: a .tmp file removed in a finally,
never published.
"""

from pathlib import Path

import pytest

from raiden_viz import sources


@pytest.fixture
def raiden():
    return sources.RaidenSource(
        {"id": "raiden", "label": "Raiden", "kind": "raiden",
         "bucket": "tri-ml-datasets-uw2", "prefix": "raiden_datasets/raw"}
    )


@pytest.fixture
def recorded(monkeypatch):
    """Record every (cache_name, remote) the adapter asks for, producing nothing."""
    calls = []

    def fake_get_or_create(cache_name, produce, remote=True):
        calls.append((cache_name, remote))
        return Path("/tmp/unused")

    monkeypatch.setattr(sources.cache, "get_or_create", fake_get_or_create)
    monkeypatch.setattr(
        sources.s3, "try_head",
        lambda key, bucket=None: sources.s3.S3Object(key=key, size=5_000_000, etag="ETAG"),
    )
    return calls


def _by_suffix(calls, suffix):
    return [c for c in calls if c[0].endswith(suffix)]


def test_svo2_intermediate_is_kept_local(raiden, recorded):
    raiden.video_path("task", "ep", "cam0", "left")
    svo2 = _by_suffix(recorded, ".svo2")
    assert svo2, "adapter did not fetch an .svo2 at all"
    assert svo2[0][1] is False, "the .svo2 intermediate is still being published"


def test_decoded_mp4_is_still_published(raiden, recorded):
    """The expensive artifact must keep surviving redeploys — that is the whole
    point of the tier, and the reason a warmed episode stays warm for everyone."""
    raiden.video_path("task", "ep", "cam0", "left")
    mp4 = _by_suffix(recorded, ".mp4")
    assert mp4, "adapter did not produce an mp4"
    assert mp4[0][1] is True, "the decoded clip is no longer published"


def test_missing_camera_still_raises(raiden, monkeypatch):
    monkeypatch.setattr(sources.s3, "try_head", lambda key, bucket=None: None)
    with pytest.raises(FileNotFoundError):
        raiden.video_path("task", "ep", "cam0", "left")


def test_stub_file_still_raises(raiden, monkeypatch):
    monkeypatch.setattr(
        sources.s3, "try_head",
        lambda key, bucket=None: sources.s3.S3Object(key=key, size=1500, etag="E"),
    )
    with pytest.raises(ValueError):
        raiden.video_path("task", "ep", "cam0", "left")
