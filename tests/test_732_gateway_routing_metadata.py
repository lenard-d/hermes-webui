"""Regression coverage for #732 LLM Gateway routing metadata display."""

from tests.frontend_asset_contract import family_source

from pathlib import Path

from api.models import Session
from api.streaming import _normalize_gateway_routing_metadata


REPO = Path(__file__).resolve().parents[1]
STREAMING_PY = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
MESSAGES_JS = family_source("messages")
UI_JS = family_source("ui")
SESSIONS_JS = family_source("sessions")
STYLE_CSS = family_source("style")
def test_gateway_routing_metadata_is_safely_normalized_from_response_metadata():
    metadata = {
        "used_provider": "Alibaba Cloud",
        "used_model": "deepseek-v3.2",
        "requested_provider": "CanopyWave",
        "requested_model": "deepseek-v3.2",
        "api_key": "fake_credential",
        "routing": [
            {
                "provider": "CanopyWave",
                "status": "failed",
                "reason": "timeout",
                "score": 0.12,
                "api_key": "fake_credential",
            },
            {"provider": "Alibaba Cloud", "status": "selected", "score": 0.91},
        ],
    }

    normalized = _normalize_gateway_routing_metadata(metadata, requested_model="deepseek-v3.2", requested_provider="CanopyWave")

    assert normalized == {
        "used_provider": "Alibaba Cloud",
        "used_model": "deepseek-v3.2",
        "requested_provider": "CanopyWave",
        "requested_model": "deepseek-v3.2",
        "provider_changed": True,
        "model_changed": False,
        "has_failover": True,
        "routing": [
            {"provider": "CanopyWave", "status": "failed", "reason": "timeout", "score": 0.12},
            {"provider": "Alibaba Cloud", "status": "selected", "score": 0.91},
        ],
    }
    assert "fake_credential" not in repr(normalized)


def test_gateway_routing_metadata_absent_returns_none_without_placeholder_noise():
    assert _normalize_gateway_routing_metadata({}, requested_model="gpt-5.5", requested_provider="openai-codex") is None
    assert _normalize_gateway_routing_metadata(None, requested_model="gpt-5.5", requested_provider="openai-codex") is None


def test_gateway_routing_metadata_rejects_objects_and_secret_fields():
    normalized = _normalize_gateway_routing_metadata({
        "used_provider": {"name": "provider-object", "api_key": "top-secret"},
        "used_model": ["model-object"],
        "requested_provider": "safe-provider",
        "api_key": "top-secret",
        "headers": {"authorization": "Bearer top-secret"},
        "routing": [
            {
                "provider": {"name": "nested-provider"},
                "model": ["nested-model"],
                "status": "selected",
                "api_key": "top-secret",
                "request": {"authorization": "Bearer top-secret"},
            },
        ],
    })

    assert normalized == {
        "requested_provider": "safe-provider",
        "provider_changed": False,
        "model_changed": False,
        "has_failover": False,
        "routing": [{"status": "selected"}],
    }
    assert "top-secret" not in repr(normalized)
    assert "provider-object" not in repr(normalized)


def test_gateway_routing_metadata_bounds_scalars_and_attempts():
    long_provider = "p" * 300
    normalized = _normalize_gateway_routing_metadata({
        "used_provider": long_provider,
        "routing": [
            {"provider": f"provider-{index}", "status": "failed"}
            for index in range(15)
        ],
    })

    assert normalized["used_provider"] == "p" * 240
    assert len(normalized["routing"]) == 12
    assert normalized["routing"][-1]["provider"] == "provider-11"
    assert normalized["has_failover"] is True


def test_session_persists_latest_gateway_routing_and_history_across_reload():
    routing = _normalize_gateway_routing_metadata(
        {
            "used_provider": "provider-b",
            "used_model": "model-b",
            "requested_provider": "provider-a",
            "requested_model": "model-a",
            "routing": [
                {"provider": "provider-a", "status": "failed"},
                {"provider": "provider-b", "status": "selected"},
            ],
        },
        requested_model="model-a",
        requested_provider="provider-a",
    )
    session = Session(session_id="732gateway", title="Gateway", gateway_routing=routing, gateway_routing_history=[routing])
    session.messages = [{"role": "assistant", "content": "done", "_gatewayRouting": routing}]
    session.save()

    reloaded = Session.load("732gateway")

    assert reloaded.gateway_routing == routing
    assert reloaded.gateway_routing_history == [routing]
    assert reloaded.messages[-1]["_gatewayRouting"] == routing
    compact = reloaded.compact()
    assert compact["gateway_routing"] == routing
    assert compact["gateway_routing_history"] == [routing]


def test_streaming_captures_gateway_metadata_into_usage_payload_and_assistant_turn():
    local_run_py = (
        REPO / "api" / "runs" / "local.py"
    ).read_text(encoding="utf-8")
    assert "_extract_gateway_routing_metadata" in local_run_py
    assert "usage['gateway_routing']" in local_run_py
    assert "_dm['_gatewayRouting']" in local_run_py
    assert "s.gateway_routing_history" in local_run_py


def test_frontend_copies_and_formats_gateway_metadata_without_absent_noise():
    assert "d.usage.gateway_routing" in MESSAGES_JS
    assert "lastAsst._gatewayRouting" in MESSAGES_JS
    assert "_formatGatewayModelLabel" in UI_JS
    assert "_gatewayRoutingLabel" in UI_JS
    assert "msg-gateway-inline" in UI_JS
    assert "msg-model-warning-inline" in UI_JS
    assert "gateway-failover-inline" in UI_JS
    assert "if(!routing)return''" in UI_JS.replace(" ", "")
    assert "_formatSessionModelWithGateway" in SESSIONS_JS
    assert ".msg-model-warning-inline" in STYLE_CSS
