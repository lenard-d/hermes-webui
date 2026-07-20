from tests.frontend_asset_contract import family_source

from pathlib import Path
from urllib.parse import urlsplit

import api.config as cfg
import yaml


def read(path):
    return Path(path).read_text(encoding="utf-8")


def test_set_reasoning_effort_returns_status_for_explicit_model(tmp_path, monkeypatch):
    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text(
        yaml.safe_dump(
            {
                "model": {"default": "gpt-4o", "provider": "openai"},
                "agent": {"reasoning_effort": ""},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cfg, "_get_config_path", lambda: cfgfile)
    monkeypatch.setattr(cfg, "reload_config", lambda: None)

    seen = {}

    def fake_resolve(model_id, provider_id=None, base_url=None):
        seen["args"] = (model_id, provider_id, base_url)
        if model_id == "claude-opus-4-7":
            return ["minimal", "low", "medium", "high", "xhigh", "max"]
        return []

    monkeypatch.setattr(cfg, "resolve_model_reasoning_efforts", fake_resolve)

    status = cfg.set_reasoning_effort(
        "high",
        model_id="claude-opus-4-7",
        provider_id="anthropic",
    )

    assert seen["args"] == ("claude-opus-4-7", "anthropic", None)
    assert status["reasoning_effort"] == "high"
    assert status["supported_efforts"] == [
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]


def test_ui_posts_reasoning_context_with_effort():
    src = family_source("ui")
    assert "function _reasoningEffortContext()" in src
    assert "new URLSearchParams(_reasoningEffortContext())" in src
    assert "Object.assign({effort:effort},_reasoningEffortContext())" in src


def test_reasoning_post_route_threads_model_context():
    from api.http.routes import provider_mutations

    calls = []
    responses = []
    context = {
        "_clear_live_models_cache": lambda: None,
        "_handle_sessions_cleanup": lambda *_args, **_kwargs: None,
        "bad": lambda *_args, **_kwargs: None,
        "j": lambda _handler, payload, **_kwargs: responses.append(payload) or True,
        "remove_provider_key": lambda *_args, **_kwargs: None,
        "set_hermes_default_model": lambda *_args, **_kwargs: None,
        "set_provider_key": lambda *_args, **_kwargs: None,
        "set_reasoning_display": lambda *_args, **_kwargs: None,
        "set_reasoning_effort": lambda effort, **kwargs: calls.append((effort, kwargs))
        or {"reasoning_effort": effort},
    }

    assert provider_mutations.handle_post(
        object(),
        urlsplit("/api/reasoning"),
        {
            "effort": "high",
            "model": "claude-opus-4-7",
            "provider": "anthropic",
        },
        None,
        context,
    )
    assert calls == [
        (
            "high",
            {
                "model_id": "claude-opus-4-7",
                "provider_id": "anthropic",
                "base_url": None,
            },
        )
    ]
    assert responses == [{"reasoning_effort": "high"}]
