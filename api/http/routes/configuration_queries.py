"""Cohesive HTTP route group used by the transport composition root."""

from __future__ import annotations

from api.http.context import RouteContext, UNHANDLED
from api.http.plugins import handle_plugins


def handle_get(handler, parsed, ctx: RouteContext):
    RequestDiagnostics = ctx["RequestDiagnostics"]
    _handle_live_models = ctx["_handle_live_models"]
    _sanitize_error = ctx["_sanitize_error"]
    _serve_static = ctx["_serve_static"]
    bad = ctx["bad"]
    get_available_models = ctx["get_available_models"]
    get_available_models_for_session_visit = ctx[
        "get_available_models_for_session_visit"
    ]
    get_onboarding_status = ctx["get_onboarding_status"]
    get_provider_cost_history = ctx["get_provider_cost_history"]
    get_provider_quota = ctx["get_provider_quota"]
    get_providers = ctx["get_providers"]
    get_reasoning_status = ctx["get_reasoning_status"]
    get_session = ctx["get_session"]
    handle_transcribe_capability = ctx["handle_transcribe_capability"]
    j = ctx["j"]
    load_settings = ctx["load_settings"]
    logger = ctx["logger"]
    os = ctx["os"]
    parse_qs = ctx["parse_qs"]
    persisted_speech_settings_keys = ctx["persisted_speech_settings_keys"]

    if parsed.path == "/api/models":
        # Profile-scoping for non-default profiles (#3957) is handled INSIDE
        # get_available_models() — it binds the active profile's env + TLS on
        # the detached rebuild worker (and the legacy synchronous rebuild),
        # which the request-thread wrapper could not reach. See
        # api.config.get_available_models cold path + profile_scope_for_detached_worker.
        freshness = (
            parse_qs(parsed.query or "").get("freshness", [""])[0].strip().lower()
        )
        diag = RequestDiagnostics.maybe_start(
            "GET",
            parsed.path,
            logger=logger,
            print_fn=getattr(handler, "_safe_webui_print", None),
        )
        try:
            diag.stage(f"enter:freshness={freshness or 'default'}") if diag else None
            if freshness == "session_visit":
                result = get_available_models_for_session_visit()
                diag.stage("response_serialize") if diag else None
                return j(handler, result)
            if freshness:
                return bad(
                    handler, f"unknown models freshness: {freshness}", status=400
                )
            return j(handler, get_available_models())
        finally:
            if diag:
                diag.finish()

    if parsed.path == "/api/models/live":
        from api.profiles import profile_env_for_active_request

        with profile_env_for_active_request("/api/models/live", logger_override=logger):
            return _handle_live_models(handler, parsed)

    # ── Auxiliary models (GET/POST) ──
    if parsed.path == "/api/model/auxiliary":
        from api.config import get_auxiliary_models

        return j(handler, get_auxiliary_models())

    if parsed.path == "/api/dashboard/status":
        from api import dashboard_probe

        j(handler, dashboard_probe.get_dashboard_status())
        return True

    if parsed.path == "/api/dashboard/config":
        from api import dashboard_probe

        try:
            j(handler, dashboard_probe.get_dashboard_config())
        except ValueError as exc:
            bad(handler, str(exc), status=400)
        return True

    # ── Providers (GET) ──
    if parsed.path == "/api/providers":
        # Apply the active per-request profile's env so provider auth probes
        # resolve against that profile's credentials, not the process-default
        # profile's (#3957). Without this, get_auth_status() probes on a
        # non-default profile resolve the wrong/empty creds and can stall past
        # the 30s frontend timeout. No-op for the default profile.
        from api.profiles import profile_env_for_active_request_readonly

        with profile_env_for_active_request_readonly(
            "/api/providers", logger_override=logger
        ):
            return j(handler, get_providers())

    # ── Plugins/hooks visibility (read-only, no callback/source internals) ──
    if parsed.path == "/api/plugins":
        return handle_plugins(handler, parsed)
    if parsed.path == "/api/provider/quota":
        query = parse_qs(parsed.query)
        provider_id = query.get("provider", [""])[0] or None
        refresh = (query.get("refresh", [""])[0] or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        # Bind the active request's profile env (matches /api/providers and
        # /api/models/live). #4365 added a credential_pool.load_pool() path in
        # get_provider_quota for all pooled providers; without this wrapper that
        # read/write runs under the process-default profile, so a multi-profile
        # client would see (and seed) the default profile's pool instead of its
        # own (#4247/#4067 profile-isolation class).
        from api.profiles import profile_env_for_active_request_readonly

        with profile_env_for_active_request_readonly(
            "/api/provider/quota", logger_override=logger
        ):
            return j(handler, get_provider_quota(provider_id, refresh=refresh))

    if parsed.path == "/api/provider/cost-history":
        query = parse_qs(parsed.query)
        provider_id = query.get("provider", [""])[0] or None
        days_raw = (query.get("days", ["7"])[0] or "7").strip()
        try:
            days = max(1, min(int(days_raw), 365))
        except (ValueError, TypeError):
            days = 7
        return j(handler, get_provider_cost_history(provider_id, days))

    if parsed.path == "/api/settings":
        settings = load_settings()
        settings["persisted_speech_keys"] = persisted_speech_settings_keys()
        # Never expose the stored password hash to clients
        settings.pop("password_hash", None)
        settings.setdefault("max_tokens", None)
        settings.setdefault("max_tokens_effective", None)
        settings.setdefault("max_tokens_fallback", None)
        try:
            from api.config import get_max_tokens_status

            settings.update(get_max_tokens_status())
        except Exception:
            settings["max_tokens"] = None
            settings["max_tokens_effective"] = None
            settings["max_tokens_fallback"] = None
        # Surface env-var precedence so the UI can disable the password field
        # instead of silently no-oping the save (#1560). The setting takes
        # precedence in api.auth.get_password_hash(), but until now the UI
        # had no way to know — see issue #1139 / #1560.
        settings["password_env_var"] = bool(
            os.getenv("HERMES_WEBUI_PASSWORD", "").strip()
        )
        # Auth-state fields for frontend safety badge / confirmation flows
        from api.auth import get_password_hash, is_auth_enabled

        settings["auth_enabled"] = is_auth_enabled()
        settings["password_auth_enabled"] = get_password_hash() is not None
        try:
            from api.auth import passkey_feature_enabled as _pffe
            from api.auth import registered_credentials as _rc

            if _pffe():
                settings["passkeys_enabled"] = bool(_rc())
                settings["passwordless_enabled"] = (
                    bool(_rc()) and not settings["password_auth_enabled"]
                )
            else:
                settings["passkeys_enabled"] = False
                settings["passwordless_enabled"] = False
        except Exception:
            pass
        # Inject the running version so the UI badge stays in sync with git tags
        # without any manual release step.
        try:
            from api.updates import AGENT_VERSION, WEBUI_VERSION

            settings["webui_version"] = WEBUI_VERSION
            settings["agent_version"] = AGENT_VERSION
        except Exception:
            pass
        # Channel-scoped display badge — SEPARATE from webui_version (which is
        # load-bearing for asset cache-busting / SW cache / skew detection and
        # must stay channel-neutral). update_channel_version is display-only.
        try:
            from api.updates import channel_version_badge, read_update_channel

            channel = read_update_channel()
            settings["update_channel"] = channel
            settings["update_channel_version"] = channel_version_badge(channel)
        except Exception:
            pass
        return j(handler, settings)

    if parsed.path == "/api/transcribe/capability":
        return handle_transcribe_capability(handler)

    if parsed.path == "/api/reasoning":
        # Current reasoning config (shared source of truth with the CLI —
        # reads display.show_reasoning and agent.reasoning_effort from
        # the active profile's config.yaml).
        query = parse_qs(parsed.query)
        model_id = (query.get("model", [""])[0] or "").strip() or None
        provider_id = (query.get("provider", [""])[0] or "").strip() or None
        base_url = (query.get("base_url", [""])[0] or "").strip() or None
        return j(
            handler,
            get_reasoning_status(
                model_id=model_id,
                provider_id=provider_id,
                base_url=base_url,
            ),
        )

    if parsed.path == "/api/onboarding/status":
        return j(handler, get_onboarding_status())

    if parsed.path == "/api/extensions/status":
        from api.extensions import get_extension_status

        return j(handler, get_extension_status())

    if parsed.path == "/api/extensions/registry":
        from api.extensions import get_extension_registry

        return j(handler, get_extension_registry())

    if parsed.path.startswith("/extensions/"):
        from api.extensions import serve_extension_static

        return serve_extension_static(handler, parsed)

    if parsed.path.startswith("/static/"):
        return _serve_static(handler, parsed)

    if parsed.path == "/api/session/worktree/status":
        query = parse_qs(parsed.query)
        sid = query.get("session_id", [""])[0]
        if not sid:
            return bad(handler, "session_id is required", status=400)
        try:
            s = get_session(sid, metadata_only=True)
        except KeyError:
            return bad(handler, "Session not found", status=404)
        try:
            from api.worktrees import worktree_status_for_session

            return j(handler, {"status": worktree_status_for_session(s)})
        except ValueError as exc:
            return bad(handler, str(exc), status=400)
        except Exception as exc:
            logger.exception("failed to read worktree status for session %s", sid)
            return bad(handler, _sanitize_error(exc), status=500)
    return UNHANDLED
