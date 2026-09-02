"""Clip decode jobs.

Decoding one camera costs a 200-880 MB download plus an ffmpeg pass — ~2.5 minutes
for a 100 MB .svo2. Doing that inside the request means the ALB's 60s idle timeout
severs the browser first, and the <video> element reports the truncated response as
"Could not decode this stream" — a decode failure message for what is actually a
timeout, while the decode goes on to succeed server-side.

So the work moves off the request: a job runs it in the background and the route
reports progress until the clip is ready.
"""

import threading
import time

import pytest

from raiden_viz import clips


class _ImmediateThread:
    """Runs the target inline so ensure() is synchronous under test."""

    def __init__(self, target=None, args=(), daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


@pytest.fixture
def sync_threads(monkeypatch):
    monkeypatch.setattr(clips.threading, "Thread", _ImmediateThread)


@pytest.fixture
def jobs(sync_threads):
    return clips.ClipJobs()


# --- the happy path -------------------------------------------------------


def test_unknown_clip_is_not_ready_and_starts_a_job(jobs):
    calls = []
    st = jobs.ensure("k", lambda: calls.append(1))
    assert calls == [1], "ensure did not start the job"
    assert st["ready"] is True or st["decoding"] is True


def test_finished_job_reports_ready(jobs):
    jobs.ensure("k", lambda: None)
    st = jobs.state("k")
    assert st["ready"] is True
    assert st["decoding"] is False
    assert st["error"] is None


def test_state_is_None_for_a_clip_never_asked_for(jobs):
    assert jobs.state("never") is None


# --- failures must be reportable, not silent ------------------------------


def test_missing_camera_is_reported_with_its_type(jobs):
    def produce():
        raise FileNotFoundError("camera not found: nope")

    jobs.ensure("k", produce)
    st = jobs.state("k")
    assert st["ready"] is False
    assert st["decoding"] is False
    assert "camera not found" in st["error"]
    assert st["error_type"] == "FileNotFoundError"


def test_stub_file_is_reported_with_its_type(jobs):
    def produce():
        raise ValueError("camera 'x' has no recorded video (stub file)")

    jobs.ensure("k", produce)
    assert jobs.state("k")["error_type"] == "ValueError"


def test_unexpected_failure_is_still_terminal(jobs):
    jobs.ensure("k", lambda: (_ for _ in ()).throw(RuntimeError("ffmpeg died")))
    st = jobs.state("k")
    assert st["decoding"] is False, "a crashed job must not report decoding forever"
    assert st["error_type"] == "RuntimeError"


def test_a_failed_clip_can_be_retried(jobs):
    """A transient failure (a throttle, a torn download) must not be permanent —
    the catalog cards made exactly that mistake earlier today."""
    jobs.ensure("k", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert jobs.state("k")["error_type"] == "RuntimeError"

    jobs.ensure("k", lambda: None, retry=True)
    assert jobs.state("k")["ready"] is True


# --- one decode, not N ----------------------------------------------------


def test_second_ensure_does_not_start_a_second_decode(jobs):
    calls = []
    jobs.ensure("k", lambda: calls.append(1))
    jobs.ensure("k", lambda: calls.append(1))
    assert calls == [1], "decoded twice"


def test_concurrent_ensures_decode_once(sync_threads):
    """Three camera tiles mount at once and all ask for the same clip."""
    jobs = clips.ClipJobs()
    started = []
    gate = threading.Event()

    def produce():
        started.append(1)
        gate.wait(timeout=2)

    # Real threads here: the point is the lock, which _ImmediateThread would hide.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(clips.threading, "Thread", threading.Thread)
        for _ in range(3):
            jobs.ensure("k", produce)
        time.sleep(0.1)
        gate.set()
    assert started == [1], f"decoded {len(started)} times"


# --- the registry must not grow forever -----------------------------------


def test_registry_is_bounded(jobs):
    for i in range(clips.MAX_JOBS + 25):
        jobs.ensure(f"k{i}", lambda: None)
    assert len(jobs._jobs) <= clips.MAX_JOBS, "registry grows unbounded"


def test_pruning_keeps_the_most_recent(jobs):
    for i in range(clips.MAX_JOBS + 5):
        jobs.ensure(f"k{i}", lambda: None)
    newest = f"k{clips.MAX_JOBS + 4}"
    assert jobs.state(newest) is not None, "pruned the entry we just created"


# --- the key ---------------------------------------------------------------


def test_key_distinguishes_every_component():
    base = ("raiden", "task", "ep", "cam", "left")
    seen = {clips.job_key(*base)}
    for i in range(len(base)):
        other = list(base)
        other[i] = "different"
        k = clips.job_key(*other)
        assert k not in seen, f"key ignores component {i}"
        seen.add(k)


def test_key_is_stable():
    a = clips.job_key("s", "t", "e", "c", "left")
    b = clips.job_key("s", "t", "e", "c", "left")
    assert a == b


# --- the route ------------------------------------------------------------
#
# Driven through TestClient, not by calling the registry: gating the call away in
# the route while the helper was correct is exactly the bug that shipped in the
# catalog earlier today.


class FakeVideoSource:
    def __init__(self, raises=None):
        self.spec = {"id": "s1", "label": "S", "kind": "raiden"}
        self.raises = raises
        self.calls = []

    def video_path(self, task, episode, camera, eye):
        self.calls.append((task, episode, camera, eye))
        if self.raises:
            raise self.raises
        return "/tmp/clip.mp4"


@pytest.fixture
def video_app(monkeypatch):
    from fastapi.testclient import TestClient

    from raiden_viz import app as app_module

    monkeypatch.setattr(app_module.clips.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(app_module, "_CLIPS", clips.ClipJobs())
    src = FakeVideoSource()
    monkeypatch.setattr(app_module, "_src", lambda sid: src)
    return TestClient(app_module.app), src, app_module


URL = "/api/sources/s1/tasks/t1/episodes/e1/video/status?camera=cam0&eye=left"


def test_status_starts_the_decode_and_reports_it(video_app):
    client, src, _ = video_app
    r = client.get(URL)
    assert r.status_code == 200
    assert src.calls == [("t1", "e1", "cam0", "left")], "decode was not started"
    assert r.json()["ready"] is True          # _ImmediateThread finishes inline


def test_status_does_not_re_decode_on_every_poll(video_app):
    client, src, _ = video_app
    client.get(URL)
    client.get(URL)
    client.get(URL)
    assert len(src.calls) == 1, f"decoded {len(src.calls)} times"


def test_missing_camera_is_a_404(video_app, monkeypatch):
    from raiden_viz import app as app_module

    src = FakeVideoSource(raises=FileNotFoundError("camera not found: nope"))
    monkeypatch.setattr(app_module, "_src", lambda sid: src)
    r = video_app[0].get(URL)
    assert r.status_code == 404
    assert "camera not found" in r.text


def test_stub_camera_is_a_422(video_app, monkeypatch):
    from raiden_viz import app as app_module

    src = FakeVideoSource(raises=ValueError("camera 'cam0' has no recorded video"))
    monkeypatch.setattr(app_module, "_src", lambda sid: src)
    r = video_app[0].get(URL)
    assert r.status_code == 422


def test_unexpected_decode_failure_is_reported_not_swallowed(video_app, monkeypatch):
    from raiden_viz import app as app_module

    src = FakeVideoSource(raises=RuntimeError("ffmpeg died"))
    monkeypatch.setattr(app_module, "_src", lambda sid: src)
    r = video_app[0].get(URL)
    assert r.status_code == 500
    assert "ffmpeg died" in r.text
