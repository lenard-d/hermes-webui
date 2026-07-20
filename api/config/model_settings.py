"""Advanced and auxiliary model settings policy and persistence."""

import copy
import json

from api import config as _config_module


def _parse_positive_int_config_value(raw) -> int | None:
    if raw is None:
        return None
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def get_max_tokens_status() -> dict[str, int | None]:
    """Return the Settings-facing max_tokens state from the active profile config.

    ``max_tokens`` is the root override the Settings field owns directly.
    ``max_tokens_fallback`` is the agent-level fallback when the root config
    resolves to ``None``, matching the streaming path exactly.
    ``max_tokens_effective`` is the runtime cap a new streaming turn would
    currently use.
    """
    api = _config_module
    config_data = api._load_yaml_config_file(api._get_config_path())
    if not isinstance(config_data, dict):
        return {
            "max_tokens": None,
            "max_tokens_effective": None,
            "max_tokens_fallback": None,
        }

    raw_root_value = config_data.get("max_tokens")
    root_value = api._parse_positive_int_config_value(raw_root_value)

    fallback_value = None
    if raw_root_value is None:
        agent_cfg = config_data.get("agent")
        if isinstance(agent_cfg, dict):
            fallback_value = api._parse_positive_int_config_value(
                agent_cfg.get("max_tokens")
            )

    effective_value = root_value if root_value is not None else fallback_value
    return {
        "max_tokens": root_value,
        "max_tokens_effective": effective_value,
        "max_tokens_fallback": fallback_value,
    }


def set_max_tokens(max_tokens) -> dict[str, int | None]:
    """Persist a root-level ``max_tokens`` override to the active profile config.

    Blank/``None`` clears the root override so ``agent.max_tokens`` can resume.
    Positive integers are written to the active profile's ``config.yaml``.
    Unrelated YAML keys are preserved verbatim.
    """
    api = _config_module
    if isinstance(max_tokens, str):
        max_tokens = max_tokens.strip()
    clear_root = max_tokens in (None, "")
    parsed_max_tokens = api._parse_positive_int_config_value(max_tokens)
    if not clear_root and parsed_max_tokens is None:
        return api.get_max_tokens_status()

    config_path = api._get_config_path()
    should_save = True
    with api._cfg_lock:
        config_data = api._load_yaml_config_file_raw(config_path)
        if clear_root:
            if "max_tokens" not in config_data:
                should_save = False
            else:
                config_data.pop("max_tokens", None)
        elif parsed_max_tokens is not None:
            config_data["max_tokens"] = parsed_max_tokens
        if should_save:
            api._save_yaml_config_file(config_path, config_data)
    if not should_save:
        return api.get_max_tokens_status()
    api.reload_config()
    return api.get_max_tokens_status()


def set_reasoning_display(show: bool) -> dict:
    """Persist ``display.show_reasoning`` to the active profile's config.yaml.

    Mirrors CLI ``/reasoning show|hide``: writes the same key that the CLI
    writes, so the preference is shared across the WebUI and the terminal
    REPL for the same profile.
    """
    api = _config_module
    config_path = api._get_config_path()
    with api._cfg_lock:
        config_data = api._load_yaml_config_file(config_path)
        display_cfg = config_data.get("display")
        if not isinstance(display_cfg, dict):
            display_cfg = {}
        display_cfg["show_reasoning"] = bool(show)
        config_data["display"] = display_cfg
        api._save_yaml_config_file(config_path, config_data)
    api.reload_config()
    return api.get_reasoning_status()


