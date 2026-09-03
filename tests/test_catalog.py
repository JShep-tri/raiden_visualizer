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


# --- cards survive a container (the derived tier) -------------------------


@pytest.fixture
def remote(monkeypatch, fake_s3):
    """Turn the derived tier on and wire it to the fake client."""
    from raiden_viz import cache, config

    monkeypatch.setattr(config, "DERIVED_BUCKET", "derived-bucket")
    monkeypatch.setattr(config, "DERIVED_PREFIX", "derived")
    monkeypatch.setattr(cache, "_derived_client", lambda: fake_s3)
    return fake_s3


def _card_key(sid="s1"):
    return f"derived/{catalog._cache_key(sid)}"


def test_successful_card_is_published_remotely(builder, remote):
    builder.build_deep("s1", FakeSource())
    assert _card_key() in remote.objects, "a good card must outlive the container"


def test_failed_card_is_not_published_remotely(builder, remote):
    """A restart must not inherit another container's failure."""
    builder.build_deep("s1", FakeSource(fail=True))
    assert _card_key() not in remote.objects


def test_phase1_placeholder_is_not_published_remotely(builder, remote, monkeypatch):
    """A 'building' stub must never be what a later restart restores."""
    pushed = []
    from raiden_viz import cache

    real = cache.push_remote
    monkeypatch.setattr(
        cache, "push_remote",
        lambda name, src: (pushed.append(cache.get_json(name)), real(name, src))[1],
    )
    builder.build_deep("s1", FakeSource())
    assert all(not (p or {}).get("building") for p in pushed)


def test_card_is_restored_from_remote_after_a_cold_start(builder, remote, cache_dir):
    builder.build_deep("s1", FakeSource())
    assert _card_key() in remote.objects

    # A new container: fresh builder, empty local cache, remote intact.
    for f in cache_dir.iterdir():
        f.unlink()
    fresh = catalog.CatalogBuilder()
    card = fresh.get_card("s1")
    assert card is not None, "cold start did not restore the card from the derived tier"
    assert card["built_ok"] is True
    assert card["num_episodes"] == 7


# --- staleness ------------------------------------------------------------


def test_fresh_card_is_not_refreshed(builder):
    builder.build_deep("s1", FakeSource())
    builder.start_deep("s1", FakeSource(fail=True))   # would fail if it ran
    assert _cards(builder, "s1")["built_ok"] is True


def test_stale_card_is_refreshed_in_the_background(builder):
    builder.build_deep("s1", FakeSource())
    stale = dict(_cards(builder, "s1"))
    stale["built_at"] = time.time() - (catalog._CARD_TTL_S + 1)
    builder._deep["s1"] = stale

    builder.start_deep("s1", FakeSource())
    assert _cards(builder, "s1")["built_at"] > stale["built_at"], "stale card never refreshed"


def test_card_without_built_at_refreshes_once(builder):
    """Cards written before built_at existed must not be served forever."""
    builder.build_deep("s1", FakeSource())
    legacy = dict(_cards(builder, "s1"))
    del legacy["built_at"]
    builder._deep["s1"] = legacy

    builder.start_deep("s1", FakeSource())
    assert "built_at" in _cards(builder, "s1")


def test_refresh_never_blanks_the_card(builder, monkeypatch):
    """A refresh must not publish a building=true stub over a good card, or the
    dashboard would flash empty every time a card goes stale."""
    from raiden_viz import cache

    builder.build_deep("s1", FakeSource())
    stale = dict(_cards(builder, "s1"))
    stale["built_at"] = time.time() - (catalog._CARD_TTL_S + 1)
    builder._deep["s1"] = stale

    written = []
    real = cache.put_json
    monkeypatch.setattr(
        cache, "put_json",
        lambda name, value, remote=False: (written.append(value), real(name, value, remote))[1],
    )
    builder.start_deep("s1", FakeSource())
    assert not any(v.get("building") for v in written), "refresh blanked the card"


# --- release the running flag --------------------------------------------


def test_running_flag_is_released_even_if_publishing_fails(builder, monkeypatch):
    from raiden_viz import cache

    monkeypatch.setattr(cache, "put_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk")))
    with pytest.raises(RuntimeError):
        builder.build_deep("s1", FakeSource())
    assert builder.is_running("s1") is False, "a failed publish wedged the card"


# --- startup warmup -------------------------------------------------------


def test_warmup_starts_every_available_source(monkeypatch):
    """Nothing but a user request touches /api/catalog (the LB only polls
    /api/health), so without warmup the cold-cache scan lands on a colleague."""
    from raiden_viz import app as app_module
    from raiden_viz import sources

    specs = [{"id": "a", "label": "A", "kind": "yam"}, {"id": "b", "label": "B", "kind": "yam"}]
    monkeypatch.setattr(app_module.config, "SOURCES", specs)
    monkeypatch.setattr(sources, "get_sources", lambda _s: {"a": object(), "b": object()})

    started = []
    monkeypatch.setattr(app_module._CATALOG, "start_deep", lambda sid, src: started.append(sid))

    app_module._warm_catalog()
    assert started == ["a", "b"]


