"""Provider identity and custom-endpoint discovery helpers.

The model catalog and active configuration remain owned by ``api.config`` for
compatibility.  Helpers resolve that facade at call time so existing patches of
``cfg``, provider tables, plugin hooks, and environment readers remain effective.
"""

import copy
import re
from typing import Protocol
from urllib.parse import urlparse

from api import config as _config_module


class ProviderDiscoveryAPI(Protocol):
    cfg: dict
    _PROVIDER_ALIASES: dict
    _PROVIDER_DISPLAY: dict
    _PROVIDER_MODELS: dict
    _LEGACY_CUSTOM_API_KEY_ENV_WARNED: set[str]
    _AMBIENT_GH_CLI_MARKERS: frozenset[str]
    _AMBIENT_GH_ENV_SOURCES: frozenset[str]
    _NOUS_FEATURED_THRESHOLD: int
    _NOUS_VENDOR_PRIORITY: tuple[str, ...]
    logger: object

    def _is_plugin_model_provider(self, provider_id: str) -> bool: ...
    def _thread_local_env_value(self, name: str) -> str: ...
    def _custom_provider_slug_from_name(self, name: object) -> str: ...
    def _custom_provider_entries(self, config_obj: dict | None = None) -> list[dict]: ...
    def _configured_model_ids(self, raw_models: object) -> list[str]: ...
    def _named_custom_provider_slug_for_provider(
        self, provider: object, config_obj: dict | None = None
    ) -> str: ...
    def _named_custom_provider_slug_for_base_url(
        self, base_url: object, config_obj: dict | None = None
    ) -> str: ...
    def _normalize_base_url_for_match(self, value: object) -> str: ...
    def _resolve_provider_alias(self, name: str) -> str: ...
    def _api_key_env_name(self, provider_id: object) -> str: ...
    def _legacy_custom_api_key_env_name(self, provider_id: object) -> str: ...
    def _get_label_for_model(self, model_id: str, existing_groups: list) -> str: ...
    def _format_ollama_label(self, model_id: str) -> str: ...
    def _strip_picker_provider_hint(self, model_id: str) -> str: ...
    def _model_matches_picker_selection(
        self,
        model_id: str,
        selected_model_id: str | None,
        provider_id: str | None = None,
    ) -> bool: ...


def _get_anthropic_fallback_env_vars() -> tuple[str, ...]:
    """Read Anthropic auth env vars from the shared agent registry when available."""
    fallback = (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
    )
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY

        anthropic = (
            PROVIDER_REGISTRY.get("anthropic")
            if isinstance(PROVIDER_REGISTRY, dict)
            else None
        )
        env_vars = getattr(anthropic, "api_key_env_vars", None)
        if not env_vars:
            return fallback

        out = []
        for _var in env_vars:
            if not isinstance(_var, str):
                continue
            _normalized = _var.strip()
            if _normalized and _normalized not in out:
                out.append(_normalized)
        return tuple(out) if out else fallback
    except Exception:
        return fallback


def _resolve_provider_alias(name: str) -> str:
    """Return the canonical provider slug for *name*.

    Applies the WebUI's local alias table first, then merges any additional
    aliases the agent provides when hermes_cli is on ``sys.path``.
    """
    if not name:
        return name
    raw = str(name).strip().lower()
    try:
        from hermes_cli.models import _PROVIDER_ALIASES as _agent_aliases

        if raw in _agent_aliases:
            return _agent_aliases[raw]
    except Exception:
        pass
    return _config_module._PROVIDER_ALIASES.get(raw, name)


def _is_known_model_provider(provider_id: str) -> bool:
    """Return whether WebUI can render the model provider in its picker."""
    pid = (provider_id or "").strip().lower()
    if not pid:
        return False
    if pid.startswith("custom:"):
        return True
    api = _config_module
    if pid in api._PROVIDER_DISPLAY or pid in api._PROVIDER_MODELS:
        return True
    try:
        if api._is_plugin_model_provider(pid):
            return True
    except Exception:
        api.logger.warning(
            "plugin model-provider check failed for %s", pid, exc_info=True
        )
    return False