def set_reasoning_effort(
    effort: str,
    *,
    model_id: str | None = None,
    provider_id: str | None = None,
    base_url: str | None = None,
) -> dict:
    """Persist ``agent.reasoning_effort`` to the active profile's config.yaml.

    Mirrors CLI ``/reasoning <level>``: same key, same valid values
    (``none`` | ``minimal`` | ``low`` | ``medium`` | ``high`` | ``xhigh`` | ``max``).

    An empty string is accepted as "clear the override" — it removes the
    ``agent.reasoning_effort`` key so the provider default takes effect. This is
    the re-enable path for thinking-toggle-only models (GLM-4.5–5.1 on native
    zai): the dropdown's "Default"/"On" option POSTs ``effort:''`` to switch
    thinking back on after the user selected "None". Without this, the toggle
    would be one-way (off-only) for those models. (#6219 round-3)

    Raises ``ValueError`` on any other unrecognised level so callers can 400.
    """
    api = _config_module
    raw = str(effort or "").strip().lower()
    if raw and raw != "none" and raw not in api.VALID_REASONING_EFFORTS:
        raise ValueError(
            f"Unknown reasoning effort '{effort}'. "
            f"Valid: none, {', '.join(api.VALID_REASONING_EFFORTS)}."
        )
    config_path = api._get_config_path()
    with api._cfg_lock:
        config_data = api._load_yaml_config_file(config_path)
        agent_cfg = config_data.get("agent")
        if not isinstance(agent_cfg, dict):
            agent_cfg = {}
        if raw:
            agent_cfg["reasoning_effort"] = raw
        else:
            # Clear the override so the provider default takes effect (the
            # "Default"/"On" re-enable path for thinking-toggle-only models).
            # Drop the key entirely rather than writing an empty string so the
            # CLI's "is reasoning_effort configured?" check stays simple.
            agent_cfg.pop("reasoning_effort", None)
        config_data["agent"] = agent_cfg
        api._save_yaml_config_file(config_path, config_data)
    api.reload_config()
    return api.get_reasoning_status(
        model_id=model_id,
        provider_id=provider_id,
        base_url=base_url,
    )


def _public_advanced_model_options(model_cfg: dict) -> dict:
    """Return write-only-safe advanced options from a model config block."""
    if not isinstance(model_cfg, dict):
        model_cfg = {}
    return {
        "base_url": str(model_cfg.get("base_url") or "").strip(),
        "timeout": model_cfg.get("timeout", ""),
        "download_timeout": model_cfg.get("download_timeout", ""),
        "max_concurrency": model_cfg.get("max_concurrency", ""),
        "extra_body": model_cfg.get("extra_body")
        if isinstance(model_cfg.get("extra_body"), dict)
        else {},
        "api_key_set": bool(str(model_cfg.get("api_key") or "").strip()),
    }


def _is_openai_family_provider(provider: str | None) -> bool:
    """Return True when a provider should receive OpenAI-family request overrides."""
    if not provider:
        return False
    resolved = str(_config_module._resolve_provider_alias(str(provider).strip().lower()))
    return resolved in ("openai", "openai-api", "openai-codex")


def _normalize_openai_family_model_id(model_id: str | None) -> str:
    """Return a model id in the form expected by hermes_cli fast-mode resolution."""
    model = str(model_id or "").strip()
    if not model:
        return ""

    if model.startswith("@") and ":" in model:
        model = model.split(":", 1)[1].strip()

    if "://" in model:
        return model

    if "/" in model:
        provider_hint, candidate = model.split("/", 1)
        if provider_hint.strip().lower() in {"openai", "openai-api", "openai-codex"}:
            model = candidate.strip()
        else:
            return ""

    return model


def _legacy_openai_service_tier_overrides(
    model_id: str | None, provider: str | None
) -> dict:
    """Compatibility fallback for standalone WebUI installs without hermes_cli.

    Normal operation delegates to Hermes Agent model metadata.  This fallback
    preserves the old WebUI behavior when the agent package is unavailable,
    while still failing closed for codex model slugs and foreign provider IDs.
    """
    api = _config_module
    if not api._is_openai_family_provider(provider):
        return {}
    resolved_provider = str(
        api._resolve_provider_alias(str(provider or "").strip().lower())
    )
    raw_model = str(model_id or "").strip()
    if "://" not in raw_model and "/" in raw_model:
        provider_hint = raw_model.split("/", 1)[0].strip().lower()
        if provider_hint not in {"openai", "openai-api", "openai-codex"}:
            return {}
    normalized_model = api._normalize_openai_family_model_id(model_id)
    if not normalized_model:
        if resolved_provider == "openai-codex":
            return {}
        return {"service_tier": "priority"}
    lowered = normalized_model.lower()
    if "codex" in lowered:
        return {}
    if lowered.startswith(("gpt-", "o1", "o3", "o4")):
        return {"service_tier": "priority"}
    return {}