def test_warmup_skips_sources_that_did_not_register(monkeypatch):
    from raiden_viz import app as app_module
    from raiden_viz import sources

    specs = [{"id": "a", "label": "A", "kind": "yam"}, {"id": "gated", "label": "G", "kind": "yam"}]
    monkeypatch.setattr(app_module.config, "SOURCES", specs)
    monkeypatch.setattr(sources, "get_sources", lambda _s: {"a": object()})

    started = []
    monkeypatch.setattr(app_module._CATALOG, "start_deep", lambda sid, src: started.append(sid))

    app_module._warm_catalog()
    assert started == ["a"]


def test_warmup_failure_never_breaks_startup(monkeypatch, caplog):
    """A container that cannot warm its cache must still serve."""
    from raiden_viz import app as app_module
    from raiden_viz import sources

    monkeypatch.setattr(
        sources, "get_sources",
        lambda _s: (_ for _ in ()).throw(RuntimeError("S3 unreachable")),
    )
    with caplog.at_level("ERROR"):
        app_module._warm_catalog()          # must not raise
    assert any("warmup failed" in r.message for r in caplog.records)


def test_warmup_can_be_disabled(monkeypatch):
    """RAIDEN_WARM_CATALOG=0 for a laptop checkout."""
    from fastapi.testclient import TestClient

    from raiden_viz import app as app_module

    calls = []
    monkeypatch.setattr(app_module, "_warm_catalog", lambda: calls.append(1))

    monkeypatch.setattr(app_module.config, "WARM_CATALOG_ON_START", False)
    with TestClient(app_module.app):
        pass
    assert calls == []

    monkeypatch.setattr(app_module.config, "WARM_CATALOG_ON_START", True)
    with TestClient(app_module.app):
        pass
    assert calls == [1]


# --- through the ENDPOINT, not the builder --------------------------------
#
# The builder tests above all call start_deep directly, which is exactly why they
# passed while /api/catalog was gating the call away: `get_catalog` only invoked
# start_deep for a MISSING or BUILDING card, so a failed card (building=false) was
# served forever and a stale card never refreshed. Drive the route.


@pytest.fixture
def catalog_app(monkeypatch, cache_dir):
    """The app wired to a fresh builder and one fake source."""
    from fastapi.testclient import TestClient

    from raiden_viz import app as app_module
    from raiden_viz import catalog as catalog_mod
    from raiden_viz import sources

    monkeypatch.setattr(catalog_mod.threading, "Thread", _ImmediateThread)
    builder = catalog_mod.CatalogBuilder()
    monkeypatch.setattr(app_module, "_CATALOG", builder)
    monkeypatch.setattr(app_module.config, "SOURCES", [{"id": "s1", "label": "Fake", "kind": "yam"}])
    monkeypatch.setattr(sources, "get_sources", lambda _s: {"s1": FakeSource()})
    # No context manager: lifespan (and therefore warmup) deliberately does not run,
    # so these tests exercise the request path alone.
    return TestClient(app_module.app), builder


def test_endpoint_retries_a_failed_card(catalog_app):
    """The prod regression: cards failed before a cross-account grant landed, and no
    page load ever rebuilt them."""
    client, builder = catalog_app
    builder.build_deep("s1", FakeSource(fail=True))
    assert builder.get_card("s1")["built_ok"] is False

    aged = dict(builder.get_card("s1"))
    aged["failed_at"] = time.time() - (catalog._RETRY_COOLDOWN_S + 1)
    builder._deep["s1"] = aged

    assert client.get("/api/catalog").status_code == 200
    assert builder.get_card("s1")["built_ok"] is True, "endpoint did not retry the failed card"

    # And the next request serves the rebuilt card.
    card = client.get("/api/catalog").json()["datasets"][0]
    assert card["built_ok"] is True
    assert card["num_episodes"] == 7


def test_endpoint_does_not_retry_inside_the_cooldown(catalog_app):
    client, builder = catalog_app
    builder.build_deep("s1", FakeSource(fail=True))
    first = builder.get_card("s1")["failed_at"]

    assert client.get("/api/catalog").status_code == 200
    assert builder.get_card("s1")["built_ok"] is False
    assert builder.get_card("s1")["failed_at"] == first, "retried inside the cooldown"


