"""Model/provider selection and runtime routing policy.

The public package entrypoint re-exports this module's deep interface.  Each
operation resolves mutable configuration through ``api.config`` at call
time so profile switches and established test overrides stay authoritative.
"""

from __future__ import annotations

import os

from api import config as _config_module


def provider_display_name(provider_id: str) -> str:
    """Return a stable display label for a provider identifier."""
    normalized = str(provider_id or "").strip().lower()
    return _config_module._PROVIDER_DISPLAY.get(
        normalized, normalized.replace("-", " ").title()
    )


def resolve_model_provider(model_id: str, *, explicitly_picked: bool = False) -> tuple:
    """Resolve model name, provider, and base_url for AIAgent.

    Model IDs from the dropdown can be in several formats:
      - 'claude-sonnet-4.6'            (bare name, uses config default provider)
      - 'anthropic/claude-sonnet-4.6'  (OpenRouter-style provider/model)
      - '@minimax:MiniMax-M2.7'        (explicit provider hint from dropdown)

    The @provider:model format is used for models from non-default provider
    groups in the dropdown, so we can route them through the correct provider
    via resolve_runtime_provider(requested=provider) instead of the default.

    Custom OpenAI-compatible endpoints are special: their model IDs often look
    like provider/model (for example ``google/gemma-4-26b-a4b``), which would be
    mistaken for an OpenRouter model if we only looked at the slash. To avoid
    that, first check whether the selected model matches an entry in
    config.yaml -> custom_providers and route it through that named custom
    provider.

    Returns (model, provider, base_url) where provider and base_url may be None.

    ``explicitly_picked``: True when the caller knows the user DELIBERATELY
    selected ``model_id`` this session (persisted from an ``explicit_model_pick``
    UI action), as opposed to it being a stale session leftover. Used ONLY for
    the custom-proxy COLD-catalog decision (#5979): with no provenance available,
    a deliberately-picked ``vendor/model`` is preserved verbatim (the user chose
    it, the proxy routes on it), while an UNMARKED id (a stale cross-provider
    leftover, e.g. #433's ``openai/gpt-5.4`` on a bare-only relay) still gets the
    legacy redundant-prefix strip so it keeps routing when cold. Warm provenance
    (endpoint-advertised ids) always takes precedence over this flag.
    """
    config_provider = None
    config_base_url = None
    model_cfg = _config_module.cfg.get("model", {})
    if isinstance(model_cfg, dict):
        config_base_url = model_cfg.get("base_url")
        config_provider = _config_module._resolve_configured_provider_id(
            model_cfg.get("provider"),
            _config_module.cfg,
            base_url=config_base_url,
            resolve_alias=False,
        )

    # Heal legacy ``provider: local`` entries (written by WebUI < v0.50.252)
    # at read time. ``local`` is not a registered provider, so passing it
    # downstream raises a ``LOCAL_API_KEY`` error from the auxiliary client
    # mid-conversation when compression/vision/web-extract fires. Route
    # through ``custom`` instead — it takes the ``no-key-required``
    # OpenAI-compat path that local servers (Ollama, LM Studio, llama.cpp,
    # vLLM, TabbyAPI) actually use. See #1384.
    if isinstance(config_provider, str) and config_provider.strip().lower() == "local":
        config_provider = "custom"

    model_id = (model_id or "").strip()
    if not model_id:
        return model_id, config_provider, config_base_url

    # Custom providers declared in config.yaml should win over slash-based
    # OpenRouter heuristics. Their model IDs commonly contain '/' too.
    # However, when the active provider is an explicit non-custom provider and
    # the requested model_id is the configured default model, that active
    # provider takes precedence over overlapping custom_providers[] entries.
    # Otherwise WebUI routes to custom:<name> instead of the intended endpoint
    # and can surface a 401 from the wrong provider (#1922).
    # For all other cases, preserve custom_providers[] routing for explicitly
    # selected custom provider models.
    _is_explicit_non_custom_provider = (
        config_provider is not None
        and config_provider != "custom"
        and not config_provider.startswith("custom:")
    )
    _default_model = model_cfg.get("default") if isinstance(model_cfg, dict) else None
    # Owns model if it appears in the static catalog for the configured provider.
    # The provider catalog is keyed by CANONICAL slug (e.g. 'zai', not the 'z-ai'
    # alias a user may write in config), so canonicalise config_provider before
    # the lookup — otherwise an aliased active provider gets an empty ownership
    # set and _skip_custom_providers guard-2 silently fails, letting another
    # providers.<slug>.models entry hijack an active-owned model (#5511).
    _canon_config_provider = (
        _config_module._canonicalise_provider_id(config_provider)
        if config_provider
        else ""
    )
    _provider_models_set: set[str] = set()
    if (
        _canon_config_provider
        and _canon_config_provider in _config_module._PROVIDER_MODELS
        and isinstance(_config_module._PROVIDER_MODELS[_canon_config_provider], list)
    ):
        _provider_models_set = {
            m.get("id", "")
            for m in _config_module._PROVIDER_MODELS[_canon_config_provider]
            if isinstance(m, dict) and isinstance(m.get("id"), str)
        }
    # The active provider may be defined ENTIRELY via config.yaml `providers:`
    # (no static provider-catalog entry) with its own `models:` allowlist. Fold
    # that allowlist into the ownership set too, so the active provider owns its
    # own declared models and can't be hijacked by another providers.<slug>
    # entry that happens to list the same bare id earlier in config order (#5511).
    if _canon_config_provider:
        _providers_cfg_own = _config_module.cfg.get("providers", {})
        if isinstance(_providers_cfg_own, dict):
            for _slug, _pdef in _providers_cfg_own.items():
                if not isinstance(_pdef, dict):
                    continue
                if (
                    _config_module._canonicalise_provider_id(_slug)
                    != _canon_config_provider
                ):
                    continue
                if _canon_config_provider == "copilot":
                    continue  # copilot.models is a settings map, not an allowlist
                _provider_models_set.update(
                    _config_module._configured_model_ids(_pdef.get("models"))
                )
    _skip_custom_providers = _is_explicit_non_custom_provider and (
        # Guard 1: model is the configured default (existing behaviour).
        (_default_model is not None and model_id == _default_model)
        # Guard 2: model is owned by the configured non-custom provider.
        or model_id in _provider_models_set
    )
    custom_providers = _config_module.cfg.get("custom_providers", [])
    if isinstance(custom_providers, list) and not _skip_custom_providers:
        for entry in custom_providers:
            if not isinstance(entry, dict):
                continue
            entry_model = (entry.get("model") or "").strip()
            entry_name = (entry.get("name") or "").strip()
            entry_base_url = (entry.get("base_url") or "").strip()
            entry_model_ids = set()
            if entry_model:
                entry_model_ids.add(entry_model)
            entry_model_ids.update(
                _config_module._configured_model_ids(entry.get("models"))
            )
            if entry_name and model_id in entry_model_ids:
                provider_hint = _config_module._custom_provider_slug_from_name(
                    entry_name
                )
                return model_id, provider_hint, entry_base_url or None

    # Check user-defined providers (config.yaml → providers:).
    # Mirrors the custom_providers scan above — exact match against each
    # entry's declared models list (case-sensitive to match custom_providers).
    providers_cfg = _config_module.cfg.get("providers", {})
    if isinstance(providers_cfg, dict):
        target = model_id.strip()
        # Honor the same active/default ownership guard as the custom_providers
        # scan (_skip_custom_providers, config.py:2535): when the active provider
        # explicitly owns this model (it's the configured default or in the
        # active provider's model set), another provider's overlapping
        # `providers.<slug>.models` entry must NOT hijack routing away from the
        # active provider (#5511 gate finding — e.g. active ai-gateway + default
        # gpt-5 was being pulled to providers.openai.models.gpt-5). In that case
        # restrict the scan to the active provider's own canonical slug.
        _active_slug = _canon_config_provider
        for slug, pdef in providers_cfg.items():
            if not isinstance(pdef, dict):
                continue
            # Copilot is the documented exception: `providers.copilot.models` is
            # a per-model SETTINGS map (reasoning_effort, limits, etc.), NOT a
            # routable allowlist (see the exception at the catalog-build site).
            # Scanning it here would let a Copilot per-model settings entry
            # hijack that model's routing away from its real provider (#5511).
            if _config_module._canonicalise_provider_id(slug) == "copilot":
                continue
            # Ownership guard: when the active provider owns this model, only its
            # own providers: entry may match; skip all other slugs.
            if (
                _skip_custom_providers
                and _config_module._canonicalise_provider_id(slug) != _active_slug
            ):
                continue
            if target in _config_module._configured_model_ids(pdef.get("models")):
                p_base_url = str(pdef.get("base_url") or "").strip()
                return model_id, slug, p_base_url or None

    # @provider:model format — explicit provider hint from the dropdown.
    # Route through that provider directly (resolve_runtime_provider will
    # resolve credentials in streaming.py).
    # Use rsplit to handle provider_ids that contain ':' (e.g. custom:my-key).
    # With rsplit, "@custom:my-key:model" → provider="custom:my-key", model="model".
    # BUT: model IDs that end in :free / :beta / :thinking collide with the
    # rsplit grammar (e.g. "@openrouter:tencent/hy3-preview:free" would split
    # into provider="openrouter:tencent/hy3-preview", model="free").  Guard
    # against that by falling back to split(":") when the rsplit result is not
    # a recognised provider (#1744).
    #
    # Edge case (#1776): for custom providers with the same suffix
    # ("@custom:my-key:some-model:free"), rsplit yields
    # provider_hint="custom:my-key:some-model", bare_model="free", and the
    # custom-prefix guard below skips the split-fallback. Detect the
    # over-split structurally — custom hints normally carry one slug segment
    # after ``custom:``. If ``provider_hint`` has extra ``:`` tokens because the
    # model ID contained tags like ``:free``, peel one segment back (#1776).
    #
    # Exception: ``custom:<ip-or-host>:<port>`` is a single logical slug derived
    # from OpenAI ``base_url`` authority and contains no eaten model segments.
    parsed_provider_hint = _config_module._parse_provider_qualified_model_id(model_id)
    if parsed_provider_hint is not None:
        bare_model, provider_hint = parsed_provider_hint
        if (
            provider_hint.startswith("custom:")
            and config_base_url
            and _config_module._is_local_server_provider(config_provider)
            and provider_hint.lower()
            in _config_module._custom_endpoint_slugs_for_base_url(config_base_url)
        ):
            return bare_model, config_provider, config_base_url
        return (
            bare_model,
            provider_hint,
            _config_module._get_provider_base_url(provider_hint),
        )

    if "/" in model_id:
        prefix, bare = model_id.split("/", 1)
        # OpenRouter always needs the full provider/model path (e.g. openrouter/free,
        # anthropic/claude-sonnet-4.6). Never strip the prefix for OpenRouter.
        if config_provider == "openrouter":
            return model_id, "openrouter", config_base_url
        # Portal providers (Nous, OpenCode, NVIDIA NIM) serve models from multiple
        # upstream namespaces — check them BEFORE the prefix-strip branch so that
        # a model id whose prefix happens to equal the config_provider (e.g.
        # nvidia/nemotron-... on NVIDIA NIM) still keeps the full namespaced path.
        # The earlier ordering ran this guard AFTER the prefix-strip, so it never
        # fired in the prefix==config_provider case, causing HTTP 404 from the
        # portal which requires the full provider/model id (#2177; sibling of
        # #854 / #894 for Nous, where this guard was originally added).
        _PORTAL_PROVIDERS = {"nous", "opencode-zen", "opencode-go", "nvidia"}
        if config_provider in _PORTAL_PROVIDERS:
            return model_id, config_provider, config_base_url
        # If prefix matches config provider exactly, strip it and use that provider directly.
        # e.g. config=anthropic, model=anthropic/claude-... → bare name to anthropic API
        if config_provider and prefix == config_provider:
            return bare, config_provider, config_base_url
        # The OpenAI Codex provider uses a real base_url, but its default
        # ChatGPT endpoint cannot serve OpenRouter-style provider/model IDs.
        # Keep that narrow exception before the custom endpoint protection so
        # selecting openai/gpt-5.5 from OpenRouter under active Codex still
        # routes through OpenRouter. Other base_url-backed real providers may be
        # custom/proxy endpoints, so they must fall through to the branch below.
        if (
            config_provider == "openai-codex"
            and str(config_base_url or "").strip().rstrip("/")
            == "https://chatgpt.com/backend-api/codex"
            and prefix in _config_module._PROVIDER_MODELS
            and prefix != config_provider
        ):
            return model_id, "openrouter", None
        # Cross-provider via custom_providers: if the prefix matches a named custom
        # provider entry (e.g. "ollama-local/glm-4.7-flash:q4_k_m"), route through it
        # instead of falling back to the default config provider. MUST come BEFORE
        # the config_base_url branch because many providers have a base_url set.
        if prefix and config_provider and prefix != config_provider:
            _custom_cfg = _config_module.cfg.get("custom_providers", [])
            if isinstance(_custom_cfg, list):
                for _entry in _custom_cfg:
                    if (
                        isinstance(_entry, dict)
                        and _entry.get("name", "").strip() == prefix
                    ):
                        _slug = _config_module._custom_provider_slug_from_name(prefix)
                        _base = (_entry.get("base_url") or "").strip()
                        return model_id, _slug, _base or None

        # If a custom endpoint base_url is configured, don't reroute through OpenRouter
        # just because the model name contains a slash (e.g. google/gemma-4-26b-a4b).
        # The user has explicitly pointed at a base_url, so trust their routing config.
        if config_base_url:
            # Local model servers (LM Studio, Ollama, llama.cpp, vLLM, TabbyAPI)
            # register models under their full HuggingFace-style id. Stripping the
            # prefix breaks the lookup and causes a fresh instance to load with
            # default settings, ignoring user-tuned context length / parallel slots.
            # See #1625. Detect either by canonical provider name OR by base_url
            # pointing at a loopback/private host.
            if _config_module._is_local_server_provider(
                config_provider
            ) or _config_module._base_url_points_at_local_server(config_base_url):
                return model_id, config_provider, config_base_url
            # Strip the provider prefix only when it's a known provider namespace
            # AND stripping is the right call for this configured provider:
            #
            #  * A real first-party provider pointed at an OpenAI-compatible proxy
            #    (e.g. provider=openai + base_url=litellm) expects the bare id —
            #    "openai/gpt-5.4" → "gpt-5.4", "google/gemma-…" → "gemma-…". This
            #    is the #433 behaviour and applies whenever config_provider is not
            #    the bare "custom" pseudo-provider.
            #
            #  * A *bare* ``custom`` provider (or a named ``custom:<slug>``) is a
            #    vendor-routing proxy (LiteLLM, Bedrock gateway, OpenRouter-style
            #    multi-vendor endpoint). There we strip ONLY a prefix that is
            #    redundant with the model's own first-party namespace
            #    ("openai/gpt-5.4" → gpt-5.4, since gpt-5.4 is genuinely an OpenAI
            #    model — #433). An intrinsic routing prefix whose bare id is NOT a
            #    first-party model of that namespace is kept whole, because the
            #    proxy routes on the full string and truncating it 403s "model not
            #    allowed": "bedrock/opus-4-6" stays intact (opus-4-6 ∉ bedrock
            #    catalog — #3872).
            #
            # Unknown prefixes (e.g. "zai-org/GLM-5.1" on DeepInfra) are intrinsic
            # to the model ID and always preserved (#548). The redundant-prefix
            # strip that matches the *configured* provider's own family is handled
            # earlier by the ``prefix == config_provider`` branch.
            _cp_lower = (config_provider or "").strip().lower()
            _is_custom = _cp_lower == "custom" or _cp_lower.startswith("custom:")
            if _is_custom:
                # Vendor-routing proxy: the reliable signal for whether the
                # endpoint wants the full ``vendor/model`` id or the bare id is
                # what its own catalog actually advertised (the ids the user
                # picked from the dropdown, populated by the endpoint's live
                # ``/v1/models`` probe or a ``custom_providers[].models``
                # allowlist). The catalog-family heuristic
                # is the wrong question: it answered "is this bare id a first-
                # party model of the prefix's home vendor?" which is True for BOTH
                # ``x-ai/grok-4.5`` (proxy advertised it whole — must preserve,
                # #5979) and ``openai/gpt-5.4`` (a stale leftover on a relay that
                # only serves bare ``gpt-5.4`` — must strip, #433). Those two are
                # structurally identical to the family heuristic, so a model
                # graduating into a first-party catalog (agent commit 62ada5175
                # adding grok-4.5) silently flipped a working custom-proxy id from
                # preserved to stripped. Tri-state provenance tells them apart:
                #
                # (1) Config declares the full id verbatim (model.default /
                #     model.models / custom_providers[].models). Authoritative and
                #     network-free, so #5979 survives a cold restart — preserve.
                if _config_module._model_id_declared_in_config(
                    model_id, config_provider
                ):
                    return model_id, config_provider, config_base_url
                # (2) The endpoint's live/cached catalog advertised it.
                # Bound from the catalog owner below after facade state exists.
                _advertised = _config_module._endpoint_advertised_model_ids(
                    config_provider
                )  # noqa: F821
                if _advertised:
                    # Full id advertised → route on it verbatim (#5979/#3872/#548).
                    if model_id in _advertised:
                        return model_id, config_provider, config_base_url
                    # ONLY the bare id advertised → the prefix is a redundant
                    # leftover the relay rejects; strip it (#433). Keep the
                    # known-prefix guard so an adversarial catalog
                    # advertising a bare id can't strip an unknown-vendor prefix.
                    if (
                        bare in _advertised
                        and prefix in _config_module._PROVIDER_MODELS
                    ):
                        return bare, config_provider, config_base_url
                    # Advertised but neither exact shape matched → intrinsic /
                    # unknown prefix the proxy routes on; preserve it whole.
                    return model_id, config_provider, config_base_url
                # (3) Provenance genuinely unavailable (cold/unbuilt or
                #     fingerprint-mismatched catalog AND not config-declared).
                #     Distinguish a DELIBERATE selection from a stale leftover:
                #
                #     * explicitly_picked → PRESERVE verbatim. The user chose this
                #       exact ``vendor/model`` in the UI this session; the proxy
                #       routes on it. A wrong strip destroys a namespace the proxy
                #       needs (recurs every turn, unrepairable short of declaring
                #       every model in config) — this is b3nw's #5979 case: a
                #       non-default pick on a custom:<slug> proxy, cold catalog.
                #     * NOT explicitly_picked → legacy redundant-prefix strip. An
                #       unmarked id here is a stale cross-provider leftover (the
                #       user switched providers and the old session model lingers,
                #       e.g. #433's ``openai/gpt-5.4`` on a relay that only serves
                #       bare ``gpt-5.4``); stripping keeps it routing while cold.
                #
                #     Warm provenance (case 2, endpoint-advertised ids) always
                #     wins over this flag; the send path also warms provenance
                #     network-free from the disk cache first
                #     (warm_models_catalog_provenance_if_cold), so this branch is
                #     reached only in the narrow no-disk-cache window. The flag
                #     removes the data-driven flaw where a model graduating into
                #     the static first-party catalog silently flipped routing
                #     (exactly how #5979 regressed).
                if explicitly_picked:
                    return model_id, config_provider, config_base_url
                if (
                    prefix in _config_module._PROVIDER_MODELS
                    and _config_module._is_first_party_model(prefix, bare)
                ):
                    return bare, config_provider, config_base_url
                return model_id, config_provider, config_base_url
            # Non-custom first-party provider pointed at an OpenAI-compatible
            # proxy (e.g. provider=openai + base_url=litellm): the bare id is
            # what it expects — "openai/gpt-5.4" → "gpt-5.4" (#433).
            if prefix in _config_module._PROVIDER_MODELS:
                return bare, config_provider, config_base_url
            # Intrinsic / unknown prefix — pass the full model_id through unchanged.
            return model_id, config_provider, config_base_url

        # If prefix does NOT match config provider, the user picked a cross-provider model
        # from the OpenRouter dropdown (e.g. config=anthropic but picked openai/gpt-5.4-mini).
        # In this case always route through openrouter with the full provider/model string.
        # Exception (#4210): a custom provider (bare ``custom`` or named ``custom:<slug>``)
        # is a vendor-routing proxy, not a first-party provider — its model ids commonly
        # contain a known-provider prefix that the proxy uses for upstream routing, not
        # an OpenRouter dropdown selection. Keep the request on the custom provider; the
        # base_url-set sibling of this exception lives earlier in the ``config_base_url``
        # branch (#3872).
        _cp_lower_cross = (config_provider or "").strip().lower()
        _is_custom_cross = _cp_lower_cross == "custom" or _cp_lower_cross.startswith(
            "custom:"
        )
        if (
            prefix in _config_module._PROVIDER_MODELS
            and prefix != config_provider
            and not _is_custom_cross
        ):
            return model_id, "openrouter", None

    return model_id, config_provider, config_base_url


