import pytest

from raiden_viz import cache, config


def test_get_or_create_produces_and_memoizes(cache_dir):
    calls = []

    def produce(dst):
        calls.append(dst)
        dst.write_bytes(b"hello")

    first = cache.get_or_create("thing.bin", produce)
    second = cache.get_or_create("thing.bin", produce)

    assert first == second
    assert first.read_bytes() == b"hello"
    assert len(calls) == 1, "second call must hit the cache, not re-produce"


@pytest.fixture
def remote(monkeypatch, fake_s3):
    """Turn the derived tier on and wire it to the fake client."""
    monkeypatch.setattr(config, "DERIVED_BUCKET", "derived-bucket")
    monkeypatch.setattr(config, "DERIVED_PREFIX", "derived")
    monkeypatch.setattr(cache, "_derived_client", lambda: fake_s3)
    return fake_s3


def test_remote_disabled_by_default(cache_dir, monkeypatch):
    monkeypatch.setattr(config, "DERIVED_BUCKET", "")
    assert cache.remote_enabled() is False


def test_produce_uploads_to_remote(cache_dir, remote):
    cache.get_or_create("clip.mp4", lambda dst: dst.write_bytes(b"video"))
    assert remote.objects["derived/clip.mp4"] == b"video"


def test_remote_hit_skips_produce(cache_dir, remote):
    remote.objects["derived/clip.mp4"] = b"video"

    def produce(dst):
        raise AssertionError("must not re-decode when the remote tier has it")

    out = cache.get_or_create("clip.mp4", produce)
    assert out.read_bytes() == b"video"


def test_exists_pulls_from_remote(cache_dir, remote):
    remote.objects["derived/clip.mp4"] = b"video"
    assert cache.exists("clip.mp4") is True
    assert (cache_dir / "clip.mp4").read_bytes() == b"video"


def test_exists_false_on_total_miss(cache_dir, remote):
    assert cache.exists("nope.mp4") is False


def test_remote_url_presigns_only_when_present(cache_dir, remote):
    assert cache.remote_url("clip.mp4") is None
    remote.objects["derived/clip.mp4"] = b"video"
    assert cache.remote_url("clip.mp4").startswith(
        "https://example.invalid/derived/clip.mp4"
    )


def test_push_remote_never_raises(cache_dir, remote, monkeypatch):
    """Caching is an optimisation: an S3 outage must not fail the request that
    produced the artifact."""

    def boom(*a, **k):
        raise RuntimeError("s3 down")

    monkeypatch.setattr(remote, "upload_file", boom)
    out = cache.get_or_create("clip.mp4", lambda dst: dst.write_bytes(b"video"))
    assert out.read_bytes() == b"video"


def test_no_remote_calls_when_tier_is_off(cache_dir, monkeypatch, fake_s3):
    """With the tier off, nothing must reach for an S3 client at all -- that is what
    keeps a laptop checkout and aws-anthony-1 working unchanged."""
    monkeypatch.setattr(config, "DERIVED_BUCKET", "")

    def no_client():
        raise AssertionError("must not construct an S3 client when the tier is off")

    monkeypatch.setattr(cache, "_derived_client", no_client)
    out = cache.get_or_create("clip.mp4", lambda dst: dst.write_bytes(b"video"))
    assert out.read_bytes() == b"video"
    assert cache.exists("clip.mp4") is True
    assert cache.remote_url("clip.mp4") is None


# --- local-only artifacts -------------------------------------------------
#
# The derived tier is for things that are EXPENSIVE TO PRODUCE. A raiden .svo2 is
# not produced at all — it is a byte-for-byte copy of a source object that already
# exists in tri-ml-datasets-uw2, downloaded only as the input to the transcode.
# Publishing it duplicated ~2.8 GB of source data into the derived bucket (a third
# of it) to save a re-download in the narrow case where the MP4 expired but the
# source did not. The yam adapter already treats its MCAP this way.


def test_local_only_artifact_is_not_uploaded(cache_dir, remote):
    cache.get_or_create("big.svo2", lambda dst: dst.write_bytes(b"src"), remote=False)
    assert "derived/big.svo2" not in remote.objects, "published a local-only artifact"


def test_local_only_artifact_is_not_fetched_from_remote(cache_dir, remote):
    """It must not read the tier either — otherwise old objects keep being pulled."""
    remote.objects["derived/big.svo2"] = b"stale"
    made = []
    out = cache.get_or_create("big.svo2", lambda dst: (made.append(1), dst.write_bytes(b"fresh"))[1],
                              remote=False)
    assert made == [1], "used the remote tier instead of producing locally"
    assert out.read_bytes() == b"fresh"


def test_default_still_publishes(cache_dir, remote):
    """Regression guard: the expensive derivatives must keep surviving redeploys."""
    cache.get_or_create("clip.mp4", lambda dst: dst.write_bytes(b"video"))
    assert remote.objects["derived/clip.mp4"] == b"video"
