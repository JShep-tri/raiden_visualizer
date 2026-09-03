"""overview() is memoised.

It walks every task listing every episode, and nothing cached it — so a 129k-episode
source paid roughly 130 paginated LIST calls, cross-region, on EVERY dataset click.
It also backs cheap_card, so the catalog paid it per card build too.
"""

import time

import pytest

from raiden_viz import config, sources


class CountingSource(sources.Source):
    """Counts how many times the underlying listing work actually happens."""

    def __init__(self, spec=None):
        super().__init__(spec or {"id": "c", "label": "C", "kind": "raiden",
                                  "bucket": "b", "prefix": "p"})
        self.list_calls = 0

    def list_tasks(self):
        self.list_calls += 1
        return ["t1", "t2"]

    def list_episodes(self, task):
        return [f"{task}_ep{i}" for i in range(3)]

    def _add_collection_span(self, per_task):
        return None                      # skips the per-task metadata probes


@pytest.fixture
def src():
    return CountingSource()


def test_overview_is_computed_once_across_repeated_calls(src):
    first = src.overview()
    second = src.overview()
    assert src.list_calls == 1, f"re-listed {src.list_calls} times"
    assert second["num_episodes"] == first["num_episodes"] == 6


def test_overview_recomputes_once_stale(src, monkeypatch):
    monkeypatch.setattr(config, "OVERVIEW_TTL_S", 0)
    src.overview()
    time.sleep(0.01)
    src.overview()
    assert src.list_calls == 2, "a stale overview was served forever"


def test_each_source_caches_independently():
    a, b = CountingSource(), CountingSource()
    a.overview()
    a.overview()
    b.overview()
    assert a.list_calls == 1 and b.list_calls == 1


def test_a_caller_mutating_the_result_cannot_poison_the_cache(src):
    """The /overview route does `ov["region"] = ...` on what it gets back."""
    got = src.overview()
    got["region"] = "us-west-2"
    got["num_episodes"] = 999999
    again = src.overview()
    assert "region" not in again, "caller's key leaked into the cache"
    assert again["num_episodes"] == 6, "caller's mutation corrupted the cache"


def test_the_route_still_reports_region(monkeypatch):
    """Regression guard for the mutation the copy is protecting against."""
    from fastapi.testclient import TestClient

    from raiden_viz import app as app_module

    monkeypatch.setattr(app_module, "_src", lambda sid: CountingSource())
    r = TestClient(app_module.app).get("/api/sources/c/overview")
    assert r.status_code == 200
    assert r.json()["region"] == config.AWS_REGION