def _resolve_main_model_fast_mode_overrides(
    model_id: str | None, provider: str | None = None
) -> dict:
    """Return provider request overrides for the main model fast-mode setting."""
    api = _config_module
    normalized_model = api._normalize_openai_family_model_id(model_id)
    if not normalized_model:
        return api._legacy_openai_service_tier_overrides(model_id, provider)
    try:
        from hermes_cli.models import resolve_fast_mode_overrides
    except Exception:
        api.logger.debug(
            "Failed to import hermes_cli.models.resolve_fast_mode_overrides; using WebUI compatibility fallback."
        )
        return api._legacy_openai_service_tier_overrides(model_id, provider)
    try:
        resolved = resolve_fast_mode_overrides(normalized_model)
    except Exception:
        api.logger.debug(
            "Failed to resolve fast-mode overrides for %r; using WebUI compatibility fallback.",
            normalized_model,
        )
        return api._legacy_openai_service_tier_overrides(model_id, provider)
    return resolved if isinstance(resolved, dict) else {}


def _main_model_supports_service_tier(
    model_id: str | None,
    provider: str | None,
) -> bool:
    """Return True when the current main-model selection can use OpenAI service tier."""
    api = _config_module
    if not api._is_openai_family_provider(provider):
        return False
    return (
        str(
            api._resolve_main_model_fast_mode_overrides(model_id, provider).get(
                "service_tier", ""
            )
        )
        .strip()
        .lower()
        == "priority"
    )


def _model_supports_fast_tier_for_provider(
    model_id: str | None, provider: str | None
) -> bool:
    """Return whether a provider/model entry supports WebUI's service-tier toggle."""
    return _config_module._main_model_supports_service_tier(model_id, provider)


def _annotate_fast_tier_model_groups(payload: dict | None) -> dict | None:
    """Add service-tier capability metadata to OpenAI-family model groups."""
    if not isinstance(payload, dict):
        return payload
    groups = payload.get("groups")
    if not isinstance(groups, list):
        return payload
    api = _config_module
    for group in groups:
        if not isinstance(group, dict):
            continue
        provider_id = str(group.get("provider_id") or "").strip()
        if not api._is_openai_family_provider(provider_id):
            continue
        for bucket in ("models", "extra_models"):
            models = group.get(bucket)
            if not isinstance(models, list):
                continue
            for model in models:
                if not isinstance(model, dict):
                    continue
                model_id = str(model.get("id") or "").strip()
                if model_id:
                    model["supports_fast_tier"] = (
                        api._model_supports_fast_tier_for_provider(
                            model_id, provider_id
                        )
                    )
    return payload


def _public_main_service_tier(model_cfg: dict) -> str:
    """Return the saved main-model service tier only for OpenAI-family providers."""
    if not isinstance(model_cfg, dict):
        return ""
    api = _config_module
    model_id = str(model_cfg.get("default") or model_cfg.get("name") or "").strip()
    provider = str(model_cfg.get("provider") or "").strip().lower()
    if not provider:
        _, provider, _ = api.resolve_model_provider(model_id)
    if not api._main_model_supports_service_tier(model_id, provider):
        return ""
    service_tier = str(model_cfg.get("service_tier") or "").strip().lower()
    return "priority" if service_tier == "priority" else ""


def _main_model_request_overrides(
    config_data: dict,
    effective_model: str | None = None,
    effective_provider: str | None = None,
) -> dict:
    """Return supported runtime request overrides for the main chat model.

    When *effective_model* / *effective_provider* are supplied, the
    service-tier gate checks those instead of the saved default model,
    so a per-session model switch to a non-OpenAI provider does not
    leak ``service_tier`` onto an unsupported request.
    """
    if not isinstance(config_data, dict):
        return {}
    model_cfg = config_data.get("model", {})
    if not isinstance(model_cfg, dict):
        return {}
    api = _config_module
    overrides = {}
    gate_model = effective_model
    gate_provider = effective_provider
    if not gate_model:
        gate_model = str(
            model_cfg.get("default") or model_cfg.get("name") or ""
        ).strip()
    if not gate_provider:
        gate_provider = str(model_cfg.get("provider") or "").strip().lower()
        if not gate_provider:
            _, gate_provider, _ = api.resolve_model_provider(gate_model)
    if api._main_model_supports_service_tier(gate_model, gate_provider):
        service_tier = str(model_cfg.get("service_tier") or "").strip().lower()
        if service_tier == "priority":
            overrides["service_tier"] = "priority"
    extra_body = model_cfg.get("extra_body")
    if isinstance(extra_body, dict) and extra_body:
        overrides["extra_body"] = copy.deepcopy(extra_body)
    return overrides


