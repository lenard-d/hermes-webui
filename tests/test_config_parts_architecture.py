"""Architecture contract for the importable ``api.config_parts`` slices."""

import subprocess
import sys

import api.config as config
from api.config_parts import (
    config_io,
    model_settings,
    model_reasoning,
    path_env,
    provider_discovery,
    provider_routing,
    settings_persistence,
)


def test_model_settings_imports_without_importing_config_facade():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import api.config_parts.model_settings; "
            "assert 'api.config' not in sys.modules",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_config_reexports_config_domain_implementations():
    assert config.get_config is config_io.get_config
    assert config._save_yaml_config_file is config_io._save_yaml_config_file
    assert config._discover_agent_dir is path_env._discover_agent_dir
    assert config.resolve_default_workspace is path_env.resolve_default_workspace
    assert config._resolve_provider_alias is provider_discovery._resolve_provider_alias
    assert config._configured_model_ids is provider_discovery._configured_model_ids
    assert config._parse_provider_qualified_model_id is (
        provider_routing._parse_provider_qualified_model_id
    )
    assert config._get_provider_cfg is provider_routing._get_provider_cfg
    assert config._normalize_appearance is settings_persistence._normalize_appearance
    assert config._read_raw_settings_file is settings_persistence._read_raw_settings_file
    assert config.load_settings is settings_persistence.load_settings
    assert config.save_settings is settings_persistence.save_settings
    assert config._atomic_write_settings_text is (
        settings_persistence._atomic_write_settings_text
    )
    assert config.parse_reasoning_effort is model_reasoning.parse_reasoning_effort
    assert config.resolve_model_reasoning_efforts is (
        model_reasoning.resolve_model_reasoning_efforts
    )
    assert config.coerce_reasoning_effort_for_model is (
        model_reasoning.coerce_reasoning_effort_for_model
    )
    assert config.get_reasoning_status is model_reasoning.get_reasoning_status
    model_settings_exports = (
        "_parse_positive_int_config_value",
        "get_max_tokens_status",
        "set_max_tokens",
        "set_reasoning_display",
        "set_reasoning_effort",
        "_public_advanced_model_options",
        "_is_openai_family_provider",
        "_normalize_openai_family_model_id",
        "_legacy_openai_service_tier_overrides",
        "_resolve_main_model_fast_mode_overrides",
        "_main_model_supports_service_tier",
        "_model_supports_fast_tier_for_provider",
        "_annotate_fast_tier_model_groups",
        "_public_main_service_tier",
        "_main_model_request_overrides",
        "_apply_advanced_model_options",
        "set_hermes_default_model",
        "AUXILIARY_TASK_CATALOG",
        "AUX_TASK_SLOTS",
        "RETIRED_AUX_TASK_SLOTS",
        "_aux_task_payload",
        "_iter_auxiliary_task_rows",
        "get_auxiliary_models",
        "_coerce_optional_positive_int",
        "set_auxiliary_model",
    )
    for name in model_settings_exports:
        assert getattr(config, name) is getattr(model_settings, name)


def test_config_io_resolves_patched_env_reader_at_call_time(monkeypatch):
    monkeypatch.setattr(
        config,
        "_thread_local_env_value",
        lambda name, default="": f"profile:{name}",
    )

    assert config._expand_env_vars(
        {"api_key": "${TOKEN}", "nested": ["${SECOND}"]}
    ) == {
        "api_key": "profile:TOKEN",
        "nested": ["profile:SECOND"],
    }


def test_path_policy_resolves_patched_facade_helpers_at_call_time(monkeypatch, tmp_path):
    rejected = tmp_path / "rejected"
    selected = tmp_path / "selected"
    monkeypatch.setattr(config, "_workspace_candidates", lambda _raw=None: [rejected, selected])
    monkeypatch.setattr(config, "_ensure_workspace_dir", lambda path: path == selected)

    assert config.resolve_default_workspace() == selected


def test_provider_discovery_resolves_patched_alias_table_at_call_time(monkeypatch):
    monkeypatch.setattr(config, "_PROVIDER_DISPLAY", {"canonical": "Canonical"})
    monkeypatch.setattr(config, "_PROVIDER_MODELS", {})
    monkeypatch.setattr(
        config,
        "_resolve_provider_alias",
        lambda provider_id: "canonical" if provider_id == "alias" else provider_id,
    )

    assert config._canonicalise_provider_id("ALIAS") == "canonical"


def test_settings_persistence_resolves_patched_raw_reader_at_call_time(monkeypatch):
    monkeypatch.setattr(
        config,
        "_read_raw_settings_file",
        lambda: {"theme": "light", "skin": "mono"},
    )
    monkeypatch.setattr(config, "get_effective_default_model", lambda: "patched-model")
    monkeypatch.setattr(config, "get_config", lambda: {})

    loaded = config.load_settings()

    assert loaded["theme"] == "light"
    assert loaded["skin"] == "mono"
    assert loaded["default_model"] == "patched-model"


def test_settings_write_publication_state_is_owned_by_config_facade():
    assert "_SETTINGS_DEFAULTS" not in vars(settings_persistence)
    assert "_SETTINGS_WRITE_VERSION" not in vars(settings_persistence)
    assert "_SETTINGS_WRITE_LOCK" not in vars(settings_persistence)
    assert isinstance(config._SETTINGS_WRITE_VERSION, int)
    assert config._SETTINGS_WRITE_LOCK is not None


def test_model_reasoning_resolves_patched_impl_at_call_time(monkeypatch):
    monkeypatch.setattr(
        config,
        "_resolve_model_reasoning_efforts_impl",
        lambda *_args, **_kwargs: ["none", "low", "high"],
    )
    monkeypatch.setattr(
        config,
        "_filter_reasoning_efforts_for_provider",
        lambda efforts, *_args: efforts,
    )
    monkeypatch.setattr(config, "_zai_glm_classification", lambda *_args: None)

    assert config.resolve_model_reasoning_efforts("patched-model") == [
        "none",
        "low",
        "high",
    ]


def test_model_settings_resolves_patched_facade_helpers_at_call_time(monkeypatch):
    monkeypatch.setattr(config, "_resolve_provider_alias", lambda _provider: "openai")
    monkeypatch.setattr(
        config,
        "_resolve_main_model_fast_mode_overrides",
        lambda _model, _provider=None: {"service_tier": "priority"},
    )

    assert config._is_openai_family_provider("patched-provider") is True
    assert config._main_model_supports_service_tier(
        "patched-model", "patched-provider"
    ) is True


def test_model_settings_does_not_own_persistent_or_catalog_cache_state():
    facade_owned_state = {
        "cfg",
        "_available_models_cache",
        "_available_models_cache_ts",
        "_available_models_live_rebuild_ts",
        "_available_models_cache_source_fingerprint",
        "_available_models_cache_lock",
        "_cache_build_cv",
        "_cache_build_in_progress",
        "_models_cache_build_generation",
        "_active_models_cache_build_generation",
    }

    assert facade_owned_state <= vars(config).keys()
    assert facade_owned_state.isdisjoint(vars(model_settings))
    assert callable(config.get_available_models)
