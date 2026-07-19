"""Publication fencing for detached /api/models catalog rebuilds."""

from __future__ import annotations

import json
import threading


def _catalog(owner: str) -> dict:
    return {
        "active_provider": owner,
        "default_model": f"{owner}/model",
        "configured_model_badges": {},
        "groups": [],
        "aliases": {},
    }


def test_invalidated_detached_build_cannot_overwrite_newer_publication(
    monkeypatch, tmp_path
):
    """A timed-out build must lose publication rights after invalidation.

    Build A remains detached after the foreground budget expires.  Invalidate
    it, let build B publish, then release A.  A must not overwrite B's memory,
    provenance, or disk snapshot, and must not disturb the current build owner.
    """
    from api import config

    cache_path = tmp_path / "models_cache.json"
    source_fingerprint = {"test": "generation-fence"}
    build_a_started = threading.Event()
    release_build_a = threading.Event()
    workers: list[threading.Thread] = []
    call_count = 0
    call_lock = threading.Lock()

    real_thread = threading.Thread

    def tracking_thread(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        if kwargs.get("name") == "models-catalog-rebuild":
            workers.append(thread)
        return thread

    def rebuild(_builder):
        nonlocal call_count
        with call_lock:
            call_count += 1
            build_number = call_count
        if build_number == 1:
            build_a_started.set()
            assert release_build_a.wait(timeout=5), "test never released build A"
            return _catalog("build-a")
        assert build_number == 2
        return _catalog("build-b")

    monkeypatch.setattr(config.threading, "Thread", tracking_thread)
    monkeypatch.setattr(config, "_LIVE_REBUILD_BUDGET_SECONDS", 0.05)
    monkeypatch.setattr(config, "_invoke_models_rebuild", rebuild)
    monkeypatch.setattr(config, "_load_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(config, "_load_stale_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(
        config, "_static_models_catalog_without_live_probes", lambda: _catalog("fallback")
    )
    monkeypatch.setattr(config, "_get_models_cache_path", lambda: cache_path)
    monkeypatch.setattr(config, "_models_cache_source_fingerprint", lambda: source_fingerprint)
    monkeypatch.setattr(config, "_current_webui_version", lambda: None)

    first = config.get_available_models()
    assert first["active_provider"] == "fallback"
    assert build_a_started.is_set()

    config.invalidate_models_cache()
    second = config.get_available_models()
    assert second["active_provider"] == "build-b"
    assert config._available_models_cache["active_provider"] == "build-b"
    assert config._models_cache_provenance[0]["active_provider"] == "build-b"
    assert json.loads(cache_path.read_text())["active_provider"] == "build-b"

    release_build_a.set()
    workers[0].join(timeout=5)
    assert not workers[0].is_alive(), "detached build A did not finish"

    assert config._available_models_cache["active_provider"] == "build-b"
    assert config._models_cache_provenance[0]["active_provider"] == "build-b"
    assert json.loads(cache_path.read_text())["active_provider"] == "build-b"
    assert config._cache_build_in_progress is False


def test_revoked_synchronous_build_returns_without_publishing(monkeypatch, tmp_path):
    """A reentrantly invalidated sync build may return, but cannot publish."""
    from api import config

    cache_path = tmp_path / "models_cache.json"
    source_fingerprint = {"test": "sync-generation-fence"}
    call_count = 0

    def rebuild(_builder):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            config.invalidate_models_cache()
            newer = config.get_available_models()
            assert newer["active_provider"] == "build-b"
            return _catalog("build-a")
        assert call_count == 2
        return _catalog("build-b")

    monkeypatch.setattr(config, "_LIVE_REBUILD_BUDGET_SECONDS", 0)
    monkeypatch.setattr(config, "_invoke_models_rebuild", rebuild)
    monkeypatch.setattr(config, "_load_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(config, "_load_stale_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(config, "_get_models_cache_path", lambda: cache_path)
    monkeypatch.setattr(config, "_models_cache_source_fingerprint", lambda: source_fingerprint)
    monkeypatch.setattr(config, "_current_webui_version", lambda: None)

    local_result = config.get_available_models()

    assert local_result["active_provider"] == "build-a"
    assert config._available_models_cache["active_provider"] == "build-b"
    assert config._models_cache_provenance[0]["active_provider"] == "build-b"
    assert json.loads(cache_path.read_text())["active_provider"] == "build-b"
    assert config._cache_build_in_progress is False
