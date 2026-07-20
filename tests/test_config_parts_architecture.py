"""Architecture contract for the importable ``api.config_parts`` slices."""

import subprocess
import sys

import api.config as config
from api.config_parts import (
    config_io,
    models_cache,
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


def test_models_cache_imports_without_importing_config_facade():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import api.config_parts.models_cache; "
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
    models_cache_exports = (
        "_get_models_cache_path",
        "_get_auth_store_path",
        "_models_cache_file_fingerprint",
        "_models_cache_catalog_fingerprint",
        "_AUTH_FINGERPRINT_VOLATILE_KEYS",
        "_strip_volatile_auth_fields",
        "_auth_store_semantic_fingerprint",
        "_models_cache_source_fingerprint",
        "_delete_models_cache_on_disk",
        "_is_valid_models_cache",
        "_is_loadable_disk_cache",
        "_load_models_cache_from_disk",
        "_model_aliases_from_config",
        "_load_stale_models_cache_from_disk",
        "_save_models_cache_to_disk",
        "_get_fresh_memory_models_cache",
        "_models_cache_file_age_seconds",
        "warm_models_catalog_provenance_if_cold",
        "get_available_models_for_session_visit",
        "_maybe_log_slow_stages",
        "invalidate_models_cache",
        "invalidate_credential_pool_cache",
        "invalidate_provider_models_cache",
    )
    for name in models_cache_exports:
        assert getattr(config, name) is getattr(models_cache, name)


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


def test_models_cache_does_not_own_mutable_cache_or_generation_state():
    facade_owned_state = {
        "_models_cache_path",
        "_available_models_cache",
        "_available_models_cache_ts",
        "_available_models_live_rebuild_ts",
        "_available_models_cache_source_fingerprint",
        "_available_models_cache_lock",
        "_cache_build_cv",
        "_cache_build_in_progress",
        "_models_cache_build_generation",
        "_active_models_cache_build_generation",
        "_models_cache_provenance",
        "_advertised_model_ids_memo",
        "_CREDENTIAL_POOL_CACHE",
    }

    assert facade_owned_state <= vars(config).keys()
    assert facade_owned_state.isdisjoint(vars(models_cache))


def test_models_cache_session_visit_exports_preserve_facade_module_identity():
    exports = (
        config._models_cache_file_age_seconds,
        config.warm_models_catalog_provenance_if_cold,
        config.get_available_models_for_session_visit,
        config._maybe_log_slow_stages,
    )

    assert exports == (
        models_cache._models_cache_file_age_seconds,
        models_cache.warm_models_catalog_provenance_if_cold,
        models_cache.get_available_models_for_session_visit,
        models_cache._maybe_log_slow_stages,
    )
    assert {export.__module__ for export in exports} == {"api.config"}


def test_session_visit_cache_uses_late_bound_facade_and_preserves_fallback_order(
    monkeypatch,
):
    events = []
    stale_catalog = {"groups": ["stale"]}
    monkeypatch.setattr(config, "_get_models_cache_path", lambda: "patched-path")
    monkeypatch.setattr(
        config,
        "_models_cache_file_age_seconds",
        lambda path, _now: events.append(("age", path)) or None,
    )
    monkeypatch.setattr(
        config,
        "_load_stale_models_cache_from_disk",
        lambda: events.append("stale") or stale_catalog,
    )

    def fail_refresh(**kwargs):
        events.append(("refresh", kwargs))
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(config, "get_available_models", fail_refresh)
    monkeypatch.setattr(
        config,
        "_maybe_log_slow_stages",
        lambda *_args: events.append("timing"),
    )

    assert config.get_available_models_for_session_visit() == stale_catalog
    assert events == [
        ("age", "patched-path"),
        "stale",
        ("refresh", {"force_refresh": True}),
        "timing",
    ]


def test_models_cache_slow_stage_logger_reports_ordered_deltas():
    class Recorder:
        def __init__(self):
            self.calls = []

        def warning(self, *args):
            self.calls.append(args)

    logger = Recorder()

    config._maybe_log_slow_stages(
        logger,
        [("enter", 10.0), ("disk", 10.125), ("refresh", 10.5)],
        400.0,
        "models.session_visit",
    )

    assert logger.calls == [
        (
            "[SLOW] %s total=%.1fms stages: %s",
            "models.session_visit",
            500.0,
            "disk=125.0ms refresh=375.0ms",
        )
    ]


def test_models_cache_full_invalidation_publishes_before_revoking_generation(
    monkeypatch,
):
    events = []
    monkeypatch.setattr(config, "_available_models_cache", {"stale": True})
    monkeypatch.setattr(config, "_available_models_cache_ts", 12.0)
    monkeypatch.setattr(config, "_available_models_live_rebuild_ts", 13.0)
    monkeypatch.setattr(
        config, "_available_models_cache_source_fingerprint", {"stale": True}
    )
    monkeypatch.setattr(config, "_models_cache_provenance", ({"stale": True}, {}))
    monkeypatch.setattr(config, "_models_cache_build_generation", 41)
    monkeypatch.setattr(config, "_active_models_cache_build_generation", 41)
    monkeypatch.setattr(config, "_cache_build_in_progress", True)
    monkeypatch.setattr(config, "_CREDENTIAL_POOL_CACHE", {("profile", "p"): 1})

    def observe(event):
        events.append(
            (
                event,
                config._available_models_cache,
                config._available_models_cache_source_fingerprint,
                config._models_cache_provenance,
            )
        )

    original_sync = config._sync_models_cache_provenance
    original_invalidate_build = config._invalidate_models_build_locked

    def sync_provenance():
        original_sync()
        observe("provenance")

    def invalidate_build():
        original_invalidate_build()
        observe("generation")

    monkeypatch.setattr(config, "_sync_models_cache_provenance", sync_provenance)
    monkeypatch.setattr(config, "_invalidate_models_build_locked", invalidate_build)
    monkeypatch.setattr(config, "_delete_models_cache_on_disk", lambda: observe("disk"))
    import api.plugin_providers as plugin_providers

    monkeypatch.setattr(
        plugin_providers,
        "invalidate_plugin_model_provider_cache",
        lambda: observe("plugin"),
    )

    config.invalidate_models_cache()

    assert [event[0] for event in events] == [
        "provenance",
        "generation",
        "disk",
        "plugin",
    ]
    assert all(
        snapshot is None and fingerprint is None and provenance is None
        for _, snapshot, fingerprint, provenance in events
    )
    assert config._models_cache_build_generation == 42
    assert config._active_models_cache_build_generation is None
    assert config._cache_build_in_progress is False
    assert config._CREDENTIAL_POOL_CACHE == {}


def test_models_cache_provider_invalidation_uses_late_bound_profile_and_alias(
    monkeypatch,
):
    events = []
    monkeypatch.setattr(config, "_available_models_cache", {"stale": True})
    monkeypatch.setattr(config, "_available_models_cache_ts", 12.0)
    monkeypatch.setattr(config, "_available_models_live_rebuild_ts", 13.0)
    monkeypatch.setattr(
        config, "_available_models_cache_source_fingerprint", {"stale": True}
    )
    monkeypatch.setattr(config, "_models_cache_provenance", ({"stale": True}, {}))
    monkeypatch.setattr(config, "_models_cache_build_generation", 17)
    monkeypatch.setattr(config, "_active_models_cache_build_generation", 17)
    monkeypatch.setattr(config, "_cache_build_in_progress", True)
    monkeypatch.setattr(
        config,
        "_CREDENTIAL_POOL_CACHE",
        {("patched-profile", "alias"): 1, ("patched-profile", "canonical"): 2},
    )
    monkeypatch.setattr(
        config, "_credential_pool_profile_tag", lambda: "patched-profile"
    )
    monkeypatch.setattr(config, "_resolve_provider_alias", lambda _provider: "canonical")
    original_sync = config._sync_models_cache_provenance
    original_invalidate_build = config._invalidate_models_build_locked

    def sync_provenance():
        original_sync()
        events.append("provenance")

    def invalidate_build():
        assert config._models_cache_provenance is None
        original_invalidate_build()
        events.append("generation")

    monkeypatch.setattr(config, "_sync_models_cache_provenance", sync_provenance)
    monkeypatch.setattr(config, "_invalidate_models_build_locked", invalidate_build)
    monkeypatch.setattr(config, "_delete_models_cache_on_disk", lambda: events.append("disk"))

    config.invalidate_provider_models_cache("alias")

    assert events == ["provenance", "generation", "disk"]
    assert config._available_models_cache is None
    assert config._available_models_cache_source_fingerprint is None
    assert config._models_cache_provenance is None
    assert config._models_cache_build_generation == 18
    assert config._active_models_cache_build_generation is None
    assert config._cache_build_in_progress is False
    assert config._CREDENTIAL_POOL_CACHE == {}


def test_models_cache_credential_invalidation_uses_late_bound_facade(
    monkeypatch,
):
    monkeypatch.setattr(
        config,
        "_CREDENTIAL_POOL_CACHE",
        {("patched-profile", "alias"): 1, ("patched-profile", "canonical"): 2},
    )
    monkeypatch.setattr(
        config, "_credential_pool_profile_tag", lambda: "patched-profile"
    )
    monkeypatch.setattr(config, "_resolve_provider_alias", lambda _provider: "canonical")
    invalidated_usage = []
    import api.providers as providers

    monkeypatch.setattr(
        providers,
        "invalidate_account_usage_status_cache",
        invalidated_usage.append,
    )

    config.invalidate_credential_pool_cache("alias")

    assert config._CREDENTIAL_POOL_CACHE == {}
    assert invalidated_usage == ["alias", "canonical"]
