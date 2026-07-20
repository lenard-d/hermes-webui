"""Compatibility resolution for persisted session model and provider state."""

from __future__ import annotations

from api.config import (
    DEFAULT_MODEL,
    _provider_is_known_or_configured,
    get_available_models,
)
from api.model_context import (
    _clean_session_model_provider,
    _split_provider_qualified_model,
)
from api.sessions.custom_model_identity import _repair_bare_custom_provider_model
from api.sessions.model_identity import (
    _catalog_has_provider,
    _catalog_provider_id_sets,
    _model_matches_active_provider_family,
    _normalize_provider_id,
    _should_attach_codex_provider_context,
)


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

            providers_cfg = (
                _active_cfg.get("providers") if isinstance(_active_cfg, dict) else {}
            )
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
            requested_provider == "openai-codex" and model_prefix == "openai"
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
    bare_model, explicit_provider = (
        _split_provider_qualified_model(model) if model else ("", None)
    )
    if profile_provider and not explicit_provider:
        _profile_provider_normalized = _normalize_provider_id(profile_provider)
        _profile_default = str(profile_default_model or "").strip()
        if not model:
            _fallback = _profile_default or default_model
            return _fallback, profile_provider, bool(_fallback)

        model_prefix = model.split("/", 1)[0].strip().lower() if "/" in model else ""
        model_provider_from_name = (
            _normalize_provider_id(model_prefix) if "/" in model else ""
        )

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
        if "/" in model and _profile_provider_normalized in {
            "openrouter",
            "custom",
            "",
        }:
            return model, profile_provider, False

        if (
            "/" in model
            and model_provider_from_name
            and model_provider_from_name != _profile_provider_normalized
        ):
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
                if _should_attach_codex_provider_context(
                    bare_model, raw_active_provider, catalog
                )
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
                if _should_attach_codex_provider_context(
                    default_model, raw_active_provider, catalog
                )
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
                if (
                    model_provider
                    and model_provider != active_provider
                    and default_model
                ):
                    provider_context = (
                        raw_active_provider
                        if _should_attach_codex_provider_context(
                            default_model, raw_active_provider, catalog
                        )
                        else None
                    )
                    return default_model, provider_context, True
                provider_context = (
                    raw_active_provider
                    if _should_attach_codex_provider_context(
                        model, raw_active_provider, catalog
                    )
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
    if (
        model_provider
        and model_provider not in {"", "custom", "openrouter"}
        and model_provider != _active_for_compare
        and default_model
    ):
        return default_model, requested_provider, True
    return model, requested_provider, False
