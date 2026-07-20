"""Resolve Hermes Agent constructor configuration for one local run."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from .local_events import LocalEventTranslator


@dataclass(frozen=True)
class LocalAgentConfiguration:
    """Resolved, version-compatible constructor input for Hermes Agent."""

    parameters: set[str]
    kwargs: dict[str, Any]
    fallback_models: list[dict[str, Any]] | None
    max_iterations: int | None
    max_tokens: int | None
    reasoning: Any


def _positive_int(value) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _fallback_entries(raw) -> list[dict[str, Any]]:
    items = [raw] if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    entries = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("provider") or "").strip()
        model = str(entry.get("model") or "").strip()
        if not provider or not model:
            continue
        entries.append(
            {
                "model": model,
                "provider": provider,
                "base_url": entry.get("base_url"),
                "api_key": entry.get("api_key"),
                "key_env": entry.get("key_env"),
            }
        )
    return entries


def _fallback_chain(config: dict) -> list[dict[str, Any]] | None:
    chain = []
    seen = set()
    for key in ("fallback_providers", "fallback_model"):
        for entry in _fallback_entries(config.get(key)):
            identity = (
                str(entry.get("provider") or "").strip().lower(),
                str(entry.get("model") or "").strip().lower(),
                str(entry.get("base_url") or "").strip().rstrip("/").lower(),
            )
            if identity in seen:
                continue
            seen.add(identity)
            chain.append(entry)
    return chain or None


def build_local_agent_configuration(
    api: ModuleType,
    *,
    agent_class,
    config: dict,
    model,
    provider,
    base_url,
    api_key,
    toolsets,
    session_id: str,
    session_db,
    prefill_messages,
    callbacks: LocalEventTranslator,
    clarify_callback,
    runtime: dict,
    request_overrides,
) -> LocalAgentConfiguration:
    """Build constructor kwargs while adapting to the installed Agent version."""
    parameters = set(inspect.signature(agent_class.__init__).parameters)
    fallback_models = _fallback_chain(config)
    agent_config = config.get("agent", {}) if isinstance(config, dict) else {}
    if not isinstance(agent_config, dict):
        agent_config = {}
    max_iterations = _positive_int(
        agent_config.get("max_turns", config.get("max_turns"))
    )
    max_tokens = _positive_int(config.get("max_tokens", agent_config.get("max_tokens")))
    try:
        effort = api.coerce_reasoning_effort_for_model(
            agent_config.get("reasoning_effort"),
            model,
            provider_id=provider,
            base_url=base_url,
        )
        reasoning = api.parse_reasoning_effort(effort)
    except Exception:
        reasoning = None

    kwargs = {
        "model": model,
        "provider": provider,
        "base_url": base_url,
        "api_key": api_key,
        "platform": "webui",
        "quiet_mode": True,
        "enabled_toolsets": toolsets,
        "fallback_model": fallback_models,
        "session_id": session_id,
        "session_db": session_db,
        "prefill_messages": prefill_messages,
        "stream_delta_callback": callbacks.token,
        "reasoning_callback": callbacks.reasoning,
        "tool_progress_callback": callbacks.tool,
        "clarify_callback": clarify_callback,
    }
    optional = {
        "interim_assistant_callback": callbacks.interim_assistant,
        "tool_start_callback": callbacks.tool_start,
        "tool_complete_callback": callbacks.tool_complete,
        "status_callback": callbacks.status,
        "api_mode": runtime.get("api_mode"),
        "acp_command": runtime.get("command"),
        "acp_args": runtime.get("args"),
        "credential_pool": runtime.get("credential_pool"),
        "gateway_session_key": session_id,
    }
    for name, value in optional.items():
        if name in parameters:
            kwargs[name] = value
    if "reasoning_config" in parameters and reasoning is not None:
        kwargs["reasoning_config"] = reasoning
    if "prefill_messages" not in parameters:
        kwargs.pop("prefill_messages", None)
    if "max_iterations" in parameters and max_iterations is not None:
        kwargs["max_iterations"] = max_iterations
    if "max_tokens" in parameters and max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if "request_overrides" in parameters and request_overrides:
        kwargs["request_overrides"] = request_overrides
    return LocalAgentConfiguration(
        parameters=parameters,
        kwargs=kwargs,
        fallback_models=fallback_models,
        max_iterations=max_iterations,
        max_tokens=max_tokens,
        reasoning=reasoning,
    )