def test_endpoint_refreshes_a_stale_card(catalog_app):
    """Cards persist in the derived tier now, so without this they would be served
    forever and never pick up newly uploaded episodes."""
    client, builder = catalog_app
    builder.build_deep("s1", FakeSource())
    aged = dict(builder.get_card("s1"))
    aged["built_at"] = time.time() - (catalog._CARD_TTL_S + 1)
    builder._deep["s1"] = aged

    assert client.get("/api/catalog").status_code == 200
    assert builder.get_card("s1")["built_at"] > aged["built_at"], "stale card never refreshed"


def test_endpoint_leaves_a_fresh_card_alone(catalog_app):
    client, builder = catalog_app
    builder.build_deep("s1", FakeSource())
    before = builder.get_card("s1")["built_at"]

    assert client.get("/api/catalog").status_code == 200
    assert builder.get_card("s1")["built_at"] == before, "rebuilt a fresh card"


def test_endpoint_serves_a_stub_and_starts_a_first_build(catalog_app):
    client, builder = catalog_app
    assert builder.get_card("s1") is None

    data = client.get("/api/catalog").json()
    assert data["datasets"][0]["label"] == "Fake"
    assert builder.get_card("s1")["built_ok"] is True, "first build never started"


# --- logging is actually configured --------------------------------------


def test_configure_logging_lets_INFO_through(monkeypatch):
    """Without this the raiden_viz logger has no handler, so Python's lastResort
    fallback drops everything below WARNING — which silently swallowed the catalog
    warmup confirmation while letting logger.exception through."""
    import logging

    from raiden_viz import app as app_module

    log = logging.getLogger("raiden_viz")
    monkeypatch.setattr(log, "handlers", [])          # simulate an unconfigured app
    monkeypatch.setattr(log, "level", logging.NOTSET)
    assert log.getEffectiveLevel() == logging.WARNING, "expected the lastResort default"

    app_module._configure_logging()

    assert log.handlers, "no handler installed"
    assert log.getEffectiveLevel() == logging.INFO
    # catalog.py logs on a CHILD logger, which must inherit this.
    assert logging.getLogger("raiden_viz.catalog").isEnabledFor(logging.INFO)


def test_configure_logging_is_idempotent(monkeypatch):
    import logging

    from raiden_viz import app as app_module

    log = logging.getLogger("raiden_viz")
    monkeypatch.setattr(log, "handlers", [])
    app_module._configure_logging()
    app_module._configure_logging()
    assert len(log.handlers) == 1, "duplicate handlers would double every log line"


def test_configure_logging_honours_the_env_override(monkeypatch):
    import logging

    from raiden_viz import app as app_module

    log = logging.getLogger("raiden_viz")
    monkeypatch.setattr(log, "handlers", [])
    monkeypatch.setattr(app_module.config, "LOG_LEVEL", "WARNING")
    app_module._configure_logging()
    assert log.getEffectiveLevel() == logging.WARNING


# --- sampled stats: fast, but the numbers must stay honest ----------------


class BigSource:
    """A source large enough that the deep pass subsamples it."""

    TOTAL = 200_000
    SAMPLE = 1_200
    DUR = 40.0                      # seconds per episode, uniform for easy maths

    def __init__(self):
        self.spec = {"id": "big", "label": "Big", "kind": "yam"}
        self.bucket = "b"
        self.prefix = "p"
        self.full_calls = []

    def overview(self):
        # Exact per-task counts, sorted descending, as the real overview() returns.
        tasks = [{"task": f"t{i}", "episodes": n, "_eps": ["x"] * n}
                 for i, n in enumerate([90_000, 60_000, 50_000])]
        return {"num_tasks": 3, "num_episodes": self.TOTAL, "stations": [], "tasks": tasks}

    def stats(self, full=False):
        self.full_calls.append(full)
        eps = [{"task": "t0", "duration_s": self.DUR} for _ in range(self.SAMPLE)]
        return {"num_episodes": self.SAMPLE, "total_episodes": self.TOTAL,
                "scanned": self.SAMPLE, "sampled": True, "episodes": eps}


def test_catalog_asks_for_a_SAMPLED_pass(builder):
    """Regression guard: full=True was ~51 minutes on the largest source."""
    src = BigSource()
    builder.build_deep("big", src)
    assert src.full_calls == [False], f"deep pass asked for full={src.full_calls}"


def test_sampled_hours_are_extrapolated_not_summed(builder):
    """Summing a 1,200-episode sample of a 200,000-episode source would under-report
    the card's headline duration by ~99.4%."""
    src = BigSource()
    builder.build_deep("big", src)
    card = builder.get_card("big")

    summed = src.SAMPLE * src.DUR / 3600.0                 # the wrong answer
    expected = src.TOTAL * src.DUR / 3600.0                 # mean x true count
    assert card["sampled"] is True
    assert card["total_hours"] == round(expected, 1)
    assert card["total_hours"] > summed * 100, "hours look summed, not extrapolated"


