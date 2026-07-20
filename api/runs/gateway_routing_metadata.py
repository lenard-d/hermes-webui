"""Bounded, display-safe LLM Gateway routing metadata normalization."""

from __future__ import annotations



_GATEWAY_ROUTING_TOP_LEVEL_KEYS = {
    "used_provider",
    "used_model",
    "requested_provider",
    "requested_model",
}
_GATEWAY_ROUTING_CONTAINER_KEYS = (
    "llm_gateway",
    "gateway",
    "metadata",
    "response_metadata",
    "routing_metadata",
    "usage",
)
_GATEWAY_ROUTING_ATTEMPT_KEYS = {
    "provider",
    "model",
    "status",
    "reason",
    "selection_reason",
    "score",
    "latency_ms",
    "error",
    "timestamp",
    "selected",
    "attempt",
    "attempt_index",
}


def _clean_gateway_routing_scalar(value):
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        text = str(value).strip()
        if not text:
            return None
        return value if isinstance(value, (int, float, bool)) else text[:240]
    return None


def _find_gateway_metadata_payload(payload):
    if not isinstance(payload, dict):
        return None
    if any(key in payload for key in _GATEWAY_ROUTING_TOP_LEVEL_KEYS) or isinstance(payload.get("routing"), list):
        return payload
    for key in _GATEWAY_ROUTING_CONTAINER_KEYS:
        nested = payload.get(key)
        found = _find_gateway_metadata_payload(nested)
        if found:
            return found
    return None


def _normalize_gateway_routing_metadata(
    payload,
    requested_model=None,
    requested_provider=None,
):
    """Return safe LLM Gateway routing metadata, or None when absent.

    LLM Gateway response metadata can contain provider/model routing details,
    but WebUI must only persist display-safe scalars and a bounded routing list.
    Secrets or provider-specific request objects are deliberately ignored.
    """
    src = _find_gateway_metadata_payload(payload)
    if not src:
        return None

    normalized = {}
    for key in _GATEWAY_ROUTING_TOP_LEVEL_KEYS:
        value = _clean_gateway_routing_scalar(src.get(key))
        if value is not None:
            normalized[key] = value

    if "requested_model" not in normalized:
        fallback_model = _clean_gateway_routing_scalar(requested_model)
        if fallback_model is not None:
            normalized["requested_model"] = fallback_model
    if "requested_provider" not in normalized:
        fallback_provider = _clean_gateway_routing_scalar(requested_provider)
        if fallback_provider is not None:
            normalized["requested_provider"] = fallback_provider

    routing = []
    raw_routing = src.get("routing")
    if isinstance(raw_routing, list):
        for attempt in raw_routing[:12]:
            if not isinstance(attempt, dict):
                continue
            clean_attempt = {}
            for key in _GATEWAY_ROUTING_ATTEMPT_KEYS:
                value = _clean_gateway_routing_scalar(attempt.get(key))
                if value is not None:
                    clean_attempt[key] = value
            if clean_attempt:
                routing.append(clean_attempt)
    if routing:
        normalized["routing"] = routing

    used_provider = str(normalized.get("used_provider") or "").strip().lower()
    requested_provider_norm = str(normalized.get("requested_provider") or "").strip().lower()
    used_model = str(normalized.get("used_model") or "").strip().lower()
    requested_model_norm = str(normalized.get("requested_model") or "").strip().lower()
    provider_changed = bool(used_provider and requested_provider_norm and used_provider != requested_provider_norm)
    model_changed = bool(used_model and requested_model_norm and used_model != requested_model_norm)
    attempted_providers = [
        str(attempt.get("provider") or "").strip().lower()
        for attempt in routing
        if attempt.get("provider")
    ]
    distinct_attempted_providers = {provider for provider in attempted_providers if provider}
    failed_before_selection = any(
        str(attempt.get("status") or "").strip().lower() in {"failed", "error", "timeout", "rejected"}
        for attempt in routing
    )
    has_failover = bool(provider_changed or len(distinct_attempted_providers) > 1 or failed_before_selection)

    if not (
        normalized.get("used_provider") or normalized.get("used_model") or routing or provider_changed or model_changed
    ):
        return None
    normalized["provider_changed"] = provider_changed
    normalized["model_changed"] = model_changed
    normalized["has_failover"] = has_failover
    return normalized


def _extract_gateway_routing_metadata(
    agent,
    result,
    requested_model=None,
    requested_provider=None,
):
    candidates = []
    if isinstance(result, dict):
        candidates.extend([
            result.get("llm_gateway"),
            result.get("gateway"),
            result.get("metadata"),
            result.get("response_metadata"),
            result.get("routing_metadata"),
            result.get("usage"),
            result,
        ])
    for attr in (
        "llm_gateway_metadata",
        "gateway_metadata",
        "last_response_metadata",
        "response_metadata",
        "routing_metadata",
        "last_usage",
    ):
        if agent is not None:
            candidates.append(getattr(agent, attr, None))
    for candidate in candidates:
        normalized = _normalize_gateway_routing_metadata(
            candidate,
            requested_model=requested_model,
            requested_provider=requested_provider,
        )
        if normalized:
            return normalized
    return None
