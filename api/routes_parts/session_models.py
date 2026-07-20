"""Session model, provider, and context-window compatibility resolution."""

# Functions in this owner are rebound to the ``api.routes`` facade so legacy
# monkeypatch seams keep resolving through the facade globals.
# ruff: noqa: F821

from __future__ import annotations

import threading


def _starts_token(raw: str, prefix: str) -> bool:
    if not raw.startswith(prefix):
        return False
    rest = raw[len(prefix):]
    return rest == "" or rest[0] in ":/"


def _normalize_provider_id(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if raw in _PROVIDER_ALIASES:
        return _PROVIDER_ALIASES[raw]
    for prefix, normalized in (
        ("openai-codex", "openai"),
        ("openai", "openai"),
        ("anthropic", "anthropic"),
        ("claude", "anthropic"),
        ("google", "google"),
        ("gemini", "google"),
        ("openrouter", "openrouter"),
        ("custom", "custom"),
    ):
        if _starts_token(raw, prefix):
            return normalized
    # Unknown prefix — return empty so callers treat it as "no match" and pass
    # the model through unchanged rather than incorrectly stripping it.
    return ""


def _catalog_provider_id_sets(catalog: dict) -> tuple[set[str], set[str]]:
    raw_provider_ids: set[str] = set()
    normalized_provider_ids: set[str] = set()
    for group in catalog.get("groups") or []:
        raw = str(group.get("provider_id") or "").strip().lower()
        if not raw:
            continue
        raw_provider_ids.add(raw)
        normalized = _normalize_provider_id(raw)
        if normalized:
            normalized_provider_ids.add(normalized)
    return raw_provider_ids, normalized_provider_ids


def _catalog_has_provider(
    provider_raw: str,
    provider_normalized: str,
    raw_provider_ids: set[str],
    normalized_provider_ids: set[str],
) -> bool:
    return (
        provider_raw in raw_provider_ids
        or (provider_normalized and provider_normalized in raw_provider_ids)
        or (provider_normalized and provider_normalized in normalized_provider_ids)

    )


def _model_matches_active_provider_family(
    model: str,
    active_provider: str,
) -> bool:
    model_lower = model.lower()
    for bare_prefix in ("gpt", "claude", "gemini"):
        if model_lower.startswith(bare_prefix):
            return _normalize_provider_id(bare_prefix) == active_provider
    return False


def _catalog_model_id_matches(candidate: str, model: str) -> bool:
    candidate = str(candidate or "").strip()
    if candidate.startswith("@") and ":" in candidate:
        candidate = candidate.rsplit(":", 1)[1]
    if "/" in candidate:
        candidate = candidate.split("/", 1)[1]
    return candidate.replace("-", ".").lower() == model.replace("-", ".").lower()


def _catalog_group_owns_exact_model(group: dict, model: str) -> bool:
    provider_id = str(group.get("provider_id") or "").strip()
    wrapper = f"@{provider_id}:"
    for bucket in ("models", "extra_models"):
        for entry in group.get(bucket) or []:
            if not isinstance(entry, dict):
                continue
            candidate = str(entry.get("id") or "").strip()
            if candidate.lower().startswith(wrapper.lower()):
                candidate = candidate[len(wrapper):]
            if candidate == model or _catalog_model_id_matches(candidate, model):
                return True
    return False


def _repair_foreign_session_model_provider(
    session,
    *,
    requested_model: str,
    requested_provider: str | None,
    resolved_model: str,
    resolved_provider: str | None,
    explicit_model_pick: bool,
    profile_provider: str | None,
) -> str | None:
    """Repair a stale provider only when the cached catalog names one owner."""
    stored_model = str(getattr(session, "model", "") or "").strip()
    stored_provider = _clean_session_model_provider(getattr(session, "model_provider", None))
    requested_provider = _clean_session_model_provider(requested_provider)
    resolved_provider = _clean_session_model_provider(resolved_provider)
    profile_provider = _clean_session_model_provider(profile_provider)
    _, qualified_provider = _split_provider_qualified_model(requested_model)
    if (
        explicit_model_pick
        or qualified_provider
        or not stored_model
        or not stored_provider
        or (
            str(requested_model or "").strip() != stored_model
            and not _catalog_model_id_matches(str(requested_model or "").strip(), stored_model)
        )
        or requested_provider != stored_provider
        or (
            resolved_model != stored_model
            and not _catalog_model_id_matches(resolved_model, stored_model)
        )
        or resolved_provider != stored_provider
        or not profile_provider
        or profile_provider == stored_provider
    ):
        return resolved_provider

    try:
        catalog = get_available_models(prefer_cache=True)
    except Exception:
        return resolved_provider
    groups = [group for group in catalog.get("groups") or [] if isinstance(group, dict)]
    stored_groups = [
        group
        for group in groups
        if str(group.get("provider_id") or "").strip().lower() == stored_provider
    ]
    if (
        not stored_groups
        or any(group.get("models_endpoint_error") for group in stored_groups)
        or any(_catalog_group_owns_exact_model(group, stored_model) for group in stored_groups)
    ):
        return resolved_provider
    owners = [
        group
        for group in groups
        if str(group.get("provider_id") or "").strip().lower() != stored_provider
        and _catalog_group_owns_exact_model(group, stored_model)
    ]
    if len(owners) != 1:
        return resolved_provider
    return str(owners[0].get("provider_id") or "").strip() or resolved_provider


def _clean_session_model_provider(value: str | None) -> str | None:
    provider = str(value or "").strip().lower()
    if not provider or provider == "default":
        return None
    if provider.startswith("@"):
        provider = provider[1:]
    return provider or None


def _split_provider_qualified_model(model: str) -> tuple[str, str | None]:
    model = str(model or "").strip()
    if model.startswith("@") and ":" in model:
        provider_hint, bare_model = model[1:].rsplit(":", 1)
        provider = _clean_session_model_provider(provider_hint)
        bare = bare_model.strip()
        if provider and bare:
            return bare, provider
    return model, None


def _model_matches_configured_default(
    session_model: str | None,
    cfg_default: str | None,
    provider: str | None = None,
) -> bool:
    """Return True when ``session_model`` refers to the configured ``model.default``.

    The global ``model.context_length`` cap applies ONLY to the default model
    (#3256/#3263). An exact string compare is not enough because ``model.default``
    and the session model can be stored in different but equivalent shapes:
      - bare:            ``claude-opus-4.8``
      - slash-prefixed:  ``anthropic/claude-opus-4.8``  (OpenRouter-style)
      - @provider:model: ``@anthropic:claude-opus-4.8``

    Matching rule (correct in both directions):
      1. Identical strings → match.
      2. Otherwise compare BARE model ids — BUT only after a provider-compatibility
         check: if BOTH sides carry an identifiable provider (from a ``provider/``
         prefix, an ``@provider:`` qualifier, or the explicit ``provider`` arg for
         the session side) and those providers DIFFER, it is NOT a match. This
         stops a non-default model on a different provider that happens to share a
         bare name (``openai/gpt-4o`` vs default ``openrouter/gpt-4o``) from being
         treated as the default and wrongly receiving its cap.
      3. When a provider can't be identified on one side, fall through to the bare
         comparison (lenient-when-unknown — a bare default config still matches a
         bare/prefixed session model).
    Empty default → no match.
    """
    sess = str(session_model or "").strip()
    default = str(cfg_default or "").strip()
    if not sess or not default:
        return False
    if sess == default:
        return True

    def _split(value: str) -> tuple[str, str | None]:
        """Return (bare_model, provider_or_None) for any of the 3 shapes."""
        value = str(value or "").strip()
        # @provider:model
        unq, q_prov = _split_provider_qualified_model(value)
        if q_prov:
            return unq.strip(), str(q_prov).strip().lower()
        # provider/model (single leading slash segment)
        if "/" in value:
            prefix, rest = value.split("/", 1)
            return rest.strip(), prefix.strip().lower()
        return value, None

    sess_bare, sess_prov = _split(sess)
    default_bare, default_prov = _split(default)
    # The explicit provider arg is the session side's provider when the model
    # string itself didn't carry one.
    if not sess_prov and provider:
        sess_prov = str(provider).strip().lower() or None

    if not sess_bare or not default_bare or sess_bare != default_bare:
        return False
    # Bare ids match. Reject only when both sides name DIFFERENT providers.
    if sess_prov and default_prov and sess_prov != default_prov:
        return False
    return True


class _ContextLengthLookupInputs:
    __slots__ = ("config_context_length", "custom_providers", "base_url", "provider", "api_key")

    def __init__(
        self,
        *,
        config_context_length: int | None = None,
        custom_providers: list | None = None,
        base_url: str = "",
        provider: str = "",
        api_key: str = "",
    ) -> None:
        self.config_context_length = config_context_length
        self.custom_providers = custom_providers
        self.base_url = base_url
        self.provider = provider
        self.api_key = api_key


def _positive_context_length(value) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _model_lookup_candidates(model: str) -> tuple[str, ...]:
    raw = str(model or "").strip()
    candidates = []
    for candidate in (raw, _split_provider_qualified_model(raw)[0]):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
        if "/" in candidate:
            bare = candidate.split("/", 1)[1].strip()
            if bare and bare not in candidates:
                candidates.append(bare)
    return tuple(candidates)


def _models_config_context_length(models_cfg, model: str) -> int | None:
    candidates = _model_lookup_candidates(model)
    if isinstance(models_cfg, dict):
        for candidate in candidates:
            entry = models_cfg.get(candidate)
            raw_ctx = entry.get("context_length") if isinstance(entry, dict) else entry
            ctx = _positive_context_length(raw_ctx)
            if ctx is not None:
                return ctx
    if isinstance(models_cfg, list):
        for entry in models_cfg:
            if not isinstance(entry, dict):
                continue
            entry_model = str(entry.get("id") or entry.get("model") or entry.get("name") or "").strip()
            if entry_model in candidates:
                ctx = _positive_context_length(entry.get("context_length"))
                if ctx is not None:
                    return ctx
    return None


def _canonical_context_provider(value: str | None) -> str:
    provider = _clean_session_model_provider(value) or ""
    if not provider:
        return ""
    try:
        from api.config import _resolve_provider_alias

        provider = _resolve_provider_alias(provider)
    except Exception:
        pass
    return str(provider or "").strip().lower()


def _custom_provider_slug_for_context(name: object) -> str:
    try:
        from api.config import _custom_provider_slug_from_name

        return _custom_provider_slug_from_name(name)
    except Exception:
        raw = str(name or "").strip().lower()
        if not raw:
            return ""
        if raw.startswith("custom:"):
            return raw
        slug = re.sub(r"[^a-z0-9._-]+", "-", raw).strip("-")
        slug = re.sub(r"-{2,}", "-", slug)
        return f"custom:{slug}" if slug else ""


def _providers_match_for_context(config_key: object, requested_provider: str) -> bool:
    if not requested_provider:
        return False
    raw_key = str(config_key or "").strip().lower()
    key = _canonical_context_provider(raw_key)
    requested = _canonical_context_provider(requested_provider)
    return bool(
        requested
        and (
            raw_key == requested
            or key == requested
            or raw_key == str(requested_provider or "").strip().lower()
        )
    )


def _custom_provider_api_key_for_context(entry: dict, provider: str) -> str:
    """Resolve the API key for a matched ``custom_providers`` entry.

    Static session hydration/update routes already have a per-profile config
    snapshot. Resolve from the matched entry instead of re-reading global config,
    while preserving the same literal, ``${ENV_VAR}``, ``key_env``, and
    sanitized-env shapes used by streaming/provider resolution.
    """
    raw_api_key = entry.get("api_key")
    if raw_api_key is not None:
        api_key_text = str(raw_api_key).strip()
        if api_key_text.startswith("${") and api_key_text.endswith("}") and len(api_key_text) > 3:
            env_name = api_key_text[2:-1]
            resolved = os.getenv(env_name, "").strip()
            if resolved:
                return resolved
            logger.debug(
                "Custom provider %s api_key references %s, but the environment variable is unset or empty",
                provider,
                api_key_text,
            )
        elif api_key_text:
            return api_key_text

    key_env = str(entry.get("key_env") or "").strip()
    if key_env:
        resolved = os.getenv(key_env, "").strip()
        if resolved:
            return resolved

    try:
        from api.config import _lookup_custom_api_key_env

        return _lookup_custom_api_key_env(provider) or ""
    except Exception:
        return ""


def _context_length_config_api_key_for_provider(
    provider: str | None,
    cfg: dict | None,
) -> str:
    """Return a config/env API key usable for context-window metadata lookup."""
    cfg = cfg if isinstance(cfg, dict) else {}
    provider = _canonical_context_provider(provider)

    def _resolve_key(raw_api_key, raw_key_env=None) -> str:
        api_key_text = str(raw_api_key or "").strip()
        if (
            api_key_text.startswith("${")
            and api_key_text.endswith("}")
            and len(api_key_text) > 3
        ):
            resolved = os.getenv(api_key_text[2:-1], "").strip()
            if resolved:
                return resolved
        elif api_key_text:
            return api_key_text
        key_env = str(raw_key_env or "").strip()
        if key_env:
            resolved = os.getenv(key_env, "").strip()
            if resolved:
                return resolved
        return ""

    providers_cfg = cfg.get("providers") or {}
    if isinstance(providers_cfg, dict):
        for provider_key, provider_cfg in providers_cfg.items():
            if not isinstance(provider_cfg, dict):
                continue
            if not _providers_match_for_context(provider_key, provider):
                continue
            api_key = _resolve_key(provider_cfg.get("api_key"), provider_cfg.get("key_env"))
            if api_key:
                return api_key

    model_cfg = cfg.get("model", {})
    if isinstance(model_cfg, dict):
        model_provider = _canonical_context_provider(model_cfg.get("provider"))
        if not provider or _providers_match_for_context(model_provider, provider):
            api_key = _resolve_key(model_cfg.get("api_key"), model_cfg.get("key_env"))
            if api_key:
                return api_key
    return ""


def _context_length_lookup_inputs_for_model(
    model: str | None,
    provider: str | None = None,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    cfg: dict | None = None,
) -> _ContextLengthLookupInputs:
    """Return the effective metadata resolver inputs for a WebUI model.

    ``agent.model_metadata.get_model_context_length`` understands global
    ``config_context_length`` and custom-provider overrides, but only when the
    matching base URL is supplied. WebUI also owns ``providers.<provider>.models``
    overrides, so normalize those here and keep route/session-save/SSE aligned.
    """
    model_for_lookup = str(model or "").strip()
    if not model_for_lookup:
        return _ContextLengthLookupInputs()

    if cfg is None:
        try:
            from api.config import get_config as _get_config_for_cl

            cfg = _get_config_for_cl()
        except Exception:
            cfg = {}
    cfg = cfg if isinstance(cfg, dict) else {}

    bare_model, explicit_provider = _split_provider_qualified_model(model_for_lookup)
    effective_provider = _canonical_context_provider(provider or explicit_provider)
    effective_base_url = str(base_url or "").strip()

    model_cfg = cfg.get("model", {}) if isinstance(cfg, dict) else {}
    if isinstance(model_cfg, dict):
        if not effective_provider:
            effective_provider = _canonical_context_provider(model_cfg.get("provider"))
        if not effective_base_url:
            effective_base_url = str(model_cfg.get("base_url") or "").strip()

    custom_providers = cfg.get("custom_providers") if isinstance(cfg, dict) else None
    if not isinstance(custom_providers, list):
        custom_providers = None

    provider_context_length = None
    providers_cfg = (cfg.get("providers") or {}) if isinstance(cfg, dict) else {}
    if isinstance(providers_cfg, dict):
        for provider_key, provider_cfg in providers_cfg.items():
            if not isinstance(provider_cfg, dict):
                continue
            if not _providers_match_for_context(provider_key, effective_provider):
                continue
            if not effective_base_url:
                effective_base_url = str(provider_cfg.get("base_url") or "").strip()
            provider_context_length = _models_config_context_length(
                provider_cfg.get("models"),
                bare_model or model_for_lookup,
            )
            break

    custom_context_length = None
    effective_api_key = str(api_key or "").strip()
    if custom_providers:
        target_base = effective_base_url.rstrip("/")
        model_candidates = set(_model_lookup_candidates(bare_model or model_for_lookup))
        for entry in custom_providers:
            if not isinstance(entry, dict):
                continue
            entry_name = str(entry.get("name") or "").strip()
            entry_slug = _custom_provider_slug_for_context(entry_name)
            entry_base = str(entry.get("base_url") or "").strip()
            entry_base_norm = entry_base.rstrip("/")
            provider_matches = bool(
                effective_provider
                and (
                    effective_provider == entry_slug
                    or effective_provider == entry_name.lower()
                    or (effective_provider == "custom" and len(custom_providers) == 1)
                )
            )
            base_matches = bool(target_base and entry_base_norm and target_base == entry_base_norm)
            model_matches = bool(model_candidates.intersection(set(_model_lookup_candidates(entry.get("model")))))
            models_cfg = entry.get("models")
            if isinstance(models_cfg, dict):
                model_matches = model_matches or any(candidate in models_cfg for candidate in model_candidates)
            if not (provider_matches or base_matches or (not effective_provider and model_matches)):
                continue
            if not effective_provider and entry_slug:
                effective_provider = entry_slug
            if not effective_base_url and entry_base:
                effective_base_url = entry_base
            if not effective_api_key:
                effective_api_key = _custom_provider_api_key_for_context(entry, effective_provider or entry_slug)
            custom_context_length = _models_config_context_length(models_cfg, bare_model or model_for_lookup)
            break

    global_context_length = None
    if isinstance(model_cfg, dict):
        cfg_default_model = str(model_cfg.get("default") or "").strip()
        raw_cfg_ctx = model_cfg.get("context_length")
        if raw_cfg_ctx is not None and (
            not cfg_default_model
            or _model_matches_configured_default(
                model_for_lookup,
                cfg_default_model,
                effective_provider,
            )
        ):
            global_context_length = _positive_context_length(raw_cfg_ctx)

    if not effective_api_key:
        effective_api_key = _context_length_config_api_key_for_provider(effective_provider, cfg)

    return _ContextLengthLookupInputs(
        config_context_length=provider_context_length or custom_context_length or global_context_length,
        custom_providers=custom_providers,
        base_url=effective_base_url,
        provider=effective_provider,
        api_key=effective_api_key,
    )



def _should_attach_codex_provider_context(model: str, raw_active_provider: str, catalog: dict) -> bool:
    """Return True when a bare Codex model needs separate provider context.

    OpenAI, OpenAI Codex, Copilot, and OpenRouter can all expose GPT-looking
    bare names. If a session stores only ``gpt-...`` while Codex is active, a
    later provider-list/default-model round trip can lose the user's Codex
    choice. Store the provider separately instead of converting the persisted
    model to ``@openai-codex:model``.
    """
    if raw_active_provider != "openai-codex":
        return False
    if not model.lower().startswith("gpt"):
        return False
    for group in catalog.get("groups") or []:
        if str(group.get("provider_id") or "").strip().lower() != "openai-codex":
            continue
        return any(
            _catalog_model_id_matches(entry.get("id"), model)
            for entry in group.get("models", [])
            if isinstance(entry, dict)
        )
    return False

def _read_profile_model_config(
    session,
    requested_provider: str | None,
) -> tuple[str | None, str | None, dict | None]:
    """Read model.provider, model.default, and the full profile config dict.

    Returns (profile_provider, profile_default_model, profile_config_dict).
    The first two are None when the session has no profile or the profile config
    is unreadable; profile_config_dict is None in the same cases so callers only
    pay for one YAML parse.

    When the session already has an explicit ``requested_provider``, the profile
    ``model.provider`` is not returned (first tuple element is None) so profile
    does not override the session provider. ``profile_default_model`` is still
    returned for suffix repair (#5127) only when the profile's configured
    provider matches ``requested_provider`` after normalization.

    perf(webui/session-load-latency) tier2a: the parse is wrapped in a
    per-process LRU keyed by (profile_name, config_mtime, size). The
    function fires on every chat-open for sessions under a named
    profile (resolve_model=1 path), and the YAML parse alone is
    hundreds of µs to single-digit ms on the Chromebook. Cache TTL
    60s is a backstop in case mtime resolution is poor on a given
    filesystem; under normal edits the mtime changes and invalidates
    immediately.
    """
    if not getattr(session, "profile", None):
        return None, None, None

    try:
        from api.profiles import get_hermes_home_for_profile

        _profile_name = str(session.profile or "")
        _profile_home = get_hermes_home_for_profile(_profile_name)
        _profile_cfg_path = os.path.join(str(_profile_home), "config.yaml")
        if not os.path.isfile(_profile_cfg_path):
            return None, None, None
        _pcfg = _read_profile_config_cached(_profile_name, _profile_cfg_path)
        if _pcfg is None:
            return None, None, None
        _model_cfg = _pcfg.get("model") or {}
        if not isinstance(_model_cfg, dict):
            return None, None, _pcfg
        _provider = (_model_cfg.get("provider") or "").strip() or None
        _default = (_model_cfg.get("default") or "").strip() or None
    except Exception:
        logger.warning(
            "profile provider read failed for %r",
            getattr(session, "profile", None),
            exc_info=True,
        )
        return None, None, None

    _requested = _clean_session_model_provider(requested_provider)
    if _requested:
        _profile_prov = _clean_session_model_provider(_provider)
        if _profile_prov != _requested:
            return None, None, _pcfg
        return None, _default, _pcfg
    return _provider, _default, _pcfg


# perf(webui/session-load-latency) tier2a: process-wide cache for parsed
# profile config.yaml. Key = (profile_name, inode, mtime, size); value = parsed
# dict. inode tracks atomic-rename edits (most editors replace files, giving a
# new inode on Linux). mtime+size auto-invalidates on in-place edits; a 60s TTL
# is the backstop in case of coarse mtime resolution (some network filesystems
# round mtime to whole seconds — the size guard catches a write of equal-length
# bytes within the same second). On cache hit, a full-content comparison catches
# any in-place rewrite that inode+mtime+size missed (Greptile P1, PR#5803
# discussion_r3548477915). Reading and comparing the full file content (~1-10KB)
# is much cheaper than yaml.safe_load(). Reads are guarded by a single Lock to
# keep the hot path simple; the underlying yaml.safe_load is the slow step, not
# the lock, so contention is bounded.
_PROFILE_CONFIG_CACHE: "dict[tuple, tuple[float, str, dict]]" = {}
_PROFILE_CONFIG_CACHE_TTL_SECONDS = 60.0
_PROFILE_CONFIG_CACHE_LOCK = threading.Lock()


def _read_profile_config_cached(profile_name: str, cfg_path: str) -> dict | None:
    """Return parsed profile config, caching by (inode, mtime, size) with
    TTL backstop and full-content verification.

    The full-content comparison reads the current file and compares it to a
    copy stored in the cache entry. This catches any in-place rewrite where
    inode+mtime+size are identical, regardless of where in the file the change
    occurs — unlike a fixed-length prefix comparison, edits to fields after the
    first N characters are always detected. Reading and comparing the full file
    content (~1-10KB for a typical config.yaml) is much cheaper than
    yaml.safe_load().

    NOTE: The cache key uses inode+mtime+size to handle the common cases
    (atomic-rename editors -> new inode; in-place editors -> mtime/size
    change). The full-content comparison is a backstop for the rare case where
    all three collide (e.g., sed -i on a filesystem with coarse mtime
    resolution, writing the same byte count).
    """
    try:
        st = os.stat(cfg_path)
    except OSError:
        return None
    mtime = float(getattr(st, "st_mtime", 0.0) or 0.0)
    size = int(getattr(st, "st_size", 0) or 0)
    inode = int(getattr(st, "st_ino", 0) or 0)
    key = (str(profile_name or ""), inode, mtime, size)
    now = time.monotonic()
    with _PROFILE_CONFIG_CACHE_LOCK:
        cached = _PROFILE_CONFIG_CACHE.get(key)
        if cached is not None:
            cached_at, cached_content, cached_dict = cached
            if (now - cached_at) <= _PROFILE_CONFIG_CACHE_TTL_SECONDS:
                # Full content comparison catches any in-place rewrite where
                # inode+mtime+size are identical but the file content changed.
                # Reading and comparing the full file (~1-10KB) is cheaper than
                # yaml.safe_load(). Unlike a fixed-length prefix, this detects
                # edits anywhere in the file. Greptile P1 (PR#5803).
                _current_content = None
                try:
                    with open(cfg_path, "r", encoding="utf-8") as _f:
                        _current_content = _f.read()
                except Exception:
                    pass
                if _current_content == cached_content:
                    return cached_dict
                # Content changed while key collided — fall through to re-parse
    import yaml
    try:
        with open(cfg_path, encoding="utf-8") as _f:
            content = _f.read()
            parsed = yaml.safe_load(content) or {}
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    with _PROFILE_CONFIG_CACHE_LOCK:
        _PROFILE_CONFIG_CACHE[key] = (now, content, parsed)
        # Cap the cache at 32 entries; profiles are bounded in practice
        # and unbounded growth would be a leak.
        if len(_PROFILE_CONFIG_CACHE) > 32:
            # Drop the oldest entry by insertion order (dict is ordered).
            for old_key in list(_PROFILE_CONFIG_CACHE.keys())[:max(0, len(_PROFILE_CONFIG_CACHE) - 32)]:
                _PROFILE_CONFIG_CACHE.pop(old_key, None)
    return parsed


def _load_profile_config_dict(session) -> dict | None:
    """Load the session profile's config.yaml as a dict, or None."""
    if not getattr(session, "profile", None):
        return None
    try:
        from api.profiles import get_hermes_home_for_profile

        _profile_cfg_path = os.path.join(
            str(get_hermes_home_for_profile(session.profile)),
            "config.yaml",
        )
        if not os.path.isfile(_profile_cfg_path):
            return None
        import yaml

        with open(_profile_cfg_path, encoding="utf-8") as _f:
            _pcfg = yaml.safe_load(_f) or {}
        return _pcfg if isinstance(_pcfg, dict) else None
    except Exception:
        logger.warning(
            "profile config read failed for %r",
            getattr(session, "profile", None),
            exc_info=True,
        )
        return None


def _ordered_custom_provider_model_ids(entry: dict) -> list[str]:
    """Model ids from a custom_providers entry (default model + dict/list models)."""
    ordered: list[str] = []
    _cp_model = str(entry.get("model") or "").strip()
    if _cp_model:
        ordered.append(_cp_model)
    _cp_models = entry.get("models")
    if isinstance(_cp_models, dict):
        for _key in _cp_models.keys():
            if isinstance(_key, str):
                _kid = _key.strip()
                if _kid and _kid not in ordered:
                    ordered.append(_kid)
    elif isinstance(_cp_models, list):
        for _item in _cp_models:
            if isinstance(_item, str):
                _mid = _item.strip()
                if _mid and _mid not in ordered:
                    ordered.append(_mid)
            elif isinstance(_item, dict):
                _mid = str(
                    _item.get("id") or _item.get("model") or _item.get("name") or ""
                ).strip()
                if _mid and _mid not in ordered:
                    ordered.append(_mid)
    return ordered


def _repair_bare_custom_provider_model(
    bare_model: str,
    provider: str | None,
    *,
    config_obj: dict | None = None,
) -> str | None:
    """Re-qualify a bare model ID using the named custom provider's config (#5314).

    Returns the fully namespaced model id when ``bare_model`` matches the suffix
    of a registered id on ``custom_providers``; otherwise None. Model ids are
    scanned in config declaration order (default ``model`` first, then
    ``models`` dict keys or list entries) so repair is deterministic when
    suffixes collide.

    When ``config_obj`` is set (typically the session profile's config.yaml),
    only that object's ``custom_providers`` are scanned. Otherwise uses
    ``get_config()`` for the active global config (not the raw ``cfg`` alias).
    """
    try:
        model = str(bare_model or "").strip()
        prov = _clean_session_model_provider(provider)
        if not model or "/" in model or not prov:
            return None
        if prov != "custom" and not str(prov).startswith("custom:"):
            return None
        from api.config import (
            _custom_provider_entries,
            _custom_provider_slug_from_name,
            get_config,
        )

        if isinstance(config_obj, dict):
            _entries = _custom_provider_entries(config_obj)
        else:
            _cfg = get_config()
            _entries = _custom_provider_entries(
                _cfg if isinstance(_cfg, dict) else None
            )
        prov_norm = str(prov).strip().lower()
        raw_suffix = prov_norm.removeprefix("custom:")
        _matching_cp = None
        for _entry in _entries:
            entry_name = str(_entry.get("name") or "").strip().lower()
            slug = _custom_provider_slug_from_name(_entry.get("name"))
            if not slug:
                continue
            if (
                prov_norm in {entry_name, slug}
                or raw_suffix == slug.removeprefix("custom:")
            ):
                _matching_cp = _entry
                break
        if not _matching_cp:
            return None
        for _id in _ordered_custom_provider_model_ids(_matching_cp):
            if "/" in _id and _id.rsplit("/", 1)[-1] == model:
                return _id
        return None
    except Exception:
        return None


def _moa_fast_path_model_state(model: str) -> tuple[str, str, bool]:
    """Strip an optional ``@moa:``/``moa/`` prefix from an MoA-routed model.

    Split out of ``_resolve_compatible_session_model_state`` so the MoA
    fast-path stays a single-line call in that function body (see
    ``test_issue1855_resolve_model_provider_fast_path.py``, the fast-path/
    catalog-call ordering check scans a bounded window of that function's
    source, and inlining this here previously pushed the catalog call just
    past that window).
    """
    if model.startswith("@moa:"):
        return model.split(":", 1)[1].strip(), "moa", True
    if model.lower().startswith("moa/"):
        return model.split("/", 1)[1].strip(), "moa", True
    return model, "moa", False


def _resolve_compatible_session_model_state(
    model_id: str | None,
    model_provider: str | None = None,
    *,
    profile_provider: str | None = None,
    profile_default_model: str | None = None,
    profile_config: dict | None = None,
    explicit_model_pick: bool = False,
    prefer_cached_catalog: bool = False,
) -> tuple[str, str | None, bool]:
    """Return (effective_model, effective_provider, model_was_normalized).

    Sessions can outlive provider changes. When an older session still points at
    a different provider namespace (for example `gemini/...` after switching the
    agent to OpenAI Codex), reusing that stale model causes chat startup to hit
    the wrong backend and fail. Normalize only obvious cross-provider mismatches.
    When a model has an explicit provider context, keep the model string itself
    in its picker/API shape and carry the provider as separate state.

    Fast path (#1855): when the caller supplies both a model and an explicit
    ``model_provider`` AND the model is not itself ``@provider:model``-qualified,
    we can return the inputs verbatim without calling ``get_available_models()``.
    The slow path below would arrive at the same answer via
    ``if requested_provider and not explicit_provider: return model, requested_provider, False``
    after paying the full catalog-build cost. Avoiding the catalog here keeps
    ``POST /api/chat/start`` snappy even when the model catalog is cold and the
    rebuild has to make network calls (custom OpenAI-compat endpoints,
    OpenRouter ``/models``, LM Studio ``/models``, credential pool refresh),
    those used to wedge the handler for >100s and trigger 502s on default-60s
    reverse proxies, even though the WebUI itself eventually responded.

    ``prefer_cached_catalog=True`` (ours-original) makes the catalog lookup
    non-blocking: it resolves from the warm/disk cache or a network-free
    minimal catalog and NEVER triggers a live per-provider rebuild (the
    Copilot token-exchange HTTPS call that hangs a server-initiated wakeup
    turn, see rebase report §1/§3/model-resolve-hang). Human-initiated
    chat/start leaves this False to keep full live discovery; a session that
    already has a persisted model still resolves correctly because the
    persisted model wins over the catalog and the catalog is only consulted
    for the default-model backstop.
    """
    model = str(model_id or "").strip()
    requested_provider = _clean_session_model_provider(model_provider)
    if model and requested_provider == "moa":
        return _moa_fast_path_model_state(model)
    if model and requested_provider and model.startswith(f"@{requested_provider}:"):
        try:
            from api.config import cfg as _active_cfg

            providers_cfg = _active_cfg.get("providers") if isinstance(_active_cfg, dict) else {}
        except Exception:
            providers_cfg = {}
        if isinstance(providers_cfg, dict) and requested_provider in providers_cfg:
            return model, requested_provider, False
    if model and requested_provider:
        # Only safe when the model itself does not carry an ``@provider:model``
        # qualifier — qualified strings require the catalog to decide whether
        # the qualifier matches the active provider (see slow path below).
        bare_model, explicit_provider = _split_provider_qualified_model(model)
        model_prefix = model.split("/", 1)[0].strip().lower() if "/" in model else ""
        stale_codex_openai_slash_id = (
            requested_provider == "openai-codex"
            and model_prefix == "openai"
        )
        if not explicit_provider and not stale_codex_openai_slash_id:
            _profile_default = str(profile_default_model or "").strip()
            _profile_prov = _clean_session_model_provider(profile_provider)
            _providers_match_for_repair = (
                _profile_prov is None or _profile_prov == requested_provider
            )
            if (
                _profile_default
                and "/" in _profile_default
                and "/" not in model
                and _profile_default.rsplit("/", 1)[-1] == model
                and _providers_match_for_repair
                and (
                    requested_provider == "custom"
                    or str(requested_provider).startswith("custom:")
                )
            ):
                return _profile_default, requested_provider, True

            _repaired_model = _repair_bare_custom_provider_model(
                model,
                requested_provider,
                config_obj=profile_config,
            )
            if _repaired_model:
                return _repaired_model, requested_provider, True

            return model, requested_provider, False

    # Default (human chat/start) path calls get_available_models() with NO
    # kwargs so it stays signature-compatible with the many tests that stub
    # get_available_models as a zero-arg callable. Only the server-side wakeup
    # path (prefer_cached_catalog=True) opts into the cache-only mode. Some
    # tests monkeypatch get_available_models as a zero-arg callable, so probe
    # the (possibly monkeypatched) signature for ``prefer_cache`` rather than
    # catching TypeError — a blanket ``except TypeError`` would also swallow a
    # genuine TypeError raised *inside* get_available_models(prefer_cache=True)
    # and silently fall back to the slow live provider rebuild that
    # prefer_cached_catalog=True is meant to avoid.
    if prefer_cached_catalog:
        import inspect as _inspect

        try:
            _gam_accepts_prefer_cache = (
                "prefer_cache" in _inspect.signature(get_available_models).parameters
            )
        except (TypeError, ValueError):
            # Builtins / C-callables can refuse introspection; assume the
            # zero-arg stub shape in that case.
            _gam_accepts_prefer_cache = False
        if _gam_accepts_prefer_cache:
            catalog = get_available_models(prefer_cache=True)
        else:
            catalog = get_available_models()
    else:
        catalog = get_available_models()
    default_model = str(catalog.get("default_model") or DEFAULT_MODEL or "").strip()

    # Profile-aware resolution: when the caller supplies profile context
    # (not an explicit per-chat override), use the profile's provider and
    # default model as the resolution context instead of the catalog's
    # active_provider / default_model. This preserves the repair path
    # (stale models still get normalized) but normalizes to the profile's
    # default model under the profile's provider rather than the global default.
    bare_model, explicit_provider = _split_provider_qualified_model(model) if model else ("", None)
    if profile_provider and not explicit_provider:
        _profile_provider_normalized = _normalize_provider_id(profile_provider)
        _profile_default = str(profile_default_model or "").strip()
        if not model:
            _fallback = _profile_default or default_model
            return _fallback, profile_provider, bool(_fallback)

        model_prefix = model.split("/", 1)[0].strip().lower() if "/" in model else ""
        model_provider_from_name = _normalize_provider_id(model_prefix) if "/" in model else ""

        model_family = ""
        if "/" not in model:
            model_lower = model.lower()
            for bare_prefix in ("gpt", "claude", "gemini"):
                if model_lower.startswith(bare_prefix):
                    model_family = _normalize_provider_id(bare_prefix)
                    break

        if model_family and model_family != _profile_provider_normalized:
            if explicit_model_pick:
                # User explicitly chose a cross-family model; honor it (#3737)
                return model, profile_provider, False
            _target = _profile_default or default_model
            return _target, profile_provider, True

        if (
            "/" in model
            and str(profile_provider).strip().lower() == "openai-codex"
            and model_provider_from_name == "openai"
        ):
            _target = _profile_default or default_model
            return _target, profile_provider, True

        # Slash-qualified models (e.g. openai/gpt-5.4-mini) are native IDs on
        # OpenRouter and custom providers, not cross-provider artifacts. Only
        # repair when the profile provider actually requires a different family.
        if "/" in model and _profile_provider_normalized in {"openrouter", "custom", ""}:
            return model, profile_provider, False

        if "/" in model and model_provider_from_name and model_provider_from_name != _profile_provider_normalized:
            _target = _profile_default or default_model
            return _target, profile_provider, True

        # Async server-side continuations (for example delegate_task completion
        # re-entry) can arrive here with profile context but without a usable
        # requested_provider, bypassing the fast-path custom-provider repair
        # above. If the profile's configured custom-provider default is a
        # slash-qualified model whose suffix matches the bare session model,
        # repair back to the profile default before the provider call (#5225).
        if (
            "/" not in model
            and _profile_default
            and "/" in _profile_default
            and _profile_default.rsplit("/", 1)[-1] == model
            and (
                _profile_provider_normalized == "custom"
                or str(profile_provider).startswith("custom:")
            )
        ):
            return _profile_default, profile_provider, True

        _repaired_model = _repair_bare_custom_provider_model(
            model,
            profile_provider,
            config_obj=profile_config,
        )
        if _repaired_model:
            return _repaired_model, profile_provider, True

        return model, profile_provider, False

    if not model:
        return default_model, requested_provider, bool(default_model)

    active_provider = _normalize_provider_id(catalog.get("active_provider"))
    # Also keep the raw active_provider slug for cross-provider detection with
    # non-listed providers (ollama-cloud, deepseek, xai, etc.) that _normalize_provider_id
    # returns "" for. If the raw provider is set but normalization returned "", we still
    # want to detect that a session model from a known provider (e.g. openai/gpt-5.4-mini)
    # is stale relative to this unknown active provider. (#1023)
    raw_active_provider = str(catalog.get("active_provider") or "").strip().lower()
    if not active_provider and not raw_active_provider:
        bare_model, explicit_provider = _split_provider_qualified_model(model)
        return model, explicit_provider or requested_provider, False

    bare_for_context, explicit_provider = _split_provider_qualified_model(model)
    if requested_provider and not explicit_provider:
        model_prefix = model.split("/", 1)[0].strip().lower() if "/" in model else ""
        stale_codex_openai_slash_id = (
            raw_active_provider == "openai-codex"
            and requested_provider == "openai-codex"
            and model_prefix == "openai"
        )
        if not stale_codex_openai_slash_id:
            return model, requested_provider, False

    if model.startswith("@") and ":" in model:
        provider_raw = explicit_provider or ""
        provider_normalized = _normalize_provider_id(provider_raw)
        bare_model = bare_for_context.strip()
        if not provider_raw or not bare_model:
            return model, requested_provider, False

        # A fresh, explicit user pick is by definition not a stale artifact, so
        # honor the @provider:model exactly as chosen — never reroute it via the
        # active-provider family repair or the cold-catalog fallback below (a bare
        # id like "gpt-oss-120b" under an OpenAI-active agent would otherwise get
        # pulled to OpenAI by the family-match branch). If the named provider is
        # unreachable the user sees a clear run-time error rather than a silent
        # model swap. Must sit above the family-match repair (#3737 principle).
        if explicit_model_pick:
            return model, provider_raw, False

        raw_provider_ids, normalized_provider_ids = _catalog_provider_id_sets(catalog)
        hint_matches_active = (
            provider_raw == raw_active_provider
            or provider_raw == active_provider
            or (provider_normalized and provider_normalized == active_provider)
        )
        if hint_matches_active:
            # The @provider:model hint explicitly names the active provider, so this
            # selection is intentional — not a stale cross-provider artifact. Return
            # the full @provider:model string unchanged so downstream (resolve_model_provider
            # in config.py) can route through the correct provider. Stripping the prefix
            # here would collapse duplicate model IDs from different providers back to the
            # bare ID, causing the first matching provider to win on the next UI render
            # and the wrong provider to be used for the agent run. (#1253)
            return model, provider_raw, False

        if _catalog_has_provider(
            provider_raw,
            provider_normalized,
            raw_provider_ids,
            normalized_provider_ids,
        ):
            return model, provider_raw, False

        if _model_matches_active_provider_family(bare_model, active_provider):
            provider_context = (
                raw_active_provider
                if _should_attach_codex_provider_context(bare_model, raw_active_provider, catalog)
                else None
            )
            return bare_model, provider_context, True
        # On NON-explicit resolves (2nd+ turn, chat switch — explicit picks already
        # returned above), preserve the selection only when all three hold:
        #
        #   * provider_normalized == "" — a non-first-party provider hint
        #     (ollama-cloud / deepseek / xai / a named custom proxy). First-party
        #     families fall through to the stale-cross-provider repair below.
        #
        #   * the BARE model is not a first-party family id (does not start with
        #     gpt/claude/gemini), i.e. not a misrouted first-party model that a
        #     vanished provider used to host (e.g. "@copilot:claude-opus-4.6").
        #
        #   * the provider is KNOWN or CONFIGURED. This is the load-bearing
        #     distinction: catalog-absence has two causes —
        #       (a) a cold live-discovery provider (ollama-cloud is configured; its
        #           group just isn't in this cached snapshot yet) → preserve, and
        #       (b) a genuinely removed/unknown provider ("@removed:mistral-large"
        #           configured nowhere) → fall through to the default so chat/start
        #           doesn't route to an unreachable provider.
        #     _provider_is_known_or_configured() decides this from the static
        #     provider registry + config state, NOT from the cold catalog snapshot
        #     (re-deriving that live would defeat the prefer_cached_catalog win).
        #
        # DELIBERATE: the registry test treats a KNOWN built-in (deepseek, minimax,
        # ollama-cloud, …) as preservable even when the user has no key configured
        # for it. We accept this on purpose. The only fully-reliable "is this
        # provider authenticated" signal is the live auth store / catalog rebuild —
        # exactly the cost this hot path avoids — and a cheap config/env-only check
        # would mis-classify providers configured via OAuth/auth-store (ollama-cloud
        # among them), re-introducing the original silent-revert bug for them. So a
        # known-but-unconfigured pick is kept; the user gets a clear run-time auth
        # error instead of a silent swap to the default. Pinned by
        # test_at_provider_known_unconfigured_builtin_is_intentionally_preserved.
        #
        # KNOWN LIMITATION: the first-party-family test is a bare-name prefix match
        # (the same approximation _model_matches_active_provider_family uses). A
        # genuine third-party model whose name merely *starts* with gpt/claude/
        # gemini (e.g. "@ollama:gpt4all-mini") is therefore still mis-classified as
        # first-party and reverted on non-explicit paths. A name-based check cannot
        # disambiguate that; the behavior is pinned by
        # test_at_provider_first_party_named_third_party_model_known_limitation.
        _bare_is_first_party_family = any(
            bare_model.lower().startswith(_p) for _p in ("gpt", "claude", "gemini")
        )
        if (
            not provider_normalized
            and not _bare_is_first_party_family
            and _provider_is_known_or_configured(provider_raw)
        ):
            return model, provider_raw, False
        if default_model:
            provider_context = (
                raw_active_provider
                if _should_attach_codex_provider_context(default_model, raw_active_provider, catalog)
                else None
            )
            return default_model, provider_context, True
        return model, provider_raw, False

    slash = model.find("/")
    if slash < 0:
        if explicit_model_pick:
            # User explicitly chose this model; don't second-guess (#3737)
            return model, requested_provider, False
        model_lower = model.lower()
        for bare_prefix in ("gpt", "claude", "gemini"):
            if model_lower.startswith(bare_prefix):
                model_provider = _normalize_provider_id(bare_prefix)
                if model_provider and model_provider != active_provider and default_model:
                    provider_context = (
                        raw_active_provider
                        if _should_attach_codex_provider_context(default_model, raw_active_provider, catalog)
                        else None
                    )
                    return default_model, provider_context, True
                provider_context = (
                    raw_active_provider
                    if _should_attach_codex_provider_context(model, raw_active_provider, catalog)
                    else requested_provider
                )
                return model, provider_context, False
        return model, requested_provider, False

    model_provider = _normalize_provider_id(model[:slash])

    # For custom/openrouter active providers: only skip normalization when the
    # model's namespace prefix is actually routable by a group in the catalog.
    # A user who only has custom_providers configured (active_provider="custom")
    # with a stale session model like "openai/gpt-5.4-mini" would otherwise
    # never get cleaned up, causing "(unavailable)" to appear in the picker.
    if active_provider in {"custom", "openrouter"}:
        # These namespaces are always routable as-is — preserve them.
        if model_provider in {"", "custom", "openrouter"}:
            return model, requested_provider, False
        # Check if any catalog group can actually route this model's prefix.
        groups = catalog.get("groups") or []
        routable_provider_ids = {
            _normalize_provider_id(g.get("provider_id") or "") for g in groups
        }
        # openrouter group can route any provider/model namespace
        has_openrouter_group = any(
            (g.get("provider_id") or "") == "openrouter" for g in groups
        )
        if model_provider in routable_provider_ids or has_openrouter_group:
            return model, requested_provider, False
        # Model prefix is not routable — stale cross-provider reference, clear it.
        if default_model:
            return default_model, requested_provider, True
        return model, requested_provider, False

    # Skip normalization for models on custom/openrouter namespaces — these are
    # user-controlled and should never be silently replaced.
    #
    # OpenAI Codex is intentionally normalized to the OpenAI family above so bare
    # GPT IDs survive provider switches. Slash-qualified OpenAI IDs are different:
    # ``openai/gpt-...`` is the OpenRouter shape for OpenAI models, and
    # resolve_model_provider() routes that through OpenRouter when Codex is the
    # configured provider. Legacy sessions can carry that stale slash ID without
    # a saved model_provider, so repair it to the active Codex default unless the
    # session/request explicitly says it is an OpenRouter selection. (#1734)
    if (
        raw_active_provider == "openai-codex"
        and model_provider == "openai"
        and requested_provider in {None, "openai-codex"}
        and default_model
    ):
        # Persist provider_context = "openai-codex" unconditionally on this
        # repair path so the resolved shape is stable across resolutions
        # (Opus stage-303 SHOULD-FIX: avoid redundant repair-writes per
        # chat-start when the catalog-coverage check fails — e.g. if a
        # future Codex default is itself slash-prefixed). Once we've
        # decided the session belongs to Codex, persist that decision.
        return default_model, raw_active_provider, True

    # Also normalize when the model is from a known provider but the active provider
    # is an unlisted one (e.g. ollama-cloud) — active_provider is "" in that case
    # but raw_active_provider is set. If model_provider doesn't start with the raw
    # active provider name, the session model is stale. (#1023)
    _active_for_compare = active_provider or raw_active_provider
    if model_provider and model_provider not in {"", "custom", "openrouter"} and model_provider != _active_for_compare and default_model:
        return default_model, requested_provider, True
    return model, requested_provider, False


def _resolve_compatible_session_model(model_id: str | None) -> tuple[str, bool]:
    """Return (effective_model, model_was_normalized) for legacy callers."""
    effective_model, _provider, changed = _resolve_compatible_session_model_state(model_id)
    return effective_model, changed


def _normalize_session_model_in_place(session) -> str:
    original_model = getattr(session, "model", None) or ""
    original_provider = _clean_session_model_provider(
        getattr(session, "model_provider", None)
    )
    effective_model, effective_provider, changed = _resolve_compatible_session_model_state(
        original_model or None,
        original_provider,
    )
    provider_changed = effective_provider != original_provider
    # Only persist the correction if the session had an explicit model that needed changing.
    # Sessions with no model stored (empty/None) get the effective default returned without
    # a disk write — no need to rebuild the index for a fill-in-blank operation.
    if original_model and effective_model and (
        (changed and original_model != effective_model) or provider_changed
    ):
        if changed and original_model != effective_model:
            session.model = effective_model
        session.model_provider = effective_provider
        session.save(touch_updated_at=False)
    return effective_model


def _resolve_effective_session_model_for_display(session) -> str:
    """Resolve the model a session should display without mutating persisted state.

    `GET /api/session` should stay side-effect free. If a stale persisted model
    needs normalization for the current provider configuration, return the
    effective model for the response payload only and leave disk state alone.
    """
    original_model = getattr(session, "model", None) or ""
    requested_provider = getattr(session, "model_provider", None)
    _pp_provider, _pp_default, _pp_cfg = _read_profile_model_config(session, requested_provider)
    effective_model, _provider, _changed = _resolve_compatible_session_model_state(
        original_model or None,
        requested_provider,
        profile_provider=_pp_provider,
        profile_default_model=_pp_default,
        profile_config=_pp_cfg,
        # GET /api/session is a hot, side-effect-free per-tab/per-poll path.
        # It must never pay the cold live provider-catalog rebuild (a
        # botocore IMDS probe that cannot resolve on a non-AWS / WSL / corp
        # network, plus anthropic/openrouter /models). That rebuild is
        # un-cacheable here (auth.json fingerprint churn) so every cold call
        # cost ~10s and, run concurrently across browser tabs, serialized on
        # the models-cache lock and starved SSE/streaming -> BrokenPipe storm
        # (#multi-tab-streaming-interlock). The persisted session model is
        # authoritative; the catalog is only a default-model backstop, which
        # the network-free minimal catalog already provides.
        prefer_cached_catalog=True,
    )
    return effective_model or original_model

def _resolve_effective_session_model_provider_for_display(session) -> str | None:
    original_model = getattr(session, "model", None) or ""
    requested_provider = getattr(session, "model_provider", None)
    _pp_provider, _pp_default, _pp_cfg = _read_profile_model_config(session, requested_provider)
    _model, provider, _changed = _resolve_compatible_session_model_state(
        original_model or None,
        requested_provider,
        profile_provider=_pp_provider,
        profile_default_model=_pp_default,
        profile_config=_pp_cfg,
        # See _resolve_effective_session_model_for_display: same hot
        # side-effect-free GET /api/session path; must not trigger the cold
        # live rebuild. prefer_cached_catalog resolves from warm/disk cache
        # or the network-free minimal catalog.
        prefer_cached_catalog=True,
    )
    return provider


def _resolve_context_length_for_session_model(
    model: str | None,
    provider: str | None = None,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
) -> int:
    """Best-effort current context window for a session model.

    Persisted session context metadata is a snapshot from a prior model call.
    During session hydration/model switching, the current model metadata should
    be allowed to replace that stale snapshot.
    """
    model_for_lookup = str(model or "").strip()
    if not model_for_lookup:
        return 0
    try:
        from agent.model_metadata import get_model_context_length as _get_cl
        from api.config import get_config as _get_config_for_cl

        _cfg_for_cl = _get_config_for_cl()
        _ctx_lookup = _context_length_lookup_inputs_for_model(
            model_for_lookup,
            provider,
            base_url=base_url,
            api_key=api_key,
            cfg=_cfg_for_cl if isinstance(_cfg_for_cl, dict) else {},
        )
        try:
            return _get_cl(
                model_for_lookup,
                _ctx_lookup.base_url,
                api_key=_ctx_lookup.api_key,
                config_context_length=_ctx_lookup.config_context_length,
                provider=_ctx_lookup.provider or provider or "",
                custom_providers=_ctx_lookup.custom_providers,
            ) or 0
        except TypeError:
            # Older hermes-agent builds: legacy 2-arg form.
            return _get_cl(model_for_lookup, _ctx_lookup.base_url) or 0
    except Exception:
        return 0


def _session_context_length_lookup_state(
    model: str | None,
    provider: str | None,
) -> tuple[str, str, str, str]:
    """Return model/provider/base_url/api_key inputs for session context lookup.

    This stays config-based and side-effect-free for GET /api/session. It avoids
    a live provider catalog rebuild while still aligning the reload path with
    the base URL / custom-provider key shape used by streaming saves. (#4248)
    """
    model_for_lookup = str(model or "").strip()
    provider_for_lookup = str(provider or "").strip()
    base_url_for_lookup = ""
    api_key_for_lookup = ""
    if not model_for_lookup:
        return "", provider_for_lookup, "", ""
    try:
        from api.config import resolve_model_provider

        model_for_resolution = model_with_provider_context(model_for_lookup, provider_for_lookup or None)
        resolved_model, resolved_provider, resolved_base_url = resolve_model_provider(model_for_resolution)
        model_for_lookup = str(resolved_model or model_for_lookup).strip()
        provider_for_lookup = str(resolved_provider or provider_for_lookup or "").strip()
        base_url_for_lookup = str(resolved_base_url or "").strip()
    except Exception:
        logger.debug("session context-length lookup state resolution failed", exc_info=True)
    if provider_for_lookup.startswith("custom:"):
        try:
            from api.config import resolve_custom_provider_connection

            custom_key, custom_base = resolve_custom_provider_connection(provider_for_lookup)
            api_key_for_lookup = str(custom_key or "").strip()
            if not base_url_for_lookup:
                base_url_for_lookup = str(custom_base or "").strip()
        except Exception:
            logger.debug("custom provider context-length connection resolution failed", exc_info=True)
    return model_for_lookup, provider_for_lookup, base_url_for_lookup, api_key_for_lookup


def _session_model_identity_matches(
    stored_model: str | None,
    stored_provider: str | None,
    resolved_model: str | None,
    resolved_provider: str | None,
) -> bool:
    stored = str(stored_model or "").strip()
    resolved = str(resolved_model or "").strip()
    if not stored or not resolved:
        return False

    def _split_model_identity(value: str) -> tuple[str, str | None]:
        # Handle BOTH provider-qualified shapes so a slash-prefixed session model
        # (e.g. ``deepseek/deepseek-v4-1m``, OpenRouter-style) compares equal to its
        # resolved bare id. ``_split_provider_qualified_model`` only handles the
        # ``@provider:model`` form; without the slash case a reload of a
        # slash-stored model is wrongly treated as a model change, bypassing the
        # #4248 256k-clobber guard (Codex regression gate, v0.51.x).
        bare, prov = _split_provider_qualified_model(value)
        if prov is None and "/" in value:
            prefix, rest = value.split("/", 1)
            prefix = prefix.strip()
            rest = rest.strip()
            if prefix and rest:
                return rest, prefix
        return bare, prov

    stored_bare, stored_explicit_provider = _split_model_identity(stored)
    resolved_bare, resolved_explicit_provider = _split_model_identity(resolved)
    stored_provider_norm = _canonical_context_provider(stored_explicit_provider or stored_provider)
    resolved_provider_norm = _canonical_context_provider(resolved_explicit_provider or resolved_provider)
    if stored == resolved and stored_provider_norm == resolved_provider_norm:
        return True
    if stored_bare != resolved_bare:
        return False
    if stored_provider_norm and resolved_provider_norm:
        return stored_provider_norm == resolved_provider_norm
    return True


def _should_accept_session_context_length_refresh(
    persisted: int,
    resolved: int,
    *,
    model_changed: bool = False,
) -> bool:
    if not resolved:
        return False
    if not persisted:
        return True
    # #4248: an anonymous reload resolver can still fall through to the agent
    # metadata default fallback. Do not let that lower-confidence 256k value
    # clobber a larger context window persisted by the streaming path. If the
    # effective model changed, though, a 256k result may be the real new model
    # window and should replace the old snapshot.
    return model_changed or not (resolved == 256_000 and persisted > resolved)


def _rescale_threshold_tokens_for_context_window(
    threshold: int,
    old_window: int,
    new_window: int,
) -> int:
    try:
        threshold = int(threshold or 0)
        old_window = int(old_window or 0)
        new_window = int(new_window or 0)
    except (TypeError, ValueError):
        return 0
    if threshold <= 0 or old_window <= 0 or new_window <= 0:
        return 0
    return max(1, int(threshold * new_window / old_window))


def _worktree_default_from_config(profile: str | None) -> bool:
    """Return the agent's config-level ``worktree:`` default for *profile*.

    The agent CLI honors ``worktree: true`` in config.yaml for every session
    it creates (``use_worktree = worktree or w or CLI_CONFIG.get("worktree",
    False)``).  /api/session/new consults this only when the request body has
    no explicit ``worktree`` key, so both entry points to the same repo agree
    on isolation (#6022).  Explicit body values always win.

    Profile-aware on purpose: the WebUI serves multiple profiles from one
    process, and a user with ``worktree: true`` in one profile but not another
    expects per-profile behavior.  ``get_config_for_profile_home`` handles the
    ambient/common case via the mtime-tracked cache and reads a diverging
    profile's config.yaml directly off disk (see #3294).
    """
    try:
        if profile:
            from api.profiles import get_hermes_home_for_profile

            cfg_dict = get_config_for_profile_home(get_hermes_home_for_profile(profile))
        else:
            cfg_dict = get_config_for_profile_home(None)
        # Strict boolean: only a real YAML `true` opts in.  Any other shape
        # ("true", 1, [], {}, null, ...) is malformed for this key and must
        # fall to the safe no-worktree default rather than truthiness-coerce
        # into minting worktrees.
        return (cfg_dict or {}).get("worktree", False) is True
    except Exception:
        # Config resolution must never break session creation.
        logger.warning("failed to read worktree config default", exc_info=True)
        return False


def _session_model_state_from_request(
    model: str | None,
    requested_provider: str | None,
    current_provider: str | None = None,
) -> tuple[str | None, str | None]:
    model_value = str(model).strip() if model is not None else None
    provider = (
        _clean_session_model_provider(requested_provider)
        if requested_provider is not None
        else None
    )
    if model_value:
        _bare, explicit_provider = _split_provider_qualified_model(model_value)
        if explicit_provider:
            provider = explicit_provider
        elif requested_provider is None:
            provider = _clean_session_model_provider(current_provider)
        model_value, provider, _changed = _resolve_compatible_session_model_state(
            model_value,
            provider,
        )
    return model_value, provider


__routes_exports__ = (
    "_starts_token",
    "_normalize_provider_id",
    "_catalog_provider_id_sets",
    "_catalog_has_provider",
    "_model_matches_active_provider_family",
    "_catalog_model_id_matches",
    "_catalog_group_owns_exact_model",
    "_repair_foreign_session_model_provider",
    "_clean_session_model_provider",
    "_split_provider_qualified_model",
    "_model_matches_configured_default",
    "_ContextLengthLookupInputs",
    "_positive_context_length",
    "_model_lookup_candidates",
    "_models_config_context_length",
    "_canonical_context_provider",
    "_custom_provider_slug_for_context",
    "_providers_match_for_context",
    "_custom_provider_api_key_for_context",
    "_context_length_config_api_key_for_provider",
    "_context_length_lookup_inputs_for_model",
    "_should_attach_codex_provider_context",
    "_read_profile_model_config",
    "_PROFILE_CONFIG_CACHE",
    "_PROFILE_CONFIG_CACHE_TTL_SECONDS",
    "_PROFILE_CONFIG_CACHE_LOCK",
    "_read_profile_config_cached",
    "_load_profile_config_dict",
    "_ordered_custom_provider_model_ids",
    "_repair_bare_custom_provider_model",
    "_moa_fast_path_model_state",
    "_resolve_compatible_session_model_state",
    "_resolve_compatible_session_model",
    "_normalize_session_model_in_place",
    "_resolve_effective_session_model_for_display",
    "_resolve_effective_session_model_provider_for_display",
    "_resolve_context_length_for_session_model",
    "_session_context_length_lookup_state",
    "_session_model_identity_matches",
    "_should_accept_session_context_length_refresh",
    "_rescale_threshold_tokens_for_context_window",
    "_worktree_default_from_config",
    "_session_model_state_from_request",
)
