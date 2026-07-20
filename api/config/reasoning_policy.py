"""Provider/model capability policy and reasoning-effort coercion.

Capability sources answer what a model advertises.  This owner applies the
provider ceilings, forced-thinking rules, conservative fallbacks, and
downgrade ladder that determine what Hermes WebUI may actually offer or send.
"""

import re

from api import config as _config_module


KNOWN_REASONING_PROVIDERS = frozenset(
    {
        "anthropic",
        "claude",
        "anthropic-claude",
        "openai",
        "openai-api",
        "openai-codex",
        "azure",
        "azure-openai",
        "azure-foundry",
        "bedrock",
        "aws-bedrock",
        "vertex",
        "google-vertex",
        "gemini",
        "google",
        "google-gemini",
        "deepseek",
        "x-ai",
        "xai",
        "grok",
        "copilot",
        "github-copilot",
        "openrouter",
    }
)


def zai_glm_classification(model_id: str, provider_id: str) -> str | None:
    """Classify a native-Z.AI GLM model as effort, thinking, or forced."""
    api = _config_module
    provider = api._resolve_provider_alias(str(provider_id or "").strip().lower())
    if provider != "zai":
        return None
    bare = (
        api._strip_provider_hint_for_reasoning(str(model_id or ""))
        .lower()
        .rsplit("/", 1)[-1]
    )
    if "glm" not in bare:
        return None
    if bare.startswith("glm-4.7"):
        return "forced"
    match = re.search(r"glm-(\d+)(?:\D+(\d+))?", bare)
    if not match:
        return None
    major = int(match.group(1))
    minor = int(match.group(2)) if match.group(2) else 0
    if (major, minor) >= (5, 2):
        return "effort"
    if (major, minor) >= (4, 5):
        return "thinking"
    return None


def zai_glm_reasoning_efforts_supported(
    model_id: str, provider_id: str
) -> bool | None:
    """Return the native-Z.AI GLM effort-ladder capability, when applicable."""
    classification = _config_module._zai_glm_classification(model_id, provider_id)
    if classification is None:
        return None
    return classification == "effort"


def zai_glm_thinking_toggle_supported(
    model_id: str, provider_id: str
) -> bool | None:
    """Return native-Z.AI GLM thinking-toggle capability, when applicable."""
    classification = _config_module._zai_glm_classification(model_id, provider_id)
    if classification is None:
        return None
    return classification in {"effort", "thinking"}


def filter_reasoning_efforts_for_provider(
    efforts: list[str],
    model_id: str,
    provider_id: str,
) -> list[str]:
    """Apply provider/model ceilings to valid reasoning effort levels."""
    api = _config_module
    normalized = [
        str(effort).strip().lower()
        for effort in efforts
        if str(effort).strip().lower() in api.VALID_REASONING_EFFORTS
    ]
    normalized = list(dict.fromkeys(normalized))
    provider = api._resolve_provider_alias(str(provider_id or "").strip().lower())
    bare = api._strip_provider_hint_for_reasoning(model_id).lower().rsplit("/", 1)[-1]

    if provider in {
        "openai-codex",
        "openai",
        "openai-api",
        "azure-foundry",
        "azure-openai",
        "azure",
    }:
        if bare.startswith(("o1", "o3", "o4")):
            return [effort for effort in normalized if effort in {"low", "medium", "high"}]
        if bare.startswith("gpt-5"):
            return [effort for effort in normalized if effort != "max"]

    if provider in {"gemini", "google", "google-gemini", "google-vertex", "vertex"}:
        return [effort for effort in normalized if effort != "max"]

    anthropic_lanes = {
        "anthropic",
        "claude",
        "anthropic-claude",
        "azure-foundry",
        "azure-openai",
        "azure",
        "bedrock",
        "aws-bedrock",
        "vertex",
        "google-vertex",
    }
    if (
        provider in anthropic_lanes
        and "claude" in bare
        and api._is_pre_adaptive_anthropic(bare)
    ):
        return [effort for effort in normalized if effort != "max"]

    zai_supports = api._zai_glm_reasoning_efforts_supported(model_id, provider_id)
    if zai_supports is True:
        return normalized
    if zai_supports is False:
        return []
    return normalized