def _custom_provider_slug_from_name(name: object) -> str:
    raw = str(name or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("custom:"):
        return raw
    slug = re.sub(r"[^a-z0-9._-]+", "-", raw).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        return ""
    return "custom:" + slug


def _custom_provider_entries(config_obj: dict | None = None) -> list[dict]:
    source = config_obj if isinstance(config_obj, dict) else _config_module.cfg
    entries = source.get("custom_providers", [])
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _configured_model_ids(raw_models: object) -> list[str]:
    """Return ordered model IDs from supported config allowlist shapes."""
    if isinstance(raw_models, dict):
        candidates = (key for key in raw_models if isinstance(key, str))
    elif isinstance(raw_models, list):
        candidates = raw_models
    else:
        return []

    model_ids: list[str] = []
    for item in candidates:
        if isinstance(item, dict):
            candidate = item.get("id") or item.get("model") or item.get("name")
        else:
            candidate = item
        model_id = str(candidate or "").strip()
        if model_id and model_id not in model_ids:
            model_ids.append(model_id)
    return model_ids


def _configured_model_options(raw_models: object) -> list[dict[str, str]]:
    """Return picker option rows from supported config allowlist shapes."""
    labels: dict[str, str] = {}
    if isinstance(raw_models, list):
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            candidate = item.get("id") or item.get("model") or item.get("name")
            model_id = str(candidate or "").strip()
            if not model_id or model_id in labels:
                continue
            label = str(item.get("label") or model_id).strip() or model_id
            labels[model_id] = label
    return [
        {"id": model_id, "label": labels.get(model_id, model_id)}
        for model_id in _config_module._configured_model_ids(raw_models)
    ]


def _named_custom_provider_slugs(config_obj: dict | None = None) -> set[str]:
    api = _config_module
    return {
        slug
        for slug in (
            api._custom_provider_slug_from_name(entry.get("name"))
            for entry in api._custom_provider_entries(config_obj)
        )
        if slug
    }


def _named_custom_provider_slug_for_provider(
    provider: object,
    config_obj: dict | None = None,
) -> str:
    raw = str(provider or "").strip().lower()
    if not raw:
        return ""
    api = _config_module
    raw_suffix = raw.removeprefix("custom:")
    for entry in api._custom_provider_entries(config_obj):
        entry_name = str(entry.get("name") or "").strip().lower()
        slug = api._custom_provider_slug_from_name(entry_name)
        if not entry_name or not slug:
            continue
        if raw in {entry_name, slug} or raw_suffix == slug.removeprefix("custom:"):
            return slug
    return ""


def _resolve_configured_provider_id(
    provider: object,
    config_obj: dict | None = None,
    *,
    base_url: object = None,
    resolve_alias: bool = True,
) -> str:
    """Normalize configured and named-custom provider identifiers."""
    api = _config_module
    named_slug = api._named_custom_provider_slug_for_provider(provider, config_obj)
    if named_slug:
        return named_slug

    if not resolve_alias:
        raw = str(provider or "").strip().lower()
        if base_url and raw == "custom":
            by_base_url = api._named_custom_provider_slug_for_base_url(
                base_url, config_obj
            )
            if by_base_url:
                return by_base_url
        return str(provider or "")

    resolved = api._resolve_provider_alias(provider)
    if base_url and str(resolved or "").strip().lower() == "custom":
        by_base_url = api._named_custom_provider_slug_for_base_url(base_url, config_obj)
        if by_base_url:
            return by_base_url
    return resolved


def _canonicalise_provider_id(name: object) -> str:
    """Normalise a provider id into a stable lowercase-hyphenated form."""
    if not name:
        return ""
    raw = str(name).strip().lower().replace("_", "-")
    if not raw:
        return ""
    api = _config_module
    if raw in api._PROVIDER_DISPLAY or raw in api._PROVIDER_MODELS:
        return raw
    resolved = api._resolve_provider_alias(raw)
    if resolved and (
        resolved.lower() in api._PROVIDER_DISPLAY
        or resolved.lower() in api._PROVIDER_MODELS
    ):
        return resolved.lower()
    return raw


def _normalize_base_url_for_match(value: object) -> str:
    url = str(value or "").strip().rstrip("/")
    if not url:
        return ""
    parsed_url = urlparse(url if "://" in url else f"http://{url}")
    scheme = (parsed_url.scheme or "http").lower()
    netloc = (parsed_url.netloc or parsed_url.path).lower().rstrip("/")
    path = parsed_url.path.rstrip("/")
    if not parsed_url.netloc:
        path = ""
    return f"{scheme}://{netloc}{path}"


def _custom_endpoint_slugs_for_base_url(value: object) -> set[str]:
    """Return endpoint-derived custom provider slugs for a base URL."""
    url = str(value or "").strip().rstrip("/")
    if not url:
        return set()
    parsed_url = urlparse(url if "://" in url else f"http://{url}")
    host = (parsed_url.hostname or "").strip().lower()
    if not host:
        return set()
    port = parsed_url.port
    if port is None:
        scheme = (parsed_url.scheme or "http").lower()
        port = 443 if scheme == "https" else 80
    return {f"custom:{host}:{port}", f"custom:{host}-{port}"}


def _api_key_env_name(provider_id: object) -> str:
    """Return the POSIX-safe default API-key env var for a custom provider id."""
    sanitized = re.sub(r"[^A-Za-z0-9]", "_", str(provider_id or "")).upper().strip("_")
    if not sanitized:
        sanitized = "CUSTOM"
    if not sanitized.startswith("CUSTOM_"):
        sanitized = f"CUSTOM_{sanitized}"
    return f"{sanitized}_API_KEY"


def _legacy_custom_api_key_env_name(provider_id: object) -> str:
    """Return the pre-#2541 custom-provider env hint shape, if any."""
    raw = str(provider_id or "").strip().upper()
    if not raw:
        return ""
    return f"{raw}_API_KEY"


def _lookup_custom_api_key_env(provider_id: object) -> str | None:
    """Look up sanitized custom-provider env first, then legacy broken shape."""
    api = _config_module
    env_name = api._api_key_env_name(provider_id)
    api_key = api._thread_local_env_value(env_name).strip()
    if api_key:
        return api_key

    legacy_env_name = api._legacy_custom_api_key_env_name(provider_id)
    if legacy_env_name and legacy_env_name != env_name:
        legacy_key = api._thread_local_env_value(legacy_env_name).strip()
        if legacy_key:
            if legacy_env_name not in api._LEGACY_CUSTOM_API_KEY_ENV_WARNED:
                api._LEGACY_CUSTOM_API_KEY_ENV_WARNED.add(legacy_env_name)
                api.logger.warning(
                    "Custom provider API key env var %s is deprecated; use %s instead",
                    legacy_env_name,
                    env_name,
                )
            return legacy_key
    return None


def _named_custom_provider_slug_for_base_url(
    base_url: object,
    config_obj: dict | None = None,
) -> str:
    api = _config_module
    target = api._normalize_base_url_for_match(base_url)
    if not target:
        return ""
    for entry in api._custom_provider_entries(config_obj):
        entry_base_url = api._normalize_base_url_for_match(entry.get("base_url"))
        if entry_base_url != target:
            continue
        return api._custom_provider_slug_from_name(entry.get("name")) or "custom"
    return ""


def _provider_is_known_or_configured(
    provider_id: object,
    config_obj: dict | None = None,
) -> bool:
    """Return whether static registry or user config recognizes a provider."""
    raw = str(provider_id or "").strip().lower()
    if not raw:
        return False
    api = _config_module
    if api._named_custom_provider_slug_for_provider(raw, config_obj):
        return True
    if raw == "custom" or raw.startswith("custom:"):
        return bool(api._custom_provider_entries(config_obj))
    canonical = api._resolve_provider_alias(raw)
    return (
        raw in api._PROVIDER_DISPLAY
        or canonical in api._PROVIDER_DISPLAY
        or raw in api._PROVIDER_MODELS
        or canonical in api._PROVIDER_MODELS
    )


def _seed_provider_models_from_core() -> None:
    """Enrich existing provider model lists with missing IDs from hermes_cli.

    The core list is authoritative for agent-capable models, while the WebUI
    list is display-oriented. Only providers already curated by the WebUI are
    enriched, and their existing ID-prefix convention is preserved.
    """
    try:
        from hermes_cli.models import _PROVIDER_MODELS as core_provider_models
    except ImportError:
        return

    api = _config_module
    webui_key_by_canonical: dict[str, str] = {}
    for webui_key in api._PROVIDER_MODELS:
        try:
            canonical = api._resolve_provider_alias(webui_key)
        except Exception:
            canonical = webui_key
        if canonical not in webui_key_by_canonical:
            webui_key_by_canonical[canonical] = webui_key

    for provider_id, core_models in core_provider_models.items():
        if not isinstance(core_models, list):
            continue

        webui_list = api._PROVIDER_MODELS.get(provider_id)
        if webui_list is None:
            try:
                canonical_provider_id = api._resolve_provider_alias(provider_id)
            except Exception:
                canonical_provider_id = provider_id
            webui_key = webui_key_by_canonical.get(
                canonical_provider_id, provider_id
            )
            webui_list = api._PROVIDER_MODELS.get(webui_key)

        if not isinstance(webui_list, list):
            continue

        existing_ids_raw: list[str] = [
            (model.get("id") if isinstance(model, dict) else str(model)) or ""
            for model in webui_list
            if isinstance(model, dict) and model.get("id")
        ]
        prefix = ""
        if existing_ids_raw and all(
            model_id.startswith("@") and ":" in model_id
            for model_id in existing_ids_raw
        ):
            prefix = existing_ids_raw[0].split(":", 1)[0] + ":"

        def _strip_prefix(model_id: str, prefix_value: str = prefix) -> str:
            if prefix_value and model_id.startswith(prefix_value):
                return model_id[len(prefix_value) :]
            return model_id

        existing_ids = {
            _strip_prefix(model_id).replace("-", ".").lower()
            for model_id in existing_ids_raw
        }
        for model_id in core_models:
            if not isinstance(model_id, str) or not model_id.strip():
                continue
            stripped_id = model_id.strip()
            normalized_id = stripped_id.replace("-", ".").lower()
            if normalized_id in existing_ids:
                continue
            injected_id = (prefix + stripped_id) if prefix else stripped_id
            webui_list.append(
                {
                    "id": injected_id,
                    "label": api._get_label_for_model(stripped_id, []),
                }
            )


_AMBIENT_GH_CLI_MARKERS = frozenset({"gh_cli", "gh auth token"})
_AMBIENT_GH_ENV_SOURCES = frozenset({"env:github_token", "env:gh_token"})


def _is_ambient_gh_cli_entry(source: str, label: str, key_source: str) -> bool:
    """Return whether a credential was ambiently seeded from GitHub CLI."""
    source_lower = source.strip().lower()
    api = _config_module
    return (
        source_lower in api._AMBIENT_GH_CLI_MARKERS
        or source_lower in api._AMBIENT_GH_ENV_SOURCES
        or label.strip().lower() == "gh auth token"
        or key_source.strip().lower() == "gh auth token"
    )


def _format_ollama_label(model_id: str) -> str:
    """Turn an Ollama tag-shaped model ID into a readable display label."""
    name_part, _, variant = model_id.partition(":")

    def _format_tokens(value: str) -> str:
        tokens = value.replace("-", " ").replace("_", " ").split()
        formatted = []
        for token in tokens:
            alpha_only = token.replace(".", "")
            if alpha_only.isalpha() and len(token) <= 3:
                formatted.append(token.upper())
            elif alpha_only.isalnum() and alpha_only and alpha_only[0].isdigit():
                formatted.append(token.upper())
            else:
                formatted.append(
                    token[0].upper() + token[1:] if token else token
                )
        return " ".join(formatted)

    label = _format_tokens(name_part)
    if variant:
        label += f" ({_format_tokens(variant)})"
    return label


def _format_nous_label(model_id: str) -> str:
    """Turn a Nous Portal model ID into a readable provider-distinct label."""
    name_part = model_id.split("/", 1)[-1] if "/" in model_id else model_id
    if name_part.lower().startswith("minimax"):
        name_part = "MiniMax" + name_part[len("minimax") :]
    return f"{_config_module._format_ollama_label(name_part)} (via Nous)"


_NOUS_FEATURED_THRESHOLD = 25
_NOUS_FEATURED_TARGET = 15
_MODEL_PICKER_OVERFLOW_THRESHOLD = _NOUS_FEATURED_THRESHOLD
_MODEL_PICKER_VISIBLE_TARGET = _NOUS_FEATURED_TARGET
_OPENROUTER_FREE_TIER_AUGMENT_CAP = 30
_NOUS_VENDOR_PRIORITY = (
    "anthropic",
    "openai",
    "google",
    "moonshotai",
    "z-ai",
    "minimax",
    "qwen",
    "x-ai",
    "deepseek",
    "stepfun",
    "xiaomi",
    "tencent",
    "nvidia",
    "arcee-ai",
)


def _build_nous_featured_set(
    live_ids: list[str],
    *,
    selected_model_id: str | None = None,
    target: int = _NOUS_FEATURED_TARGET,
) -> tuple[list[str], list[str]]:
    """Split a large Nous catalog into featured rows and a complete overflow."""
    if not live_ids:
        return [], []
    api = _config_module
    if len(live_ids) <= api._NOUS_FEATURED_THRESHOLD:
        return list(live_ids), []

    chosen: list[str] = []
    chosen_set: set[str] = set()

    def _add(model_id: str) -> None:
        if model_id and model_id not in chosen_set:
            chosen.append(model_id)
            chosen_set.add(model_id)

    if selected_model_id:
        selected = selected_model_id
        if selected.startswith("@nous:"):
            selected = selected[len("@nous:") :]
        if selected in live_ids:
            _add(selected)

    for static_model in api._PROVIDER_MODELS.get("nous", []):
        static_id = static_model.get("id", "")
        if static_id.startswith("@nous:"):
            static_id = static_id[len("@nous:") :]
        if static_id in live_ids:
            _add(static_id)

    by_vendor: dict[str, list[str]] = {}
    for model_id in live_ids:
        if model_id in chosen_set:
            continue
        vendor = model_id.split("/", 1)[0] if "/" in model_id else ""
        by_vendor.setdefault(vendor, []).append(model_id)

    priority = list(api._NOUS_VENDOR_PRIORITY)
    leftover = sorted(vendor for vendor in by_vendor if vendor not in set(priority))
    vendor_order = priority + leftover
    while len(chosen) < target:
        added_this_pass = 0
        for vendor in vendor_order:
            if len(chosen) >= target:
                break
            bucket = by_vendor.get(vendor)
            if not bucket:
                continue
            _add(bucket.pop(0))
            added_this_pass += 1
        if added_this_pass == 0:
            break

    extras = [model_id for model_id in live_ids if model_id not in chosen_set]
    return chosen, extras


def _strip_picker_provider_hint(model_id: str) -> str:
    normalized = str(model_id or "").strip()
    if normalized.startswith("@") and ":" in normalized:
        return normalized[normalized.index(":") + 1 :]
    return normalized


def _model_matches_picker_selection(
    model_id: str,
    selected_model_id: str | None,
    provider_id: str | None = None,
) -> bool:
    selected = str(selected_model_id or "").strip()
    candidate = str(model_id or "").strip()
    if not selected or not candidate:
        return False
    if candidate == selected:
        return True

    api = _config_module
    selected_bare = api._strip_picker_provider_hint(selected)
    candidate_bare = api._strip_picker_provider_hint(candidate)
    if selected_bare != candidate_bare:
        return False

    selected_provider = ""
    if selected.startswith("@") and ":" in selected:
        selected_provider = selected[1 : selected.index(":")].lower()
    candidate_provider = str(provider_id or "").strip().lower()
    if candidate.startswith("@") and ":" in candidate:
        candidate_provider = candidate[1 : candidate.index(":")].lower()
    return (
        not selected_provider
        or not candidate_provider
        or selected_provider == candidate_provider
    )


def _split_picker_overflow_models(
    ordered_models: list[dict],
    *,
    selected_model_id: str | None = None,
    provider_id: str | None = None,
    threshold: int = _MODEL_PICKER_OVERFLOW_THRESHOLD,
    target: int = _MODEL_PICKER_VISIBLE_TARGET,
) -> tuple[list[dict], list[dict]]:
    """Split an ordered picker catalog into visible rows plus an overflow tail."""
    models = [
        copy.deepcopy(model)
        for model in (ordered_models or [])
        if isinstance(model, dict) and model.get("id")
    ]
    if len(models) <= threshold:
        return models, []

    visible = models[:target]
    extras = models[target:]
    if not selected_model_id:
        return visible, extras

    api = _config_module
    if any(
        api._model_matches_picker_selection(
            model.get("id", ""), selected_model_id, provider_id
        )
        for model in visible
    ):
        return visible, extras

    for index, model in enumerate(extras):
        if not api._model_matches_picker_selection(
            model.get("id", ""), selected_model_id, provider_id
        ):
            continue
        displaced = visible[-1]
        visible[-1] = model
        extras[index] = displaced
        break
    return visible, extras


def _apply_provider_prefix(
    raw_models: list[dict],
    provider_id: str,
    active_provider: str | None,
) -> list[dict]:
    """Apply an explicit provider hint to otherwise ambiguous model IDs."""
    active = (active_provider or "").lower()
    if not active or provider_id == active:
        return list(raw_models)
    result = []
    for model in raw_models:
        model_id = model["id"]
        entry = dict(model)
        if model_id.startswith("@") or "/" in model_id:
            result.append(entry)
        else:
            entry["id"] = f"@{provider_id}:{model_id}"
            result.append(entry)
    return result


def _deduplicate_model_ids(groups: list[dict]) -> None:
    """Make colliding model IDs globally unique across picker groups in-place."""
    if not groups:
        return

    sorted_group_indices = sorted(
        range(len(groups)),
        key=lambda index: groups[index].get("provider_id", ""),
    )
    id_map: dict[str, list[tuple[int, str, int]]] = {}
    for group_index in sorted_group_indices:
        group = groups[group_index]
        for bucket_name in ("models", "extra_models"):
            for model_index, model in enumerate(group.get(bucket_name, []) or []):
                model_id = str(model.get("id", "") or "").strip()
                if not model_id or model_id.startswith("@"):
                    continue
                id_map.setdefault(model_id, []).append(
                    (group_index, bucket_name, model_index)
                )

    for original_id, locations in id_map.items():
        if len(locations) < 2:
            continue
        for group_index, bucket_name, model_index in locations[1:]:
            group = groups[group_index]
            model = group[bucket_name][model_index]
            provider_id = group.get("provider_id", "")
            model["id"] = f"@{provider_id}:{original_id}"
            provider_name = group.get("provider", provider_id)
            if model.get("label") != original_id:
                model["label"] = f"{model['label']} ({provider_name})"
            else:
                model["label"] = f"{original_id} ({provider_name})"