def resolve_custom_provider_connection(
    provider_id: str,
) -> tuple[str | None, str | None]:
    """Return (api_key, base_url) for a named ``custom:*`` provider.

    Supports ``custom_providers[].api_key`` as either a literal key or
    ``${ENV_VAR}``, and ``custom_providers[].key_env`` as an env-var hint.
    Returns ``(None, None)`` when no named custom provider matches.
    """
    pid = str(provider_id or "").strip().lower()
    if not pid.startswith("custom:"):
        return None, None

    def _slugify(value: str) -> str:
        s = str(value or "").strip().lower().replace("_", "-").replace(" ", "-")
        while "--" in s:
            s = s.replace("--", "-")
        return s.strip("-")

    slug = _slugify(pid.split(":", 1)[1].strip())
    if not slug:
        return None, None

    # Read the live config snapshot to avoid stale module-level cache edge
    # cases after profile switches or runtime config edits.
    cfg_data = _config_module.get_config()

    def _resolve_key(raw_api_key, raw_key_env, provider_hint=None) -> str | None:
        api_key = None
        if raw_api_key is not None:
            key_text = str(raw_api_key).strip()
            if (
                key_text.startswith("${")
                and key_text.endswith("}")
                and len(key_text) > 3
            ):
                api_key = (
                    _config_module._thread_local_env_value(key_text[2:-1]).strip()
                    or None
                )
            elif key_text:
                api_key = key_text
        if not api_key:
            key_env = str(raw_key_env or "").strip()
            if key_env:
                api_key = (
                    _config_module._thread_local_env_value(key_env).strip() or None
                )
        if not api_key and provider_hint:
            api_key = _config_module._lookup_custom_api_key_env(provider_hint)
        return api_key

    custom_providers = cfg_data.get("custom_providers", [])
    if not isinstance(custom_providers, list):
        custom_providers = []

    for entry in custom_providers:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        entry_slug = _slugify(name)
        if entry_slug != slug:
            continue

        base_url = str(entry.get("base_url") or "").strip() or None
        api_key = _resolve_key(entry.get("api_key"), entry.get("key_env"), pid)
        return api_key, base_url

    # If exactly one custom provider is configured, use it as a pragmatic
    # fallback for mismatched slugs (e.g. punctuation differences).
    if len(custom_providers) == 1 and isinstance(custom_providers[0], dict):
        entry = custom_providers[0]
        return (
            _resolve_key(entry.get("api_key"), entry.get("key_env"), pid),
            str(entry.get("base_url") or "").strip() or None,
        )

    # Fallbacks for setups that don't use custom_providers names directly.
    providers_cfg = cfg_data.get("providers", {})
    provider_specific = (
        providers_cfg.get(pid, {}) if isinstance(providers_cfg, dict) else {}
    )
    provider_custom = (
        providers_cfg.get("custom", {}) if isinstance(providers_cfg, dict) else {}
    )

    model_cfg = cfg_data.get("model", {})
    model_provider = (
        str(model_cfg.get("provider") or "").strip().lower()
        if isinstance(model_cfg, dict)
        else ""
    )

    fallback_base = None
    for candidate in (provider_specific, provider_custom, model_cfg):
        if isinstance(candidate, dict):
            _base = str(candidate.get("base_url") or "").strip()
            if _base:
                fallback_base = _base
                break

    fallback_key = None
    if isinstance(provider_specific, dict):
        fallback_key = _resolve_key(
            provider_specific.get("api_key"), provider_specific.get("key_env"), pid
        )
    if not fallback_key and isinstance(provider_custom, dict):
        fallback_key = _resolve_key(
            provider_custom.get("api_key"), provider_custom.get("key_env"), pid
        )
    if (
        not fallback_key
        and isinstance(model_cfg, dict)
        and model_provider in {"custom", pid, slug}
    ):
        fallback_key = _resolve_key(
            model_cfg.get("api_key"), model_cfg.get("key_env"), pid
        )

    if fallback_key or fallback_base:
        return fallback_key, fallback_base or None

    return None, None