def provider_known_reasoning_capable(provider_id) -> bool:
    """Return whether a provider has a known reasoning-capable lane."""
    api = _config_module
    provider = api._resolve_provider_alias(str(provider_id or "").strip().lower())
    return provider in api._KNOWN_REASONING_PROVIDERS


def heuristic_reasoning_efforts(model_id: str, provider_id: str) -> list[str]:
    """Infer effort support when authoritative metadata is unavailable."""
    api = _config_module
    model = api._strip_provider_hint_for_reasoning(model_id).lower()
    provider = api._resolve_provider_alias(str(provider_id or "").strip().lower())
    if not model or provider in {"cursor-acp", "copilot-acp"}:
        return []
    bare = model.rsplit("/", 1)[-1]
    if provider == "openai-codex" and bare.startswith(("gpt-5", "o1", "o3", "o4")):
        if bare.startswith(("o1", "o3", "o4")):
            return ["low", "medium", "high"]
        return api._filter_reasoning_efforts_for_provider(
            list(api.VALID_REASONING_EFFORTS), model, provider
        )
    if provider in {"copilot", "github-copilot"} and bare.startswith(
        ("gpt-5", "o1", "o3", "o4")
    ):
        if bare.startswith(("o1", "o3", "o4")):
            return ["low", "medium", "high"]
        return list(api.VALID_REASONING_EFFORTS)
    prefixes = (
        "deepseek/",
        "anthropic/",
        "openai/",
        "x-ai/",
        "google/gemini-2",
        "google/gemma-4",
        "qwen/qwen3",
        "tencent/hy3-preview",
        "xiaomi/",
    )
    if any(model.startswith(prefix) for prefix in prefixes):
        return list(api.VALID_REASONING_EFFORTS)
    if api._nested_gateway_route_reasoning(model):
        return list(api.VALID_REASONING_EFFORTS)
    if any(
        api._candidate_supports_reasoning(candidate)
        for candidate in api._reasoning_name_candidates(bare)
    ):
        return list(api.VALID_REASONING_EFFORTS)
    return []


def coerce_reasoning_effort_for_model(
    effort: str | None,
    model_id: str | None = None,
    provider_id: str | None = None,
    base_url: str | None = None,
) -> str:
    """Return the closest supported effort for the target model/provider."""
    api = _config_module
    raw = str(effort or "").strip().lower()
    if not raw:
        return ""
    if raw == "none" and api._zai_glm_classification(model_id, provider_id) == "forced":
        return ""
    if raw == "none":
        return "none"
    if raw not in api.VALID_REASONING_EFFORTS:
        return ""

    supported = api.resolve_model_reasoning_efforts(
        model_id,
        provider_id=provider_id,
        base_url=base_url,
    )
    ceiling = api._filter_reasoning_efforts_for_provider(
        list(api.VALID_REASONING_EFFORTS),
        str(model_id or ""),
        str(provider_id or ""),
    )
    if ceiling and raw not in ceiling:
        ladder = list(api.VALID_REASONING_EFFORTS)
        raw_index = ladder.index(raw)
        for level in reversed(ladder[:raw_index]):
            if level in ceiling:
                return level

    if not supported:
        if api._zai_glm_reasoning_efforts_supported(model_id, provider_id) is False:
            return ""
        if raw == "max" and not api._provider_known_reasoning_capable(provider_id):
            return "xhigh"
        return raw
    if raw in supported:
        return raw

    ladder = list(api.VALID_REASONING_EFFORTS)
    raw_index = ladder.index(raw)
    for level in reversed(ladder[:raw_index]):
        if level in supported:
            return level
    return raw