def test_top_tasks_stay_EXACT_under_sampling(builder):
    """They come from the listing pass, not the sampled records — otherwise a task
    with 90,000 episodes would report its sample count instead."""
    src = BigSource()
    builder.build_deep("big", src)
    top = builder.get_card("big")["top_tasks"]

    assert [t["task"] for t in top] == ["t0", "t1", "t2"]
    assert [t["episodes"] for t in top] == [90_000, 60_000, 50_000]


def test_top_tasks_never_leak_the_private_episode_list(builder):
    builder.build_deep("big", BigSource())
    for t in builder.get_card("big")["top_tasks"]:
        assert set(t) == {"task", "episodes"}, f"unexpected keys: {sorted(t)}"


def test_small_source_hours_are_summed_exactly(builder):
    """A source under STATS_MAX is read whole, so nothing is estimated."""

    class SmallSource(BigSource):
        def stats(self, full=False):
            self.full_calls.append(full)
            eps = [{"task": "t0", "duration_s": 60.0} for _ in range(3)]
            return {"num_episodes": 3, "total_episodes": 3, "scanned": 3,
                    "sampled": False, "episodes": eps}

    builder.build_deep("small", SmallSource())
    card = builder.get_card("small")
    assert card["sampled"] is False
    # 3 x 60s = 180s = 0.05h, rounded to one decimal by build_deep.
    assert card["total_hours"] == 0.1


# --- scan warmup ----------------------------------------------------------
#
# The full per-episode scan backs the episode filter, and nothing started it at
# boot: a USER clicked #filter-scan-btn and then watched it, which on the largest
# source is ~51 minutes. Deploying at midnight is only useful if the scan happens
# at midnight too.


class ScannableSource:
    def __init__(self, sid="s1", fails=False):
        self.spec = {"id": sid, "label": "S", "kind": "yam"}
        self.scans = 0
        self.fails = fails

    def scan_start(self):
        if self.fails:
            raise RuntimeError("listing blew up")
        self.scans += 1
        return {"running": True}


def test_scan_warmup_starts_every_available_source(monkeypatch):
    from raiden_viz import app as app_module
    from raiden_viz import sources

    a, b = ScannableSource("a"), ScannableSource("b")
    monkeypatch.setattr(app_module.config, "SOURCES",
                        [{"id": "a", "label": "A", "kind": "yam"},
                         {"id": "b", "label": "B", "kind": "yam"}])
    monkeypatch.setattr(sources, "get_sources", lambda _s: {"a": a, "b": b})

    app_module._warm_scans()
    assert (a.scans, b.scans) == (1, 1)


def test_scan_warmup_skips_unregistered_sources(monkeypatch):
    from raiden_viz import app as app_module
    from raiden_viz import sources

    a = ScannableSource("a")
    monkeypatch.setattr(app_module.config, "SOURCES",
                        [{"id": "a", "label": "A", "kind": "yam"},
                         {"id": "gated", "label": "G", "kind": "yam"}])
    monkeypatch.setattr(sources, "get_sources", lambda _s: {"a": a})
    app_module._warm_scans()          # must not KeyError on the gated one
    assert a.scans == 1


def test_one_source_failing_does_not_stop_the_rest(monkeypatch, caplog):
    """A container that cannot scan one source must still scan the others, and
    must still serve."""
    from raiden_viz import app as app_module
    from raiden_viz import sources

    bad, good = ScannableSource("bad", fails=True), ScannableSource("good")
    monkeypatch.setattr(app_module.config, "SOURCES",
                        [{"id": "bad", "label": "B", "kind": "yam"},
                         {"id": "good", "label": "G", "kind": "yam"}])
    monkeypatch.setattr(sources, "get_sources", lambda _s: {"bad": bad, "good": good})

    with caplog.at_level("ERROR"):
        app_module._warm_scans()      # must not raise
    assert good.scans == 1, "one bad source stopped the others"
    assert any("scan warmup" in r.message for r in caplog.records)


def test_scan_warmup_is_OFF_by_default():
    """~51 minutes of S3 work at every boot is right for a deployed container and
    wrong for a laptop, so this one is opt-in — unlike the catalog warmup."""
    from raiden_viz import config

    assert config.WARM_SCANS_ON_START is False


def test_lifespan_honours_the_scan_warmup_flag(monkeypatch):
    from fastapi.testclient import TestClient

    from raiden_viz import app as app_module

    calls = []
    monkeypatch.setattr(app_module, "_warm_scans", lambda: calls.append(1))
    monkeypatch.setattr(app_module, "_warm_catalog", lambda: None)

    monkeypatch.setattr(app_module.config, "WARM_SCANS_ON_START", False)
    with TestClient(app_module.app):
        pass
    assert calls == []

    monkeypatch.setattr(app_module.config, "WARM_SCANS_ON_START", True)
    with TestClient(app_module.app):
        pass
    assert calls == [1]