# Subprocess ACP transports (Cursor/Copilot CLI). Model IDs often contain '/'
# but must still route via explicit @provider:model so they do not fall through
# to the configured default HTTP provider (e.g. openai-codex).
_ACP_SUBPROCESS_PROVIDERS = frozenset({"cursor-acp", "copilot-acp"})


def model_with_provider_context(
    model_id: str, model_provider: str | None = None
) -> str:
    """Return the model string to pass to ``resolve_model_provider()``.

    Session persistence keeps the user's selected provider in ``model_provider``
    instead of forcing every selected model into ``@provider:model`` form. At
    runtime, however, ``resolve_model_provider()`` still understands that
    internal disambiguation form, so use it only when the provider context is
    needed to route away from the current default provider.
    """
    model = str(model_id or "").strip()
    provider = str(model_provider or "").strip().lower()
    if not model or not provider or provider == "default" or model.startswith("@"):
        return model

    model_cfg = _config_module.cfg.get("model", {})
    config_provider = None
    if isinstance(model_cfg, dict):
        config_provider = str(model_cfg.get("provider") or "").strip().lower()

    # ACP subprocess providers always need the explicit hint — their slash IDs
    # are not OpenRouter paths and must not inherit config_provider routing.
    if provider in _ACP_SUBPROCESS_PROVIDERS:
        return f"@{provider}:{model}"

    # Plugin-only model providers (e.g. 9router, and other model plugins whose
    # slugs are not in the static provider tables) route through the plugin, not
    # the default provider. This MUST come before the `provider == config_provider`
    # bare-passthrough below: when a plugin provider is ALSO the configured
    # provider, returning a bare model would drop the '@plugin:' hint and the model
    # would be sent to the wrong backend. Emit the explicit hint so it stays
    # routable to the plugin that surfaced it. (#5909 gate finding)
    if _config_module._is_plugin_model_provider(provider):
        return f"@{provider}:{model}"

    # If the selected provider is already the configured provider, leaving the
    # model bare preserves provider-specific base_url/proxy settings.
    if provider == config_provider:
        return model

    # OpenRouter selections with slash IDs are explicit provider/model paths.
    if provider == "openrouter":
        return f"@{provider}:{model}"

    # Explicit providers configured in config.yaml (for example local llama.cpp,
    # Ollama, LM Studio, vLLM, or other OpenAI-compatible endpoints) must keep
    # their provider hint even when the model ID is HuggingFace-style and
    # contains '/'. Otherwise a selected local model such as
    # 'unsloth/gemma-4-12b-it-GGUF:UD-Q4_K_XL' inherits the default provider
    # (e.g. openai-codex) and is sent to the wrong backend.
    providers_cfg = (
        _config_module.cfg.get("providers")
        if isinstance(_config_module.cfg, dict)
        else {}
    )
    if isinstance(providers_cfg, dict) and provider in providers_cfg:
        return f"@{provider}:{model}"

    # (Plugin-only provider routing handled above, before the config_provider
    # bare-passthrough.)

    # For non-OpenRouter slash IDs without an explicit configured provider,
    # keep the ID intact so existing custom/proxy base_url routing and
    # portal-provider handling remain in charge.
    if "/" in model:
        return model

    return f"@{provider}:{model}"


