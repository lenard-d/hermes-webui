"""Regression coverage for WebUI streaming provider failure handling.

The incident this guards against: WebUI-created AIAgent instances did not pass
config.yaml's max_tokens, so a fallback Claude model via OpenRouter requested its
native 64k output ceiling and failed with HTTP 402 "more credits / fewer
max_tokens". The stream then looked like a stuck Thinking card instead of a
clear quota error.
"""
from pathlib import Path
from types import SimpleNamespace

from api.runs.local_agent_config import build_local_agent_configuration
from api.streaming import _classify_provider_error

LOCAL_AGENT_CACHE = (
    Path(__file__).resolve().parents[1] / "api" / "runs" / "local_agent_cache.py"
)


def _build_agent_config(config):
    class Agent:
        def __init__(self, model, max_tokens=None, **_kwargs):
            pass

    callbacks = SimpleNamespace(
        token=None,
        reasoning=None,
        tool=None,
        interim_assistant=None,
        tool_start=None,
        tool_complete=None,
        status=None,
    )
    return build_local_agent_configuration(
        agent_class=Agent,
        config=config,
        model="gpt-test",
        provider="openrouter",
        base_url=None,
        api_key="secret",
        toolsets=[],
        session_id="quota-test",
        session_db=None,
        prefill_messages=[],
        callbacks=callbacks,
        clarify_callback=None,
        runtime={},
        request_overrides=None,
    )


def test_streaming_passes_configured_max_tokens_to_agent():
    top_level = _build_agent_config({"max_tokens": "4096"})
    nested = _build_agent_config({"agent": {"max_tokens": "2048"}})

    assert top_level.max_tokens == 4096
    assert top_level.kwargs["max_tokens"] == 4096
    assert nested.max_tokens == 2048
    assert nested.kwargs["max_tokens"] == 2048


def test_streaming_agent_cache_signature_includes_max_tokens_and_fallback():
    src = LOCAL_AGENT_CACHE.read_text(encoding="utf-8")
    assert "max_tokens or \"\"" in src
    assert "fallback_models or {}" in src


def test_openrouter_more_credits_error_is_classified_as_quota():
    for message in (
        "more credits are required",
        "account can only afford 1024 tokens",
        "retry with fewer max_tokens",
    ):
        assert _classify_provider_error(message)["type"] == "quota_exhausted"
