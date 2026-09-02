"""Catalog card lifecycle: what start_deep will and will not rebuild.

The interesting cases are all failure cases. A card that fails is cached with
building=false, so the naive "skip anything not building" check made a transient
error permanent: the frontend stops polling once nothing is building, and
start_deep refused to retry, so the card stayed dead until the process restarted.
That actually happened — a cross-account S3 grant landed 18 hours after the cards
were built, and the dashboard kept serving the stale failures.
"""

import time

import pytest

from raiden_viz import catalog


class _ImmediateThread:
    """Runs the target inline so start_deep is synchronous under test."""

    def __init__(self, target=None, args=(), daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


class FakeSource:
    """Minimal source: enough for cheap_card + stats, or raises on demand."""

    def __init__(self, sid="s1", fail=False):
        self.spec = {"id": sid, "label": "Fake", "kind": "yam"}
        self.bucket = "fake-bucket"
        self.prefix = "fake/prefix"
        self._fail = fail

    def overview(self):
        if self._fail:
            raise RuntimeError("AccessDenied: no resource-based policy allows")
        return {"num_tasks": 2, "num_episodes": 7, "tasks": []}

    def stats(self, full=False):
        return {"episodes": [], "total_episodes": 0, "scanned": 0, "sampled": False}


@pytest.fixture
def sync_threads(monkeypatch):
    monkeypatch.setattr(catalog.threading, "Thread", _ImmediateThread)


@pytest.fixture
def builder(cache_dir, sync_threads):
    return catalog.CatalogBuilder()


def _cards(builder, sid):
    return builder.get_card(sid)


# --- the error card itself -------------------------------------------------


def test_failed_build_is_logged(builder, caplog):
    """A dead build must leave a trace in the app log, not just in the card."""
    with caplog.at_level("ERROR"):
        builder.build_deep("s1", FakeSource(fail=True))
    assert any("deep build failed" in r.message for r in caplog.records)
    assert any(r.exc_info for r in caplog.records), "should log the traceback"


def test_failed_card_keeps_bucket_and_prefix(builder):
    """Otherwise the card footer renders 's3://undefined/undefined'."""
    builder.build_deep("s1", FakeSource(fail=True))
    card = _cards(builder, "s1")
    assert card["built_ok"] is False
    assert card["bucket"] == "fake-bucket"
    assert card["prefix"] == "fake/prefix"
    assert "AccessDenied" in card["error"]
    assert card["failed_at"] > 0


def test_error_handler_survives_a_source_with_no_bucket(builder):
    """The error path must not itself throw on a half-built source object."""
    src = FakeSource(fail=True)
    del src.bucket
    builder.build_deep("s1", src)
    assert _cards(builder, "s1")["bucket"] is None


# --- what start_deep decides to rebuild -----------------------------------


def test_successful_card_is_not_rebuilt(builder):
    builder.build_deep("s1", FakeSource())
    assert _cards(builder, "s1")["built_ok"] is True

    later = FakeSource(fail=True)          # would fail if it ran
    builder.start_deep("s1", later)
    assert _cards(builder, "s1")["built_ok"] is True, "a good card was rebuilt"


def test_failed_card_is_not_retried_during_cooldown(builder):
    builder.build_deep("s1", FakeSource(fail=True))
    first = _cards(builder, "s1")["failed_at"]

    builder.start_deep("s1", FakeSource())  # would succeed if it ran
    card = _cards(builder, "s1")
    assert card["built_ok"] is False, "retried inside the cooldown"
    assert card["failed_at"] == first


def test_failed_card_is_retried_after_cooldown(builder):
    builder.build_deep("s1", FakeSource(fail=True))
    stale = dict(_cards(builder, "s1"))
    stale["failed_at"] = time.time() - (catalog._RETRY_COOLDOWN_S + 1)
    builder._deep["s1"] = stale

    builder.start_deep("s1", FakeSource())
    assert _cards(builder, "s1")["built_ok"] is True, "cooled-off failure never retried"


def test_legacy_failed_card_without_failed_at_is_retried(builder):
    """The regression that stranded the real dashboard: cards cached before
    failed_at existed must not be treated as permanently fresh failures."""
    builder.build_deep("s1", FakeSource(fail=True))
    legacy = dict(_cards(builder, "s1"))
    del legacy["failed_at"]
    builder._deep["s1"] = legacy

    builder.start_deep("s1", FakeSource())
    assert _cards(builder, "s1")["built_ok"] is True


def test_force_rebuilds_a_good_card(builder):
    builder.build_deep("s1", FakeSource())
    builder.start_deep("s1", FakeSource(fail=True), force=True)
    assert _cards(builder, "s1")["built_ok"] is False, "force did not rebuild"
