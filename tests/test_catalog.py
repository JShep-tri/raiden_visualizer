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
