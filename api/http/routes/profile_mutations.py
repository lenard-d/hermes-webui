"""Cohesive HTTP route group used by the transport composition root."""

from __future__ import annotations

from api.http.context import RouteContext, UNHANDLED


def handle_post(handler, parsed, body, diag, ctx: RouteContext):
    _clear_session_list_cache = ctx["_clear_session_list_cache"]
    _onboarding_gate_allows = ctx["_onboarding_gate_allows"]
    _sanitize_error = ctx["_sanitize_error"]
    _security_headers = ctx["_security_headers"]
    apply_onboarding_setup = ctx["apply_onboarding_setup"]
    bad = ctx["bad"]
    cancel_onboarding_oauth_flow = ctx["cancel_onboarding_oauth_flow"]
    complete_onboarding = ctx["complete_onboarding"]
    j = ctx["j"]
    json = ctx["json"]
    os = ctx["os"]
    persisted_speech_settings_keys = ctx["persisted_speech_settings_keys"]
    probe_provider_endpoint = ctx["probe_provider_endpoint"]
    save_settings = ctx["save_settings"]
    start_onboarding_oauth_flow = ctx["start_onboarding_oauth_flow"]

    if parsed.path == "/api/profile/create":
        name = body.get("name", "").strip()
        if not name:
            return bad(handler, "name is required")
        import re as _re

        if not _re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", name):
            return bad(
                handler,
                "Invalid profile name: lowercase letters, numbers, hyphens, underscores only",
            )
        clone_from = body.get("clone_from")
        if clone_from is not None:
            clone_from = str(clone_from).strip()
            if not _re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", clone_from):
                return bad(handler, "Invalid clone_from name")
        base_url = body.get("base_url", "").strip() if body.get("base_url") else None
        api_key = body.get("api_key", "").strip() if body.get("api_key") else None
        default_model = (
            body.get("default_model", "").strip() if body.get("default_model") else None
        )
        model_provider = (
            body.get("model_provider", "").strip()
            if body.get("model_provider")
            else None
        )
        if base_url and not base_url.startswith(("http://", "https://")):
            return bad(handler, "base_url must start with http:// or https://")
        try:
            from api.profiles import create_profile_api

            result = create_profile_api(
                name,
                clone_from=clone_from,
                clone_config=bool(body.get("clone_config", False)),
                base_url=base_url,
                api_key=api_key,
                default_model=default_model,
                model_provider=model_provider,
            )
            return j(handler, {"ok": True, "profile": result})
        except PermissionError as e:
            return bad(handler, _sanitize_error(e), 403)
        except (ValueError, FileExistsError, RuntimeError) as e:
            return bad(handler, str(e))

    if parsed.path == "/api/profile/delete":
        name = body.get("name", "").strip()
        if not name:
            return bad(handler, "name is required")
        try:
            from api.profiles import delete_profile_api, validate_profile_name

            validate_profile_name(name)
            result = delete_profile_api(name)
            return j(handler, result)
        except PermissionError as e:
            return bad(handler, _sanitize_error(e), 403)
        except (ValueError, FileNotFoundError) as e:
            return bad(handler, _sanitize_error(e))
        except RuntimeError as e:
            return bad(handler, str(e), 409)

    # ── Settings (POST) ──
    if parsed.path == "/api/settings":
        from api.auth import (
            create_session,
            get_password_hash,
            is_auth_enabled,
            parse_cookie,
            set_auth_cookie,
            verify_password,
            verify_session,
        )

        if "bot_name" in body:
            body["bot_name"] = (str(body["bot_name"]) or "").strip() or "Hermes"

        auth_enabled_before = is_auth_enabled()
        password_auth_enabled_before = (
            auth_enabled_before and get_password_hash() is not None
        )
        current_cookie = parse_cookie(handler)
        logged_in_before = bool(current_cookie and verify_session(current_cookie))
        requested_password = bool(
            isinstance(body.get("_set_password"), str)
            and body.get("_set_password", "").strip()
        )
        requested_passwordless = bool(body.pop("_passwordless", False))
        requested_clear_password = bool(
            body.get("_clear_password") or requested_passwordless
        )
        if requested_passwordless:
            body["_clear_password"] = True

        current_password = body.pop("_current_password", None)

        # #1560: HERMES_WEBUI_PASSWORD env var takes precedence in
        # api.auth.get_password_hash(), so writing password_hash to settings.json
        # has no effect on auth. Refuse loudly with 409 instead of silently
        # succeeding — the previous behaviour returned 200 + a green save toast
        # while every subsequent login still required the env-var password.
        if requested_password or requested_clear_password:
            if os.getenv("HERMES_WEBUI_PASSWORD", "").strip():
                return bad(
                    handler,
                    "HERMES_WEBUI_PASSWORD env var is set — it overrides the settings password. "
                    "Unset the env var and restart the server before changing the password here.",
                    409,
                )

        max_tokens_provided = "max_tokens" in body
        max_tokens_status = None
        max_tokens_value = body.pop("max_tokens", None) if max_tokens_provided else None

        # First password creation decides who owns a previously passwordless
        # WebUI. While auth is disabled, the generic /api/settings route is also
        # unauthenticated, so gate bootstrap password setup the same way as
        # onboarding setup: local/private networks only, unless the operator
        # explicitly opts into remote bootstrap with HERMES_WEBUI_ONBOARDING_OPEN.
        if requested_password and not auth_enabled_before:
            if not _onboarding_gate_allows(handler, auth_enabled_before):
                return bad(
                    handler,
                    "First password setup is only available from local networks when auth is not enabled. "
                    "To bootstrap this on a remote server, set HERMES_WEBUI_ONBOARDING_OPEN=1.",
                    403,
                )

        # Auth-disable safety: when password auth is currently enabled, require
        # the current password to change, clear, or switch to passwordless.
        if (
            auth_enabled_before
            and password_auth_enabled_before
            and (requested_password or requested_clear_password)
        ):
            if not isinstance(current_password, str) or not current_password:
                return bad(
                    handler,
                    "Current password is required to change or disable authentication.",
                    403,
                )
            if not verify_password(current_password):
                return bad(
                    handler,
                    "Current password is incorrect.",
                    403,
                )

        if requested_passwordless:
            from api.auth import passkey_feature_enabled
            from api.auth import registered_credentials

            if not passkey_feature_enabled():
                return bad(
                    handler,
                    "Passkey support is disabled. Enable HERMES_WEBUI_PASSKEY before going passwordless.",
                    409,
                )
            if not registered_credentials():
                return bad(
                    handler, "Register a passkey before going passwordless.", 409
                )
        elif requested_clear_password:
            from api.auth import clear_credentials

            clear_credentials()

        # Handle auth_disabled_acknowledged setting
        ack = body.pop("_auth_disabled_acknowledged", None)
        if ack is not None and not is_auth_enabled():
            body["auth_disabled_acknowledged"] = bool(ack)
        elif is_auth_enabled() or requested_password:
            body["auth_disabled_acknowledged"] = False

        from api.config import get_max_tokens_status, set_max_tokens

        saved = save_settings(body)
        saved["persisted_speech_keys"] = persisted_speech_settings_keys()
        if max_tokens_provided:
            max_tokens_status = set_max_tokens(max_tokens_value)
        saved.pop("password_hash", None)  # never expose hash to client
        saved.update(
            max_tokens_status if max_tokens_provided else get_max_tokens_status()
        )

        # Settings that change which sessions appear in the sidebar must
        # invalidate the session-list cache directly. Relying on the cache's
        # settings-file mtime stamp is fragile: a toggle that writes the
        # settings file within the same mtime granularity as a cached entry (and
        # produces the default-valued key, e.g. show_cli_sessions back to its
        # True default) can leave a stale row set served for up to the cache TTL.
        # This is the root cause of the intermittent gateway_sync test flake
        # (a freshly-inserted CLI/gateway session occasionally absent from
        # /api/sessions right after the visibility toggle). Invalidate explicitly.
        if any(
            k in body
            for k in (
                "show_cli_sessions",
                "show_claude_code_sessions",
                "show_cron_sessions",
                "show_webhook_sessions",
                "show_previous_messaging_sessions",
            )
        ):
            try:
                _clear_session_list_cache()
            except Exception:
                pass
            try:
                from api.sessions import clear_cli_sessions_cache

                clear_cli_sessions_cache()
            except Exception:
                pass

        auth_enabled_after = is_auth_enabled()
        auth_just_enabled = bool(
            requested_password and auth_enabled_after and not auth_enabled_before
        )
        logged_in_after = logged_in_before
        new_cookie = None

        if auth_just_enabled and not logged_in_before:
            new_cookie = create_session()
            logged_in_after = True

        saved["auth_enabled"] = auth_enabled_after
        saved["password_auth_enabled"] = get_password_hash() is not None
        saved["logged_in"] = logged_in_after
        saved["auth_just_enabled"] = auth_just_enabled
        try:
            from api.auth import passkey_feature_enabled as _pffe
            from api.auth import registered_credentials as _rc

            if _pffe():
                saved["passkeys_enabled"] = bool(_rc())
                saved["passwordless_enabled"] = (
                    bool(_rc()) and not saved["password_auth_enabled"]
                )
            else:
                saved["passkeys_enabled"] = False
                saved["passwordless_enabled"] = False
        except Exception:
            pass

        if not new_cookie:
            return j(handler, saved)

        response_body = json.dumps(saved, ensure_ascii=False, indent=2).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(response_body)))
        handler.send_header("Cache-Control", "no-store")
        set_auth_cookie(handler, new_cookie)
        _security_headers(handler)
        handler.end_headers()
        handler.wfile.write(response_body)
        return True

    if parsed.path == "/api/onboarding/oauth/start":
        if not _onboarding_gate_allows(handler):
            return bad(
                handler,
                "Onboarding OAuth is only available from local networks when auth is not enabled. To bypass this on a remote server, set HERMES_WEBUI_ONBOARDING_OPEN=1.",
                403,
            )
        try:
            return j(
                handler,
                start_onboarding_oauth_flow(body),
                extra_headers={"Cache-Control": "no-store"},
            )
        except ValueError as e:
            return bad(handler, str(e))
        except RuntimeError as e:
            return bad(handler, str(e), 500)

    if parsed.path == "/api/onboarding/oauth/cancel":
        try:
            return j(
                handler,
                cancel_onboarding_oauth_flow(body),
                extra_headers={"Cache-Control": "no-store"},
            )
        except ValueError as e:
            return bad(handler, str(e))

    if parsed.path == "/api/onboarding/setup":
        # Writing API keys to disk - restrict to local/private networks unless auth is active.
        # In Docker, requests arrive from the bridge network (172.x.x.x), not 127.0.0.1,
        # even when the user accesses via localhost:8787 on the host.
        # Behind a reverse proxy (nginx/Caddy/Traefik) or SSH tunnel, X-Forwarded-For
        # carries the real origin IP — read it first before falling back to the raw socket addr.
        # HERMES_WEBUI_ONBOARDING_OPEN=1 lets operators on remote servers explicitly bypass
        # the check when they control network access themselves (e.g. firewall + VPN).
        if not _onboarding_gate_allows(handler):
            return bad(
                handler,
                "Onboarding setup is only available from local networks when auth is not enabled. To bypass this on a remote server, set HERMES_WEBUI_ONBOARDING_OPEN=1.",
                403,
            )
        try:
            return j(handler, apply_onboarding_setup(body))
        except ValueError as e:
            return bad(handler, str(e))
        except RuntimeError as e:
            return bad(handler, str(e), 500)

    if parsed.path == "/api/onboarding/complete":
        # Marking onboarding complete flips the first-run wizard off (persists
        # onboarding_completed=True). Gate it on the same local-network check as
        # the other onboarding mutators so an unauthenticated public client on a
        # passwordless bind can't hide the first-run wizard. (#3765)
        if not _onboarding_gate_allows(handler):
            return bad(
                handler,
                "Onboarding is only available from local networks when auth is not enabled. To bypass this on a remote server, set HERMES_WEBUI_ONBOARDING_OPEN=1.",
                403,
            )
        return j(handler, complete_onboarding())

    if parsed.path == "/api/onboarding/probe":
        # Probe a self-hosted provider endpoint (#1499).  Validates the
        # configured base URL is reachable + parses /models, returns the
        # model catalog so the wizard can populate its dropdown.
        # Read-only: no config.yaml or .env writes happen here.  Same local-
        # network gate as /api/onboarding/setup (also writing-adjacent in
        # spirit because it carries an api_key the user typed).
        if not _onboarding_gate_allows(handler):
            return bad(
                handler,
                "Onboarding probe is only available from local networks when auth is not enabled. To bypass this on a remote server, set HERMES_WEBUI_ONBOARDING_OPEN=1.",
                403,
            )
        provider = str((body or {}).get("provider") or "").strip().lower()
        base_url = str((body or {}).get("base_url") or "")
        api_key = str((body or {}).get("api_key") or "").strip() or None
        try:
            return j(handler, probe_provider_endpoint(provider, base_url, api_key))
        except Exception as e:
            return bad(handler, f"probe failed: {e}", 500)
    return UNHANDLED
