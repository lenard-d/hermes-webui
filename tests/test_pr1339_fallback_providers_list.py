"""Behavioral coverage for WebUI fallback-chain configuration.

The local-agent configuration owner accepts both list-form
``fallback_providers`` and legacy dict-form ``fallback_model`` without calling
``.get`` on a list. It preserves chain order, credential hints, and route
deduplication before the resolved value reaches Hermes Agent.
"""

from api.runs.local_agent_config import _fallback_chain


def test_fallback_handles_both_dict_and_list_config():
    legacy = _fallback_chain(
        {"fallback_model": {"provider": "anthropic", "model": "claude"}}
    )
    modern = _fallback_chain(
        {
            "fallback_providers": [
                {"provider": "openrouter", "model": "deepseek"},
                {"provider": "anthropic", "model": "claude"},
            ]
        }
    )

    assert [entry["model"] for entry in legacy] == ["claude"]
    assert [entry["model"] for entry in modern] == ["deepseek", "claude"]


def test_fallback_list_builds_chain_before_legacy_fallback():
    chain = _fallback_chain(
        {
            "fallback_providers": [
                {"provider": "openrouter", "model": "deepseek"},
            ],
            "fallback_model": {"provider": "anthropic", "model": "claude"},
        }
    )

    assert [(entry["provider"], entry["model"]) for entry in chain] == [
        ("openrouter", "deepseek"),
        ("anthropic", "claude"),
    ]


def test_fallback_resolved_defaults_to_none():
    assert _fallback_chain({}) is None
    assert _fallback_chain({"fallback_providers": []}) is None
    assert _fallback_chain({"fallback_providers": "not-a-chain"}) is None


def test_fallback_resolved_preserves_credential_hints():
    chain = _fallback_chain(
        {
            "fallback_providers": [
                {
                    "provider": "custom",
                    "model": "model-a",
                    "base_url": "https://example.test/v1",
                    "api_key": "inline-key",
                    "key_env": "FALLBACK_API_KEY",
                }
            ]
        }
    )

    assert chain == [
        {
            "model": "model-a",
            "provider": "custom",
            "base_url": "https://example.test/v1",
            "api_key": "inline-key",
            "key_env": "FALLBACK_API_KEY",
        }
    ]


def test_fallback_chain_deduplicates_routes():
    chain = _fallback_chain(
        {
            "fallback_providers": [
                {
                    "provider": "Custom",
                    "model": "Model-A",
                    "base_url": "https://example.test/v1/",
                }
            ],
            "fallback_model": {
                "provider": "custom",
                "model": "model-a",
                "base_url": "https://example.test/v1",
            },
        }
    )

    assert len(chain) == 1
    assert chain[0]["provider"] == "Custom"
    assert chain[0]["model"] == "Model-A"