def canonical_model_provider_lane(
    model_id: str, model_provider: str | None = None
) -> tuple[str, str | None]:
    """Return the runtime-resolved model/provider pair used for lane comparisons."""
    model = str(model_id or "").strip()
    provider = str(model_provider or "").strip() or None
    if not model:
        return "", provider
    resolved_model, resolved_provider, _ = _config_module.resolve_model_provider(
        _config_module.model_with_provider_context(model, provider)
    )
    resolved_provider = str(resolved_provider or "").strip() or None
    return str(resolved_model or "").strip(), resolved_provider


def get_effective_default_model(config_data: dict | None = None) -> str:
    """Resolve the effective Hermes default model from config, then env overrides."""
    active_cfg = config_data if config_data is not None else _config_module.cfg
    default_model = _config_module.DEFAULT_MODEL

    model_cfg = active_cfg.get("model", {})
    if isinstance(model_cfg, str):
        default_model = model_cfg.strip()
    elif isinstance(model_cfg, dict):
        cfg_default = str(model_cfg.get("default") or "").strip()
        if cfg_default:
            default_model = cfg_default

    env_model = (
        os.getenv("HERMES_MODEL") or os.getenv("OPENAI_MODEL") or os.getenv("LLM_MODEL")
    )
    if env_model:
        default_model = env_model.strip()
    return default_model
