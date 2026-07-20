"""Behavior coverage for the public speech package interface."""

import pytest

from api import profiles, speech
from api.speech import providers


def test_tts_provider_credentials_follow_active_profile(monkeypatch, tmp_path):
    homes = {
        "alpha": tmp_path / "alpha",
        "beta": tmp_path / "beta",
    }
    active = {"profile": "alpha"}
    for profile, home in homes.items():
        home.mkdir()
        (home / ".env").write_text(
            f"OPENAI_API_KEY={profile}-key\n",
            encoding="utf-8",
        )
    monkeypatch.delenv("VOICE_TOOLS_OPENAI_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        profiles,
        "get_active_hermes_home",
        lambda: homes[active["profile"]],
    )
    alpha = speech.resolve_tts_provider("openai")
    active["profile"] = "beta"
    beta = speech.resolve_tts_provider("openai")

    assert isinstance(alpha, providers.OpenAITtsProvider)
    assert isinstance(beta, providers.OpenAITtsProvider)
    assert alpha.api_key == "alpha-key"
    assert beta.api_key == "beta-key"


def test_unknown_tts_engine_preserves_edge_fallback():
    assert isinstance(
        speech.resolve_tts_provider("future-browser-engine"),
        providers.EdgeTtsProvider,
    )


def test_tts_request_validation_preserves_shared_limits():
    with pytest.raises(speech.InvalidSpeechRequest, match="text is required"):
        speech.parse_tts_request({"text": "  "})
    with pytest.raises(speech.InvalidSpeechRequest, match="5000"):
        speech.parse_tts_request({"text": "x" * 5001})
    with pytest.raises(speech.InvalidSpeechRequest, match="invalid rate"):
        speech.parse_tts_request({"text": "hello", "rate": "+101%"})