def _apply_advanced_model_options(model_cfg: dict, advanced: dict | None) -> None:
    """Apply supported advanced model options to a config block in-place."""
    if advanced is None:
        return
    if not isinstance(advanced, dict):
        raise ValueError("advanced model options must be an object")
    if "base_url" in advanced:
        base_url = str(advanced.get("base_url") or "").strip().rstrip("/")
        if base_url:
            model_cfg["base_url"] = base_url
        else:
            model_cfg.pop("base_url", None)
    for field in ("timeout", "download_timeout", "max_concurrency"):
        if field in advanced:
            coerced = _config_module._coerce_optional_positive_int(
                advanced.get(field), field
            )
            if coerced == "":
                model_cfg.pop(field, None)
            elif coerced is not None:
                model_cfg[field] = coerced
    if "extra_body" in advanced:
        extra_body = advanced.get("extra_body")
        if isinstance(extra_body, str):
            text = extra_body.strip()
            try:
                extra_body = json.loads(text) if text else {}
            except json.JSONDecodeError as exc:
                raise ValueError("extra_body must be valid JSON") from exc
        if extra_body in (None, ""):
            model_cfg.pop("extra_body", None)
        elif isinstance(extra_body, dict):
            if extra_body:
                model_cfg["extra_body"] = extra_body
            else:
                model_cfg.pop("extra_body", None)
        else:
            raise ValueError("extra_body must be a JSON object")
    if "service_tier" in advanced:
        service_tier = str(advanced.get("service_tier") or "").strip().lower()
        if not service_tier or service_tier == "default":
            model_cfg.pop("service_tier", None)
        elif service_tier == "priority":
            model_cfg["service_tier"] = "priority"
        else:
            raise ValueError("service_tier must be one of: default, priority")
    if advanced.get("api_key_clear"):
        model_cfg.pop("api_key", None)
    api_key = str(advanced.get("api_key") or "").strip()
    if api_key:
        model_cfg["api_key"] = api_key


def set_hermes_default_model(
    model_id: str, provider: str | None = None, advanced: dict | None = None
) -> dict:
    """Persist the Hermes default model in config.yaml and reload runtime config."""
    selected_model = str(model_id or "").strip()
    if not selected_model:
        raise ValueError("model is required")

    api = _config_module
    config_path = api._get_config_path()
    # Hold _cfg_lock only around the read-modify-write of the YAML file.
    # reload_config() acquires _cfg_lock internally (it's not reentrant) so
    # it must be called AFTER releasing the lock to avoid deadlock.
    with api._cfg_lock:
        config_data = api._load_yaml_config_file(config_path)
        model_cfg = config_data.get("model", {})
        if not isinstance(model_cfg, dict):
            model_cfg = {}

        previous_provider = str(model_cfg.get("provider") or "").strip()
        requested_provider = str(provider or "").strip()
        resolved_model, resolved_provider, resolved_base_url = (
            api.resolve_model_provider(selected_model)
        )
        # Persist the resolved bare/slash form, NOT the `@provider:` prefix. The
        # prefix is a WebUI-internal routing hint that the hermes-agent CLI does
        # not understand — if we wrote `@nous:anthropic/claude-opus-4.6` to
        # config.yaml, a user who ran `hermes` in the terminal right after
        # saving via WebUI would have the agent send that literal string to the
        # Nous API, which would reject it (Nous expects `anthropic/claude-opus-4.6`,
        # not the prefixed form). The Settings picker handles the resulting
        # CLI-shaped bare form via `_applyModelToDropdown()`'s normalising
        # matcher — see `static/panels.js` (#895).
        persisted_model = str(resolved_model or selected_model).strip()
        persisted_provider = str(
            requested_provider or resolved_provider or previous_provider or ""
        ).strip()
        provider_override_won = bool(
            requested_provider
            and requested_provider != str(resolved_provider or "").strip()
        )
        # Never persist the bogus ``local`` value — see #1384. The auto-detect
        # block in ``_build_available_models_uncached`` was rewriting unknown
        # loopback hosts to ``provider: "local"``, which is not registered and
        # broke compression/vision mid-conversation. Route through ``custom``
        # so the agent's auxiliary client uses the ``no-key-required`` path.
        if persisted_provider.lower() == "local":
            persisted_provider = "custom"

        model_cfg["default"] = persisted_model
        if persisted_provider:
            model_cfg["provider"] = persisted_provider

        if resolved_base_url and not provider_override_won:
            model_cfg["base_url"] = str(resolved_base_url).strip().rstrip("/")
        elif persisted_provider != previous_provider:
            if persisted_provider == "openai":
                model_cfg["base_url"] = "https://api.openai.com/v1"
            else:
                # Provider changed and we have no resolved URL for the new one.
                # Drop the previous provider's base_url so New Chat doesn't route
                # to the old endpoint — this MUST also cover custom:* providers
                # (a different custom provider has a different URL); leaving the
                # stale base_url sent requests to the wrong host (#4728).
                model_cfg.pop("base_url", None)

        api._apply_advanced_model_options(model_cfg, advanced)
        if not api._main_model_supports_service_tier(
            persisted_model, persisted_provider
        ):
            model_cfg.pop("service_tier", None)

        config_data["model"] = model_cfg
        api._save_yaml_config_file(config_path, config_data)
    # Reload outside the lock — reload_config() acquires _cfg_lock itself.
    api.reload_config()
    # Invalidate the TTL cache so the next /api/models call returns fresh data
    # with the new default model. Do NOT call get_available_models() here —
    # it triggers a live provider fetch (up to 8s) that blocks the HTTP response
    # to the browser, causing a visible freeze on every Settings save (#895).
    api.invalidate_models_cache()
    return {
        "ok": True,
        "model": persisted_model,
        "provider": persisted_provider or None,
    }


