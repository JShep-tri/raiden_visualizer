"""Route-level behaviour for the video endpoint.

The source registry is stubbed so these tests never touch S3 or ffmpeg: what is under
test is the routing decision (redirect vs stream), not decoding.
"""

import pytest
from fastapi.testclient import TestClient

from raiden_viz import app as app_module
from raiden_viz import cache

URL = "/api/sources/fake/tasks/t1/episodes/e1/video?camera=cam0"


class FakeSource:
    def __init__(self, mp4):
        self._mp4 = mp4

    def video_path(self, task, episode, camera, eye):
        return self._mp4


@pytest.fixture
def client(tmp_path, monkeypatch):
    mp4 = tmp_path / "clip.mp4"
    mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    monkeypatch.setattr(app_module, "_src", lambda sid: FakeSource(mp4))
    return TestClient(app_module.app)


def test_streams_the_file_when_remote_tier_is_off(client, monkeypatch):
    monkeypatch.setattr(cache, "remote_url", lambda name: None)
    r = client.get(URL)
    assert r.status_code == 200
    assert r.headers["content-type"] == "video/mp4"


def test_redirects_when_the_derived_tier_holds_the_clip(client, monkeypatch):
    monkeypatch.setattr(cache, "remote_url", lambda name: "https://example.invalid/clip.mp4")
    r = client.get(URL, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "https://example.invalid/clip.mp4"


def test_redirect_is_keyed_on_the_cache_name(client, monkeypatch):
    """The presigned lookup must use the artifact's cache key -- the file name --
    not the route's camera/eye parameters, which do not match it for every adapter."""
    seen = []

    def spy(name):
        seen.append(name)
        return None

    monkeypatch.setattr(cache, "remote_url", spy)
    client.get(URL)
    assert seen == ["clip.mp4"]


def test_unhandled_errors_do_not_leak_internals():
    """str(exc) put bucket names and key paths in front of the browser."""
    from fastapi import FastAPI

    probe = FastAPI()

    @probe.get("/boom")
    def boom():
        raise RuntimeError("s3://tri-ml-datasets-uw2/secret/prefix denied")

    probe.add_exception_handler(Exception, app_module._unhandled)
    r = TestClient(probe, raise_server_exceptions=False).get("/boom")
    assert r.status_code == 500
    assert "tri-ml-datasets-uw2" not in r.text
