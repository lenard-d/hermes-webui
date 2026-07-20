"""Architecture contract for the importable ``api.config_parts`` slices."""

import api.config as config
from api.config_parts import config_io, path_env, provider_discovery, provider_routing


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
