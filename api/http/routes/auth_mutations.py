"""Cohesive HTTP route group used by the transport composition root."""

from __future__ import annotations

from api.http.context import RouteContext, UNHANDLED


def handle_post(handler, parsed, body, diag, ctx: RouteContext):
    _require_passkey_registration_auth = ctx["_require_passkey_registration_auth"]
    _security_headers = ctx["_security_headers"]
    bad = ctx["bad"]
    j = ctx["j"]
    json = ctx["json"]

    if parsed.path == "/api/auth/login":
        from api.auth import (
            verify_password,
            create_session,
            set_auth_cookie,
            is_auth_enabled,
        )
        from api.auth import (
            clear_login_attempts,
            login_rate_allowed,
            record_login_attempt,
        )

        if not is_auth_enabled():
            return j(handler, {"ok": True, "message": "Auth not enabled"})
        client_ip = handler.client_address[0]
        if not login_rate_allowed(client_ip):
            return j(
                handler,
                {"error": "Too many attempts. Try again in a minute."},
                status=429,
            )
        password = body.get("password", "")
        if not verify_password(password):
            record_login_attempt(client_ip)
            return bad(handler, "Invalid password", 401)
        clear_login_attempts(client_ip)
        cookie_val = create_session()
        body = json.dumps({"ok": True}).encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        _security_headers(handler)
        set_auth_cookie(handler, cookie_val)
        handler.end_headers()
        handler.wfile.write(body)
        return True

    if parsed.path == "/api/auth/passkey/options":
        from api.auth import is_auth_enabled, passkey_feature_enabled
        from api.auth import PasskeyError, PasskeyRateLimitError, authentication_options

        if not passkey_feature_enabled():
            return j(
                handler,
                {
                    "error": "Passkey support is disabled. Set HERMES_WEBUI_PASSKEY=1 or webui_passkey_enabled: true to enable."
                },
                status=404,
            )
        if not is_auth_enabled():
            return j(handler, {"error": "Auth not enabled"}, status=400)
        try:
            return j(
                handler, {"ok": True, "publicKey": authentication_options(handler)}
            )
        except PasskeyRateLimitError as e:
            return bad(handler, str(e), status=429)
        except PasskeyError as e:
            return bad(handler, str(e), status=400)

    if parsed.path == "/api/auth/passkey/login":
        from api.auth import (
            passkey_feature_enabled,
            create_session,
            is_auth_enabled,
            set_auth_cookie,
        )
        from api.auth import login_rate_allowed, record_login_attempt
        from api.auth import PasskeyError, finish_login

        if not passkey_feature_enabled():
            return j(handler, {"error": "Passkey support is disabled."}, status=404)
        if not is_auth_enabled():
            return j(handler, {"error": "Auth not enabled"}, status=400)
        client_ip = handler.client_address[0]
        if not login_rate_allowed(client_ip):
            return j(
                handler,
                {"error": "Too many attempts. Try again in a minute."},
                status=429,
            )
        try:
            finish_login(body, handler)
        except PasskeyError as e:
            record_login_attempt(client_ip)
            return bad(handler, str(e), status=401)
        cookie_val = create_session()
        body = json.dumps({"ok": True}).encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        _security_headers(handler)
        set_auth_cookie(handler, cookie_val)
        handler.end_headers()
        handler.wfile.write(body)
        return True

    if parsed.path == "/api/auth/passkey/register/options":
        from api.auth import passkey_feature_enabled
        from api.auth import PasskeyError, PasskeyRateLimitError, registration_options

        if not passkey_feature_enabled():
            return j(handler, {"error": "Passkey support is disabled."}, status=404)
        ok, error, status = _require_passkey_registration_auth(handler)
        if not ok:
            return j(handler, {"error": error}, status=status)
        try:
            return j(handler, {"ok": True, "publicKey": registration_options(handler)})
        except PasskeyRateLimitError as e:
            return bad(handler, str(e), status=429)
        except PasskeyError as e:
            return bad(handler, str(e), status=400)

    if parsed.path == "/api/auth/passkey/register":
        from api.auth import passkey_feature_enabled
        from api.auth import PasskeyError, finish_registration, registered_credentials

        if not passkey_feature_enabled():
            return j(handler, {"error": "Passkey support is disabled."}, status=404)
        ok, error, status = _require_passkey_registration_auth(handler)
        if not ok:
            return j(handler, {"error": error}, status=status)
        try:
            result = finish_registration(body, handler)
            result["credentials"] = registered_credentials()
            return j(handler, result)
        except PasskeyError as e:
            return bad(handler, str(e), status=400)

    if parsed.path == "/api/auth/passkey/delete":
        from api.auth import get_password_hash, passkey_feature_enabled
        from api.auth import PasskeyError, delete_credential, registered_credentials

        if not passkey_feature_enabled():
            return j(handler, {"error": "Passkey support is disabled."}, status=404)
        try:
            credential_id = str(body.get("id") or "")
            creds = registered_credentials()
            if (
                get_password_hash() is None
                and len(creds) <= 1
                and any(c.get("id") == credential_id for c in creds)
            ):
                return bad(
                    handler,
                    "Set a password or disable auth before removing the last passkey.",
                    409,
                )
            return j(handler, delete_credential(credential_id))
        except PasskeyError as e:
            return bad(handler, str(e), status=404)

    if parsed.path == "/api/auth/passkeys":
        from api.auth import passkey_feature_enabled
        from api.auth import registered_credentials

        if not passkey_feature_enabled():
            return j(handler, {"credentials": [], "disabled": True})
        return j(handler, {"credentials": registered_credentials()})

    if parsed.path == "/api/auth/logout":
        from api.auth import (
            clear_auth_cookie,
            ensure_trusted_auth_session,
            get_trusted_auth_logout_url,
            invalidate_session,
            parse_cookie,
        )
        from api.helpers import clear_profile_cookie

        session_info = ensure_trusted_auth_session(handler)
        cookie_val = getattr(
            handler, "_trusted_auth_session_cookie_value", None
        ) or parse_cookie(handler)
        if cookie_val:
            invalidate_session(cookie_val)
        payload = {"ok": True}
        if session_info and session_info.get("auth_type") == "trusted":
            logout_url = get_trusted_auth_logout_url()
            if logout_url:
                payload["trusted_logout_url"] = logout_url
        body = json.dumps(payload).encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        _security_headers(handler)
        clear_auth_cookie(handler)
        clear_profile_cookie(handler)
        handler.end_headers()
        handler.wfile.write(body)
        return True
    return UNHANDLED
