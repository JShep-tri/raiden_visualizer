import importlib


def test_disabled_sources_are_dropped(monkeypatch):
    monkeypatch.setenv("RAIDEN_DISABLED_SOURCES", "xdof_zed,raiden")
    from raiden_viz import config

    reloaded = importlib.reload(config)
    try:
        ids = {s["id"] for s in reloaded.SOURCES}
        assert "xdof_zed" not in ids
        assert "raiden" not in ids
        assert "worldengine" in ids, "unlisted sources must survive"
    finally:
        # Restore the real registry for every other test in the session.
        monkeypatch.delenv("RAIDEN_DISABLED_SOURCES")
        importlib.reload(config)


def test_no_sources_dropped_by_default():
    from raiden_viz import config

    ids = {s["id"] for s in config.SOURCES}
    assert "xdof_zed" in ids
