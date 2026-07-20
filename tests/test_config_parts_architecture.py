"""Architecture contracts for the real :mod:`api.config` package."""

import threading
from dataclasses import replace
from pathlib import Path

import api.config as config
from api import agent_cache, runtime_state, session_state, stream_channel
from api.config import (
    catalog_state,
    environment,
    gateway_capabilities,
    hooks,
    io,
    media_types,
    model_cache,
    model_catalog,
    model_resolution,
    reasoning,
    reasoning_identity,
    reasoning_policy,
    reasoning_probe,
    session_limits,
    snapshot,
    toolsets,
)


def test_package_entrypoint_has_no_binding_dispatch():
    source = Path(config.__file__).read_text(encoding="utf-8")
    assert "FunctionType" not in source
    assert "ContextVar" not in source
    assert "_binding" not in source


def test_config_reexports_real_owner_functions():
    assert config.get_config is io.get_config
    assert config._get_models_cache_path is model_cache._get_models_cache_path
    assert config.get_available_models is model_catalog.get_available_models
    assert config.resolve_model_provider is model_resolution.resolve_model_provider
    assert (
        config.resolve_model_reasoning_efforts
        is reasoning.resolve_model_reasoning_efforts
    )
    assert (
        config.coerce_reasoning_effort_for_model
        is reasoning_policy.coerce_reasoning_effort_for_model
    )
    assert (
        config._candidate_supports_reasoning
        is reasoning_identity.candidate_supports_reasoning
    )
    assert (
        config._lmstudio_model_reasoning_options
        is reasoning_probe.lmstudio_model_reasoning_options
    )
    assert config.get_gateway_caps is gateway_capabilities.get_gateway_caps
    assert config.thread_env_scope is environment.thread_env_scope
    assert config.MAX_UPLOAD_BYTES is media_types.MAX_UPLOAD_BYTES
    assert config.resolve_cli_toolsets is toolsets.resolve_cli_toolsets
    assert config.get_sessions_cache_max is session_limits.get_sessions_cache_max


def test_config_entrypoint_uses_public_runtime_and_channel_adapters():
    source = Path(config.__file__).read_text(encoding="utf-8")
    assert "from api.runs.channels" not in source
    assert "from api.runs.runtime_state" not in source
    assert "from api.sessions.lifecycle" not in source
    assert config.RUNTIME_STATE is runtime_state.RUNTIME_STATE
    assert config.ACTIVE_RUNS is runtime_state.ACTIVE_RUNS
    assert config.StreamChannel is stream_channel.StreamChannel
    assert config.SESSIONS is session_state.SESSIONS
    assert config.SESSION_AGENT_LOCKS is session_state.SESSION_AGENT_LOCKS
    assert config.SESSION_AGENT_CACHE is agent_cache.SESSION_AGENT_CACHE
    assert config.SESSION_AGENT_CACHE_LOCK is agent_cache.SESSION_AGENT_CACHE_LOCK
    assert config._evict_session_agent is agent_cache.evict_session_agent


def test_agent_cache_owner_honors_legacy_facade_replacements(monkeypatch):
    replacement_cache = {}
    replacement_lock = threading.Lock()
    monkeypatch.setattr(config, "SESSION_AGENT_CACHE", replacement_cache)
    monkeypatch.setattr(config, "SESSION_AGENT_CACHE_LOCK", replacement_lock)
    monkeypatch.setattr(config, "SESSION_AGENT_CACHE_MAX", 3)

    with agent_cache.locked_agent_cache() as live_cache:
        assert live_cache is replacement_cache
        live_cache["session"] = (object(), "signature")

    assert config.SESSION_AGENT_CACHE["session"][1] == "signature"
    assert agent_cache.agent_cache_max() == 3


def test_session_lock_owner_preserves_identity_across_rotation():
    old_session_id = "config-owner-old"
    new_session_id = "config-owner-new"
    held_lock = session_state.session_agent_lock(old_session_id)
    try:
        session_state.alias_session_agent_lock(
            old_session_id,
            new_session_id,
            held_lock,
        )
        assert config._get_session_agent_lock(old_session_id) is held_lock
        assert config._get_session_agent_lock(new_session_id) is held_lock
    finally:
        with session_state.SESSION_AGENT_LOCKS_LOCK:
            session_state.SESSION_AGENT_LOCKS.pop(old_session_id, None)
            session_state.SESSION_AGENT_LOCKS.pop(new_session_id, None)


def test_model_catalog_state_has_one_canonical_owner():
    state = catalog_state.MODEL_CATALOG_STATE
    assert model_cache.MODEL_CATALOG_STATE is state
    assert model_catalog.MODEL_CATALOG_STATE is state
    assert state.cache_build_cv._lock is state.available_models_cache_lock


def test_config_foundation_has_no_profile_or_provider_reverse_imports():
    root = Path(config.__file__).parent
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in root.glob("*.py")
        if path.name not in {"__init__.py"}
    )
    assert "from api.profiles" not in sources
    assert "from api.providers" not in sources


def test_snapshot_resolves_profile_paths_once(monkeypatch, tmp_path):
    home = tmp_path / "profiles" / "research"
    catalog_state.MODEL_CATALOG_STATE.models_cache_path = tmp_path / "models_cache.json"
    resolved = snapshot.resolve_config_snapshot(
        profile_home=home,
        config_data={"model": {"provider": "openai"}},
    )
    assert resolved.profile_name == "research"
    assert resolved.config_path == home / "config.yaml"
    assert resolved.auth_store_path == home / "auth.json"
    assert resolved.models_cache_path == tmp_path / "models_cache.research.json"
    assert resolved.config["model"]["provider"] == "openai"


def test_snapshot_keeps_profile_home_when_config_path_is_overridden(
    monkeypatch, tmp_path
):
    home = tmp_path / "profiles" / "research"
    external_config = tmp_path / "overrides" / "config.yaml"
    current_hooks = hooks.get_config_runtime_hooks()
    monkeypatch.setattr(
        hooks,
        "_runtime_hooks",
        replace(
            current_hooks,
            active_profile_name=lambda: "research",
            active_profile_home=lambda: home,
        ),
    )
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(external_config))

    resolved = snapshot.resolve_config_snapshot(config_data={})

    assert resolved.profile_name == "research"
    assert resolved.hermes_home == home
    assert resolved.auth_store_path == home / "auth.json"
    assert resolved.config_path == external_config
