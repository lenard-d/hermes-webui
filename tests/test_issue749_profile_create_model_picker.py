"""Regression coverage for #749 profile creation model/provider selection."""

from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import api.profiles as profiles
from tests.frontend_asset_contract import family_source

REPO = Path(__file__).resolve().parent.parent
PANELS_JS = family_source("panels")


def test_profile_create_form_exposes_model_picker():
    assert 'id="profileFormModel"' in PANELS_JS
    assert "_populateProfileFormModelSelect" in PANELS_JS
    assert "profile_model_label" in PANELS_JS
    assert "profile_model_hint" in PANELS_JS


def test_profile_create_payload_preserves_provider_context():
    fn_start = PANELS_JS.find("async function saveProfileForm()")
    assert fn_start != -1
    fn_body = PANELS_JS[fn_start : PANELS_JS.find("\n}", fn_start) + 2]
    assert "profileFormModel" in fn_body
    assert "_modelStateForSelect(modelEl, selectedModel)" in fn_body
    assert "payload.default_model" in fn_body
    assert "payload.model_provider" in fn_body


def test_profile_create_route_passes_model_fields_to_profile_api(monkeypatch):
    from api.http.routes import profile_mutations

    captured = {}

    def create_profile_api(name, **kwargs):
        captured.update(name=name, **kwargs)
        return {"name": name}

    monkeypatch.setattr(profiles, "create_profile_api", create_profile_api)
    def unused(*_args, **_kwargs):
        return None

    context = defaultdict(lambda: unused)
    context.update(
        {
            "j": lambda _handler, payload, **_kwargs: payload,
            "bad": lambda _handler, message, status=400: {
                "error": message,
                "status": status,
            },
        }
    )
    body = {
        "name": "research",
        "default_model": "anthropic/claude-opus-4.6",
        "model_provider": "nous",
    }

    result = profile_mutations.handle_post(
        object(),
        SimpleNamespace(path="/api/profile/create"),
        body,
        None,
        context,
    )

    assert result == {"ok": True, "profile": {"name": "research"}}
    assert captured["default_model"] == "anthropic/claude-opus-4.6"
    assert captured["model_provider"] == "nous"


def test_profile_model_config_writer_persists_default_and_provider(tmp_path):
    profile_dir = tmp_path / "profiles" / "research"
    profile_dir.mkdir(parents=True)

    profiles._write_model_defaults_to_config(
        profile_dir,
        default_model="anthropic/claude-opus-4.6",
        model_provider="nous",
    )

    saved = yaml.safe_load((profile_dir / "config.yaml").read_text(encoding="utf-8"))
    assert saved["model"]["default"] == "anthropic/claude-opus-4.6"
    assert saved["model"]["provider"] == "nous"


def test_profile_model_config_writer_preserves_existing_model_settings(tmp_path):
    profile_dir = tmp_path / "profiles" / "research"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text(
        "model:\n  base_url: https://gateway.example/v1\n",
        encoding="utf-8",
    )

    profiles._write_model_defaults_to_config(
        profile_dir,
        default_model="gpt-5.5",
        model_provider="openai-codex",
    )

    saved = yaml.safe_load((profile_dir / "config.yaml").read_text(encoding="utf-8"))
    assert saved["model"]["base_url"] == "https://gateway.example/v1"
    assert saved["model"]["default"] == "gpt-5.5"
    assert saved["model"]["provider"] == "openai-codex"


def test_profile_model_selection_accepts_catalog_model_with_provider():
    catalog = {
        "groups": [
            {
                "provider": "OpenAI Codex",
                "provider_id": "openai-codex",
                "models": [{"id": "gpt-5.5", "label": "GPT-5.5"}],
            }
        ]
    }

    profiles._validate_profile_model_selection(
        "gpt-5.5",
        "openai-codex",
        available_models=catalog,
    )


def test_profile_model_selection_accepts_provider_qualified_picker_value():
    catalog = {
        "groups": [
            {
                "provider": "Research Gateway",
                "provider_id": "custom:research-gateway",
                "models": [
                    {
                        "id": "@custom:research-gateway:claude-opus-4.6",
                        "label": "claude-opus-4.6",
                    }
                ],
            }
        ]
    }

    default_model, model_provider = profiles._split_webui_provider_model_value(
        "@custom:research-gateway:claude-opus-4.6",
        "custom:research-gateway",
    )

    profiles._validate_profile_model_selection(
        default_model,
        model_provider,
        available_models=catalog,
    )


def test_profile_model_selection_accepts_model_in_overflow_extra_models():
    """A default model living in a provider's picker-overflow ``extra_models``
    bucket (providers past the 15-entry visible cap, issue #3691) must validate
    — it renders in the dropdown, so it must not fail with a false 400 (#5453)."""
    catalog = {
        "groups": [
            {
                "provider": "OpenAI Codex",
                "provider_id": "openai-codex",
                "models": [{"id": "gpt-5.5", "label": "GPT-5.5"}],
                "extra_models": [
                    {"id": "gpt-5.5-overflow", "label": "GPT-5.5 Overflow"}
                ],
            }
        ]
    }

    # Selected default lives only in the overflow bucket — must still validate.
    profiles._validate_profile_model_selection(
        "gpt-5.5-overflow",
        "openai-codex",
        available_models=catalog,
    )
    assert profiles._profile_model_selection_exists(
        catalog, "gpt-5.5-overflow", "openai-codex"
    )


def test_profile_model_selection_rejects_unknown_model_provider_pair():
    catalog = {
        "groups": [
            {
                "provider": "OpenAI Codex",
                "provider_id": "openai-codex",
                "models": [{"id": "gpt-5.5", "label": "GPT-5.5"}],
            }
        ]
    }

    with pytest.raises(ValueError, match="not available for provider"):
        profiles._validate_profile_model_selection(
            "missing-model",
            "openai-codex",
            available_models=catalog,
        )


def test_profile_create_rejects_unknown_model_before_creating_profile(monkeypatch):
    calls = []

    monkeypatch.setattr(
        profiles,
        "_get_available_models_for_profile_validation",
        lambda: {
            "groups": [
                {
                    "provider": "OpenAI Codex",
                    "provider_id": "openai-codex",
                    "models": [{"id": "gpt-5.5", "label": "GPT-5.5"}],
                }
            ]
        },
    )
    monkeypatch.setattr(
        profiles,
        "_create_profile_fallback",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    with pytest.raises(ValueError, match="Selected model 'missing-model'"):
        profiles.create_profile_api(
            "research",
            default_model="missing-model",
            model_provider="openai-codex",
        )

    assert calls == []
