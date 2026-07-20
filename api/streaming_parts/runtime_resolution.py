"""Persistent-state, profile/provider runtime resolution, and lifecycle status helpers."""

from __future__ import annotations

from types import ModuleType


def file_signature(api: ModuleType, path) -> tuple[int, int] | None:
    try:
        st = path.stat()
        return (int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return None


def persistent_state_snapshot(api: ModuleType, profile_home: str | None) -> dict:
    """Capture lightweight memory/skill file signatures for save toasts."""
    if not profile_home:
        return {"memory": {}, "skills": {}}
    root = api.Path(profile_home)
    memory = {}
    for key, parts in api._PERSISTENT_MEMORY_FILES:
        sig = api._file_signature(root.joinpath(*parts))
        if sig is not None:
            memory[key] = sig
    skills = {}
    skills_dir = root / "skills"
    try:
        for skill_md in skills_dir.rglob("SKILL.md"):
            try:
                rel = str(skill_md.relative_to(skills_dir)).replace("\\", "/")
            except ValueError:
                rel = str(skill_md)
            sig = api._file_signature(skill_md)
            if sig is not None:
                skills[rel] = sig
    except OSError:
        pass
    return {"memory": memory, "skills": skills}


def persistent_state_changes(api: ModuleType, before: dict | None, after: dict | None) -> dict:
    before = before or {"memory": {}, "skills": {}}
    after = after or {"memory": {}, "skills": {}}
    memory_before = before.get("memory") or {}
    memory_after = after.get("memory") or {}
    skills_before = before.get("skills") or {}
    skills_after = after.get("skills") or {}
    memory_changed = any(memory_before.get(key) != sig for key, sig in memory_after.items())
    skills = []
    for rel, sig in skills_after.items():
        old_sig = skills_before.get(rel)
        if old_sig == sig:
            continue
        name = api.Path(rel).parent.name or api.Path(rel).stem
        skills.append({
            "name": name,
            "path": rel,
            "action": "created" if old_sig is None else "updated",
        })
    return {"memory_saved": memory_changed, "skills": skills[:10]}


def apply_profile_provider_context_to_streaming_model(
    api: ModuleType,
    model: str | None,
    provider_context: str | None,
    profile_provider: str | None,
    profile_default_model: str | None,
) -> tuple[str | None, str | None, bool]:
    """Attach profile provider context and repair stale cross-provider models."""
    if provider_context or not profile_provider:
        return model, provider_context, False

    provider_context = profile_provider.lower()
    if not profile_default_model:
        return model, provider_context, False

    from api.routes import _normalize_provider_id

    profile_provider_normalized = _normalize_provider_id(profile_provider)
    model_lower = (model or "").lower()
    # Only run the bare-prefix family match on un-namespaced model ids. A custom
    # namespace like "gemini_cli/..." or "claude-relay/..." merely *starts with* a
    # first-party token; matching it here would clobber the model to the profile
    # default on the send path (the #4278 collision — the slash-qualified branch
    # below routes through the fixed _normalize_provider_id instead).
    if "/" not in model_lower:
        for prefix in ("gpt", "claude", "gemini"):
            if model_lower.startswith(prefix):
                if _normalize_provider_id(prefix) != profile_provider_normalized:
                    return profile_default_model, provider_context, True
                return model, provider_context, False

    if "/" in model_lower:
        slash_prefix = model_lower.split("/", 1)[0]
        if provider_context == "openai-codex" and slash_prefix == "openai":
            return profile_default_model, provider_context, True

        slash_provider = _normalize_provider_id(slash_prefix)
        if (
            slash_provider
            and slash_provider != profile_provider_normalized
            and profile_provider_normalized not in {"openrouter", "custom", ""}
        ):
            return profile_default_model, provider_context, True

    return model, provider_context, False


def apply_profile_home_context_to_streaming_model(
    api: ModuleType,
    model: str | None,
    provider_context: str | None,
    profile_home: str | None,
    has_profile: bool,
) -> tuple[str | None, str | None, bool]:
    """Apply profile provider/model context from a profile config if present."""
    if not (profile_home and has_profile and not provider_context):
        return model, provider_context, False

    try:
        import yaml as _yaml_pp

        _pp_cfg_path = api.Path(profile_home) / "config.yaml"
        if not _pp_cfg_path.is_file():
            return model, provider_context, False

        _pp_cfg = _yaml_pp.safe_load(_pp_cfg_path.read_text(encoding="utf-8")) or {}
        if not isinstance(_pp_cfg, dict):
            return model, provider_context, False

        _pp = (_pp_cfg.get("model", {}).get("provider") or "").strip()
        if not _pp:
            return model, provider_context, False

        _pp_default = (_pp_cfg.get("model", {}).get("default") or "").strip()
        return api._apply_profile_provider_context_to_streaming_model(
            model,
            provider_context,
            _pp,
            _pp_default,
        )
    except Exception:
        api.logger.warning("profile provider read failed", exc_info=True)
        return model, provider_context, False


def resolve_custom_provider_runtime_overrides(
    api: ModuleType,
    resolved_provider: str | None,
    resolved_api_key: str | None,
    resolved_base_url: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Return provider/key/base_url overrides for ``custom:*`` endpoints.

    Hermes Agent treats named custom providers as routing hints around an
    OpenAI-compatible base URL.  Local OpenAI-compatible servers often run
    without authentication, so a missing key should not fail before the first
    request; pass a harmless placeholder to the SDK and let the endpoint accept
    it or return its own auth error.
    """
    if not (isinstance(resolved_provider, str) and resolved_provider.startswith("custom:")):
        return resolved_provider, resolved_api_key, resolved_base_url

    _cp_key, _cp_base = api.resolve_custom_provider_connection(resolved_provider)
    if not resolved_api_key and _cp_key:
        resolved_api_key = _cp_key
    if not resolved_base_url and _cp_base:
        resolved_base_url = _cp_base
    if resolved_base_url:
        # Route through the generic custom OpenAI-compatible client once the
        # named provider has supplied the concrete endpoint. Keeping the
        # provider as custom:<slug> would make Agent init synthesize invalid
        # env-var hints like CUSTOM:SOMETHING-8000_API_KEY on keyless setups.
        resolved_provider = "custom"
        if not resolved_api_key:
            resolved_api_key = api._KEYLESS_CUSTOM_API_KEY
    return resolved_provider, resolved_api_key, resolved_base_url


def same_base_url_endpoint(api: ModuleType, url_a: str, url_b: str) -> bool:
    """True if two base URLs point at the same scheme+host+port endpoint.

    Used to decide whether a runtime base_url is just a normalized form of the
    configured one (e.g. OpenCode-Go's ``/v1`` de-duplication on the same host)
    versus a genuinely different endpoint (an explicit ``providers.<id>.base_url``
    override at a different host/port that must be preserved). Path/query are
    intentionally ignored — the normalization #3895 fixes is path-only.
    """
    from urllib.parse import urlsplit
    try:
        a = urlsplit((url_a or "").strip())
        b = urlsplit((url_b or "").strip())
    except Exception:
        return False
    _default_port = {"http": 80, "https": 443}
    a_host = (a.hostname or "").lower()
    b_host = (b.hostname or "").lower()
    a_scheme = (a.scheme or "").lower()
    b_scheme = (b.scheme or "").lower()
    a_port = a.port or _default_port.get(a_scheme)
    b_port = b.port or _default_port.get(b_scheme)
    return bool(a_host) and a_host == b_host and a_scheme == b_scheme and a_port == b_port


def runtime_preferred_base_url(
    api: ModuleType,
    runtime_provider: dict | None,
    resolved_provider: str | None,
    configured_base_url: str | None,
) -> str | None:
    """Prefer the runtime-normalized base_url, but never override an explicit
    configured endpoint that points somewhere genuinely different.

    The #3895 bug was that WebUI used the *configured* base_url (which can carry a
    duplicated ``/v1``) instead of the runtime provider's per-model-normalized
    base_url, 404ing OpenCode-Go. But blindly preferring the runtime URL would
    clobber a legitimate ``providers.<id>.base_url`` override (e.g. LM Studio at a
    LAN IP, an OpenRouter mirror). So:
      - no runtime URL            -> keep configured
      - no configured URL         -> use runtime (all we have)
      - named ``custom:`` endpoint -> configured wins (then runtime as fallback)
      - same scheme+host+port     -> runtime wins (it's the normalized/corrected
                                      form of the same endpoint — the #3895 case)
      - different endpoint        -> configured override wins (no regression)
    """
    runtime_base_url = None
    if isinstance(runtime_provider, dict):
        runtime_base_url = runtime_provider.get("base_url")
    if not runtime_base_url:
        return configured_base_url
    if not configured_base_url:
        return runtime_base_url

    provider_id = str(
        resolved_provider
        or (runtime_provider or {}).get("provider")
        or ""
    ).strip().lower()
    if provider_id.startswith("custom:"):
        return configured_base_url or runtime_base_url

    # An explicit configured override at a DIFFERENT endpoint must be preserved;
    # only prefer the runtime URL when it's the same endpoint (path-normalized).
    if api._same_base_url_endpoint(configured_base_url, runtime_base_url):
        return runtime_base_url
    return configured_base_url


def is_fallback_lifecycle_message(api: ModuleType, kind: str, message: str) -> bool:
    """Return True if an agent lifecycle status should surface as a fallback warning."""
    k = str(kind or '').strip().lower()
    m = str(message or '').strip().lower()
    return (
        k == 'lifecycle'
        and (
            'rate limited' in m
            or 'switching to fallback' in m
            or 'falling back' in m
            or 'fallback activated' in m
            or 'trying fallback' in m
        )
    )


def is_agent_compression_start_status(api: ModuleType, kind: str, message: str) -> bool:
    """Return True only for real Hermes context-compression start notices.

    WebUI bridges matching lifecycle statuses into an SSE ``compressing`` event
    and paints the live "Compressing context" worklog divider. The previous
    matcher used broad substrings such as ``'compressing' in message`` and
    ``'preflight compression' in message``, which can false-positive on skip /
    cooldown / unrelated notices and make brand-new low-token turns look like
    auto-compression.

    Positive markers below match the agent emitters in hermes-agent
    (``turn_context`` preflight, ``conversation_loop`` pre-API / 413 / too-large,
    ``conversation_compression`` compaction status). Explicitly reject skip /
    defer notices so "Skipping preflight compression…" never surfaces as a
    running compress divider.
    """
    k = str(kind or '').strip().lower()
    m = str(message or '').strip().lower()
    if k != 'lifecycle' or not m:
        return False
    # Skip / cooldown / defer logs must never look like a live compression start.
    if (
        'skipping' in m
        or 'defer' in m
        or 'cooldown' in m
        or 'will not start' in m
    ):
        return False
    # Post-compress retry chatter is not a start event.
    if 'compressed' in m and 'compressing' not in m and 'compression attempt' not in m:
        return False
    return (
        'preflight compression:' in m
        or 'pre-api compression:' in m
        or 'compacting context' in m
        or 'context too large' in m
        or '— compressing (' in m
        or '- compressing (' in m
        or 'compression attempt' in m
    )
