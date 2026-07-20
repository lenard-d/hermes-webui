"""Model-name normalization and family classification for reasoning policy.

This module is deliberately network- and config-free.  It owns the stable
identity rules used by capability sources and provider policy; resolution and
persistence remain in their respective owners.
"""

import re


def strip_provider_hint_for_reasoning(
    model_id: str, provider: str | None = None
) -> str:
    """Remove WebUI routing hints before provider-specific capability lookup.

    Named custom-provider hints use ``@custom:<slug>:model``.  When the
    resolved provider is known, strip that complete prefix rather than leaving
    the slug attached to the model identity.
    """
    model = str(model_id or "").strip()
    if not model.startswith("@"):
        return model
    if provider:
        exact_prefix = f"@{provider}:".lower()
        if model.lower().startswith(exact_prefix):
            return model[len(exact_prefix) :]
    if ":" in model:
        return model.split(":", 1)[1]
    return model


def reasoning_name_candidates(model_id: str) -> list[str]:
    """Return normalized model-name candidates for heuristic checks."""
    bare = str(model_id or "").strip().lower().rsplit("/", 1)[-1]
    if not bare:
        return []

    candidates: list[str] = []

    def _add(value: str) -> None:
        candidate = str(value or "").strip().lower()
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    _add(bare)
    dot_parts = [part for part in bare.split(".") if part]
    if len(dot_parts) > 1:
        for index in range(1, len(dot_parts)):
            suffix = ".".join(dot_parts[index:])
            if any(ch.isalpha() for ch in suffix):
                _add(suffix)

    for candidate in list(candidates):
        _add(re.sub(r"[^a-z0-9]+", "-", candidate).strip("-"))

    return candidates


def candidate_supports_reasoning(candidate: str) -> bool:
    """Classify a normalized model-family candidate as reasoning-capable."""
    normalized = re.sub(
        r"[^a-z0-9]+", "-", str(candidate or "").strip().lower()
    ).strip("-")
    if not normalized:
        return False

    tokens = [token for token in normalized.split("-") if token]
    token_set = set(tokens)

    if "thinking" in token_set or "reasoning" in token_set:
        return True
    if "gpt" in token_set or normalized.startswith("gpt"):
        match = re.search(r"gpt-(\d+)", normalized)
        return bool(match and int(match.group(1)) >= 5)
    if normalized in {"o1", "o3", "o4"} or normalized.startswith(
        ("o1-", "o3-", "o4-")
    ):
        return True
    if "claude" in token_set or normalized.startswith("claude"):
        match = re.search(r"claude.*?(\d+)(?:\D+(\d{1,2})(?!\d))?", normalized)
        if not match:
            return False
        major = int(match.group(1))
        minor = int(match.group(2)) if match.group(2) else 0
        return major >= 4 or (major == 3 and minor >= 7)
    if "qwen" in token_set or normalized.startswith("qwen"):
        match = re.search(r"qwen.*?(\d+)(?:\D+(\d+))?", normalized)
        return bool(match and int(match.group(1)) >= 3)
    if "kimi" in token_set or normalized.startswith("kimi"):
        return True
    if "minimax" in token_set or normalized.startswith("minimax"):
        return True
    if "mimo" in token_set or normalized.startswith("mimo"):
        return True
    if "glm" in token_set or normalized.startswith("glm"):
        return True
    if "step" in token_set or normalized.startswith("step"):
        return True
    if "deepseek" in token_set:
        index = tokens.index("deepseek")
        return index + 1 < len(tokens) and tokens[index + 1].startswith(("v", "r"))
    return False


# Match nested Gemini gateway routes regardless of how many routing wrappers
# precede the route.  A negative lookbehind avoids matching inside a larger
# alphanumeric token (for example ``notvertex/gemini-x``).
NESTED_ROUTE_PATTERN = re.compile(
    r"(?<![a-z0-9])(vertex/gemini-|gemini_cli/gemini-)(.*)$"
)


def nested_route_reasoning_denied(model: str) -> bool:
    """Deny image/embedding Gemini routes even when nested behind wrappers."""
    lower = str(model or "").strip().lower()
    if not lower:
        return False
    match = NESTED_ROUTE_PATTERN.search(lower)
    if not match:
        return False
    tail = match.group(2)
    return tail.startswith("embedding") or "image" in tail or "imagine" in tail


def nested_gateway_route_reasoning(model: str) -> bool:
    """Recognize reasoning-capable nested Gemini gateway routes."""
    lower = str(model or "").strip().lower()
    if not lower:
        return False
    for prefix in ("vertex/gemini-", "gemini_cli/gemini-"):
        if not lower.startswith(prefix):
            continue
        tail = lower[len(prefix) :]
        if tail.startswith("embedding") or "image" in tail or "imagine" in tail:
            return False
        return (
            tail == "2.5"
            or tail.startswith(("2.5-", "2.5.", "3-", "3."))
            or "thinking" in tail
            or "reasoning" in tail
        )
    return False


def is_pre_adaptive_anthropic(bare_model: str) -> bool:
    """Return whether a Claude model predates adaptive thinking (4.6+)."""
    model = (bare_model or "").lower()
    if "claude" not in model:
        return False
    if re.search(r"claude-3\b", model) or re.search(r"claude-3[.\-]", model):
        return True
    match = re.search(r"(\d+)[.\-](\d{1,2})(?!\d)", model)
    if not match:
        major_only = re.search(r"[-.](\d+)(?:[-.]\d{6,})?(?:\b|-)", model)
        if major_only:
            return int(major_only.group(1)) < 5
        return False
    major, minor = int(match.group(1)), int(match.group(2))
    return (major, minor) < (4, 6)
