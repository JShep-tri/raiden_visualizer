"""Background clip-decode jobs.

Decoding one camera costs a 200-880 MB download plus an ffmpeg pass — measured at
~2.5 minutes for a 100 MB .svo2. Doing that inside the request does not work behind
a load balancer: the ALB's 60s idle timeout severs the browser first, and a <video>
element renders the truncated response as "Could not decode this stream" — a decode
error for what is actually a timeout, while the decode goes on to succeed on the
server and lands in the derived tier unused.

So the work moves off the request. A job runs it in a daemon thread; the route
reports progress until the clip is ready, then serves it as before. This is the same
shape as the catalog's deep builds and the per-source scan.

Note this registry holds no clip DATA — only whether a decode is running, done or
failed. The bytes live in the cache tiers (see cache.py), which is what makes a job
safe to forget: forgetting one costs a re-check, never a re-decode.
"""

from __future__ import annotations

import hashlib
import logging
import threading

log = logging.getLogger(__name__)

# Cap on remembered jobs. One entry per (episode, camera, eye) ever viewed would
# otherwise grow without bound in a long-lived container. Terminal entries are
# evicted oldest-first; in-flight ones are never evicted.
MAX_JOBS = 512


def job_key(sid: str, task: str, episode: str, camera: str, eye: str) -> str:
    """Stable id for one decodable clip.

    Keyed on the REQUEST parameters rather than the cache filename on purpose: the
    filename is derived differently by each source adapter (raiden keys on the .svo2
    etag, lerobot folds a time window in), and asking an adapter for it means asking
    it to do the work. Joined with NUL so no two component splits can collide.
    """
    raw = "\x00".join([sid, task, episode, camera, eye])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


class ClipJobs:
    """Registry of in-flight and finished clip decodes."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def state(self, key: str) -> dict | None:
        """Current state, or None if this clip was never asked for. A copy, so a
        caller cannot mutate the registry by editing what it reads."""
        with self._lock:
            job = self._jobs.get(key)
            return dict(job) if job is not None else None

    def ensure(self, key: str, produce, retry: bool = False) -> dict:
        """Start a decode for ``key`` if one is not already running or finished, and
        return the resulting state.

        ``produce`` is called with no arguments in a background thread; its return
        value is ignored, because the clip's bytes end up in the cache and the route
        re-resolves them there. ``retry`` re-runs a job that previously FAILED — a
        transient error (a throttle, a torn download) must not be permanent.
        """
        with self._lock:
            job = self._jobs.get(key)
            if job is not None and not (retry and job["error"] is not None):
                return dict(job)
            self._jobs[key] = {"decoding": True, "ready": False,
                               "error": None, "error_type": None}
            self._prune_locked()
        threading.Thread(target=self._run, args=(key, produce), daemon=True).start()
        return self.state(key) or {"decoding": True, "ready": False,
                                   "error": None, "error_type": None}

    def _run(self, key: str, produce) -> None:
        error = error_type = None
        try:
            produce()
        except Exception as e:
            error, error_type = str(e), type(e).__name__
            # LOG it. The state carries the message, but a decode that dies with
            # nothing in the log is how the catalog failures stayed invisible.
            log.exception("clip decode failed for %s", key)
        # Publish terminal state under the lock so a poll can never observe
        # decoding=false alongside a stale ready/error.
        with self._lock:
            job = self._jobs.get(key)
            if job is not None:          # may have been pruned mid-decode
                job.update(decoding=False, ready=error is None,
                           error=error, error_type=error_type)

    def _prune_locked(self) -> None:
        """Evict finished jobs, oldest first, until back under the cap. Never evicts
        an in-flight job: losing that would let a second request start a duplicate
        decode of the same clip."""
        while len(self._jobs) > MAX_JOBS:
            for key, job in self._jobs.items():
                if not job["decoding"]:
                    del self._jobs[key]
                    break
            else:
                return                   # everything in flight; leave it alone
