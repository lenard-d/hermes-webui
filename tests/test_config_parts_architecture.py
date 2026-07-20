"""Architecture contracts for the real :mod:`api.config` package."""

from dataclasses import replace
from pathlib import Path

import api.config as config
from api.config import catalog_state, hooks, io, model_cache, model_catalog, snapshot


def test_package_entrypoint_has_no_binding_dispatch():
    source = Path(config.__file__).read_text(encoding="utf-8")
    assert "FunctionType" not in source
    assert "ContextVar" not in source
    assert "_binding" not in source


def test_config_reexports_real_owner_functions():
    assert config.get_config is io.get_config
    assert config._get_models_cache_path is model_cache._get_models_cache_path
    assert config.get_available_models is model_catalog.get_available_models


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
