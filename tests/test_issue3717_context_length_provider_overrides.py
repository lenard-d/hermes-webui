"""Regression coverage for #3717 provider-scoped context-length overrides."""

import sys
import types
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
ROUTES_PY = (
    REPO / "api" / "model_context.py"
).read_text(encoding="utf-8")


def _install_fake_context_resolver(monkeypatch):
    calls = []
    mod = types.ModuleType("agent.model_metadata")

    def _fake(
        model,
        base_url="",
        *args,
        config_context_length=None,
        provider="",
        custom_providers=None,
        **kwargs,
    ):
        calls.append(
            {
                "model": model,
                "base_url": base_url,
                "config_context_length": config_context_length,
                "provider": provider,
                "custom_providers": custom_providers,
            }
        )
        return config_context_length or 256000

    mod.get_model_context_length = _fake
    if "agent" not in sys.modules:
        agent_pkg = types.ModuleType("agent")
        agent_pkg.__path__ = []
        monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.model_metadata", mod)
    return calls


def test_route_resolver_uses_provider_model_context_length(monkeypatch):
    import api.config as config
    import api.routes as routes

    calls = _install_fake_context_resolver(monkeypatch)
    monkeypatch.setattr(
        config,
        "get_config",
        lambda *a, **k: {
            "model": {
                "default": "default-model",
                "context_length": 123456,
            },
            "providers": {
                "openrouter": {
                    "base_url": "https://openrouter.example/v1",
                    "models": {
                        "provider-model": {"context_length": 777000},
                    },
                },
            },
            "custom_providers": [],
        },
    )

    result = routes._resolve_context_length_for_session_model(
        "provider-model",
        "openrouter",
    )

    assert result == 777000
    assert calls[-1]["config_context_length"] == 777000
    assert calls[-1]["base_url"] == "https://openrouter.example/v1"
    assert calls[-1]["provider"] == "openrouter"


def test_route_resolver_uses_provider_model_context_length_without_base_url(monkeypatch):
    import api.config as config
    import api.routes as routes

    calls = _install_fake_context_resolver(monkeypatch)
    monkeypatch.setattr(
        config,
        "get_config",
        lambda *a, **k: {
            "model": {
                "default": "default-model",
                "context_length": 123456,
            },
            "providers": {
                "anthropic": {
                    "models": {
                        "provider-model": {"context_length": 777000},
                    },
                },
            },
            "custom_providers": [],
        },
    )

    result = routes._resolve_context_length_for_session_model(
        "provider-model",
        "anthropic",
    )

    assert result == 777000
    assert calls[-1]["config_context_length"] == 777000
    assert calls[-1]["base_url"] == ""
    assert calls[-1]["provider"] == "anthropic"


def test_route_resolver_uses_named_custom_provider_base_url(monkeypatch):
    import api.config as config
    import api.routes as routes

    custom_providers = [
        {
            "name": "ZenMux",
            "base_url": "https://zenmux.example/v1",
            "models": {
                "custom-model": {"context_length": "888000"},
            },
        }
    ]
    calls = _install_fake_context_resolver(monkeypatch)
    monkeypatch.setattr(
        config,
        "get_config",
        lambda *a, **k: {
            "model": {
                "default": "default-model",
                "context_length": 123456,
            },
            "custom_providers": custom_providers,
        },
    )

    result = routes._resolve_context_length_for_session_model(
        "custom-model",
        "custom:zenmux",
    )

    assert result == 888000
    assert calls[-1]["config_context_length"] == 888000
    assert calls[-1]["base_url"] == "https://zenmux.example/v1"
    assert calls[-1]["provider"] == "custom:zenmux"
    assert calls[-1]["custom_providers"] is custom_providers


def test_global_context_length_remains_default_model_only(monkeypatch):
    import api.config as config
    import api.routes as routes

    calls = _install_fake_context_resolver(monkeypatch)
    monkeypatch.setattr(
        config,
        "get_config",
        lambda *a, **k: {
            "model": {
                "default": "default-model",
                "context_length": 123456,
            },
            "providers": {
                "openai": {
                    "base_url": "https://openai.example/v1",
                    "models": {},
                },
            },
        },
    )

    result = routes._resolve_context_length_for_session_model("other-model", "openai")

    assert result == 256000
    assert calls[-1]["config_context_length"] is None
    assert calls[-1]["base_url"] == "https://openai.example/v1"


def test_streaming_fallbacks_use_shared_provider_context_helper(monkeypatch):
    """Live SSE and terminal projection keep one provider-aware lookup policy."""
    from types import SimpleNamespace

    from api.runs import local_context_window, local_usage

    lookup_calls = []

    def lookup(model, provider, **kwargs):
        lookup_calls.append((model, provider, kwargs))
        return SimpleNamespace(
            base_url="https://provider.example/v1",
            api_key="context-key",
            config_context_length=777000,
            provider="openrouter",
            custom_providers=[{"name": "unused"}],
        )

    monkeypatch.setattr(
        local_context_window, "_context_length_lookup_inputs_for_model", lookup
    )
    monkeypatch.setattr(
        local_usage, "_context_length_lookup_inputs_for_model", lookup
    )
    _install_fake_context_resolver(monkeypatch)

    agent = SimpleNamespace(
        model="provider-model",
        provider="openrouter",
        base_url="",
        api_key="",
        context_compressor=SimpleNamespace(context_length=0),
    )
    projection = local_context_window.ContextWindowProjection.from_agent(
        agent,
        resolved_model="provider-model",
        resolved_provider="openrouter",
        resolved_base_url="",
        resolved_api_key="",
        config={},
    )
    tracker = local_usage.LocalUsageTracker(
        session_id="issue-3717",
        session_getter=lambda: SimpleNamespace(profile=None),
        agent_getter=lambda: agent,
    )

    assert projection.context_length == 777000
    assert tracker._resolved_context_length(agent.context_compressor, tracker._session()) == 777000
    assert [provider for _model, provider, _kwargs in lookup_calls] == [
        "openrouter",
        "openrouter",
    ]


def test_route_helper_keeps_all_context_length_sources_aligned():
    assert "def _context_length_lookup_inputs_for_model" in ROUTES_PY
    # Hardened against an explicit null ``providers:`` key (salvage of #3967):
    # ``cfg.get("providers") or {}`` instead of ``cfg.get("providers", {})`` so
    # a config.yaml with ``providers:`` (None) degrades to an empty mapping.
    assert 'cfg.get("providers") or {}' in ROUTES_PY
    assert 'cfg.get("custom_providers")' in ROUTES_PY
    assert "_model_matches_configured_default" in ROUTES_PY