# ── Auxiliary model configuration ──────────────────────────────────────────

# Canonical auxiliary task catalog.
# Keep in sync with hermes_cli/config.py DEFAULT_CONFIG["auxiliary"] and
# hermes_cli/web_server.py _AUX_TASK_SLOTS.
AUXILIARY_TASK_CATALOG: tuple[dict[str, str], ...] = (
    {"key": "vision", "label": "Vision", "description": "image/screenshot analysis"},
    {
        "key": "web_extract",
        "label": "Web extract",
        "description": "web page summarization",
    },
    {
        "key": "compression",
        "label": "Compression",
        "description": "context summarization",
    },
    {
        "key": "approval",
        "label": "Approval",
        "description": "smart command approval",
    },
    {"key": "mcp", "label": "MCP", "description": "MCP tool reasoning"},
    {
        "key": "title_generation",
        "label": "Title generation",
        "description": "session titles",
    },
    {
        "key": "skills_hub",
        "label": "Skills hub",
        "description": "skills search/install",
    },
    {
        "key": "curator",
        "label": "Curator",
        "description": "skill-usage review pass",
    },
    {
        "key": "kanban_decomposer",
        "label": "Kanban decomposer",
        "description": "task decomposition",
    },
    {
        "key": "profile_describer",
        "label": "Profile describer",
        "description": "profile summaries",
    },
    {
        "key": "triage_specifier",
        "label": "Triage specifier",
        "description": "issue/task triage specs",
    },
)

AUX_TASK_SLOTS: tuple[str, ...] = tuple(item["key"] for item in AUXILIARY_TASK_CATALOG)

# Slots removed from the WebUI catalog whose persisted assignments should be
# discarded when the user explicitly resets all auxiliary-model routing.
RETIRED_AUX_TASK_SLOTS: tuple[str, ...] = ("session_search",)


def _aux_task_payload(
    task_key: str,
    entry: dict,
    fallback_label: str = "",
    fallback_description: str = "",
) -> dict:
    """Build the API payload row for a single auxiliary task."""
    if not isinstance(entry, dict):
        entry = {}
    return {
        "task": task_key,
        "provider": str(entry.get("provider") or "auto").strip() or "auto",
        "model": str(entry.get("model") or "").strip(),
        "base_url": str(entry.get("base_url") or "").strip(),
        "timeout": entry.get("timeout", ""),
        "download_timeout": entry.get("download_timeout", ""),
        "max_concurrency": entry.get("max_concurrency", ""),
        "extra_body": entry.get("extra_body")
        if isinstance(entry.get("extra_body"), dict)
        else {},
        "api_key_set": bool(str(entry.get("api_key") or "").strip()),
        "label": fallback_label,
        "description": fallback_description,
    }


def _iter_auxiliary_task_rows() -> list[dict]:
    """Return canonical auxiliary task payload rows."""
    api = _config_module
    aux_cfg = api.cfg.get("auxiliary", {})
    if not isinstance(aux_cfg, dict):
        aux_cfg = {}

    rows: list[dict] = []

    # Canonical, first-class tasks from WebUI's catalog.
    for slot in api.AUXILIARY_TASK_CATALOG:
        key = str(slot["key"]).strip()
        if not key:
            continue
        rows.append(
            api._aux_task_payload(
                key,
                aux_cfg.get(key, {}),
                slot["label"],
                slot["description"],
            )
        )

    return rows


def get_auxiliary_models() -> dict:
    """Return current auxiliary task assignments from config.yaml.

    Shape:
    {
        "tasks": [
            {"task": "vision", "provider": "auto", "model": "", "base_url": ""},
            ...
        ],
        "main": {"provider": "openrouter", "model": "anthropic/claude-opus-4.7", "service_tier": ""},
    }
    """
    api = _config_module
    api.reload_config()
    model_cfg = api.cfg.get("model", {})
    if not isinstance(model_cfg, dict):
        model_cfg = {}
    main_provider = str(model_cfg.get("provider") or "").strip()
    main_model = str(model_cfg.get("default") or model_cfg.get("name") or "").strip()

    tasks = api._iter_auxiliary_task_rows()

    return {
        "tasks": tasks,
        "main": {
            "provider": main_provider,
            "model": main_model,
            "supports_fast_tier": api._main_model_supports_service_tier(
                main_model, main_provider
            ),
            "service_tier": api._public_main_service_tier(model_cfg),
            **api._public_advanced_model_options(model_cfg),
        },
    }


def _coerce_optional_positive_int(value, field: str):
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if value == "":
            return ""
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if number < 1:
        raise ValueError(f"{field} must be a positive integer")
    return number


def set_auxiliary_model(
    task: str,
    provider: str,
    model: str,
    advanced: dict | None = None,
) -> dict:
    """Persist an auxiliary model assignment in config.yaml.

    Special case: task='__reset__' clears all auxiliary slots.
    ``advanced`` may update per-slot fields surfaced behind the WebUI gear menu.
    Sensitive api_key values are write-only: get_auxiliary_models() only reports
    whether one is set.
    """
    api = _config_module
    config_path = api._get_config_path()
    with api._cfg_lock:
        config_data = api._load_yaml_config_file(config_path)
        if task != "__reset__" and task not in api.AUX_TASK_SLOTS:
            raise ValueError(
                f"Unknown auxiliary task slot: {task!r}. Valid: {list(api.AUX_TASK_SLOTS)}"
            )
        if task == "__reset__":
            # Per-slot reset: set each slot to auto, preserving extra fields
            # (timeout, extra_body, api_key, base_url, download_timeout, etc.)
            aux_cfg = config_data.get("auxiliary", {})
            if not isinstance(aux_cfg, dict):
                aux_cfg = {}
            for retired_slot in api.RETIRED_AUX_TASK_SLOTS:
                aux_cfg.pop(retired_slot, None)
            for slot in api.AUX_TASK_SLOTS:
                slot_cfg = aux_cfg.get(slot, {})
                if not isinstance(slot_cfg, dict):
                    slot_cfg = {}
                slot_cfg["provider"] = "auto"
                slot_cfg["model"] = ""
                aux_cfg[slot] = slot_cfg
            config_data["auxiliary"] = aux_cfg
        else:
            aux_cfg = config_data.get("auxiliary", {})
            if not isinstance(aux_cfg, dict):
                aux_cfg = {}
            slot_cfg = aux_cfg.get(task, {})
            if not isinstance(slot_cfg, dict):
                slot_cfg = {}
            slot_cfg["provider"] = provider or "auto"
            slot_cfg["model"] = model or ""
            if provider and (provider.startswith("custom:") or provider == "custom"):
                try:
                    _, _, resolved_base_url = api.resolve_model_provider(model)
                    if resolved_base_url:
                        slot_cfg["base_url"] = (
                            str(resolved_base_url).strip().rstrip("/")
                        )
                except Exception:
                    pass
            if advanced is not None:
                try:
                    api._apply_advanced_model_options(slot_cfg, advanced)
                except ValueError as exc:
                    msg = str(exc).replace(
                        "advanced model options", "advanced auxiliary options"
                    )
                    raise ValueError(msg) from exc
            aux_cfg[task] = slot_cfg
            config_data["auxiliary"] = aux_cfg

        api._save_yaml_config_file(config_path, config_data)

    api.reload_config()
    return {"ok": True, "task": task, "provider": provider, "model": model}
