"""Cohesive HTTP route group used by the transport composition root."""

from __future__ import annotations

from api.http.context import RouteContext, UNHANDLED
from api.http.shell import render_index_shell_base, serve_manifest, serve_unavailable


def handle_get(handler, parsed, ctx: RouteContext):
    Path = ctx["Path"]
    _LOGIN_LOCALE = ctx["_LOGIN_LOCALE"]
    _LOGIN_PAGE_HTML = ctx["_LOGIN_PAGE_HTML"]
    __file__ = ctx["__file__"]
    _html = ctx["_html"]
    _oidc_login_html = ctx["_oidc_login_html"]
    _request_base_url = ctx["_request_base_url"]
    _resolve_login_locale_key = ctx["_resolve_login_locale_key"]
    _safe_login_redirect_path = ctx["_safe_login_redirect_path"]
    _security_headers = ctx["_security_headers"]
    _serve_static = ctx["_serve_static"]
    api_config = ctx["api_config"]
    bad = ctx["bad"]
    j = ctx["j"]
    json = ctx["json"]
    load_settings = ctx["load_settings"]
    load_share = ctx["load_share"]
    parse_qs = ctx["parse_qs"]
    t = ctx["t"]

    if parsed.path.startswith("/session/static/"):
        # Strip the leading "/session" so _serve_static() sees a path that
        # starts with "/static/" (its required prefix). _serve_static enforces
        # its own path-traversal sandbox via Path.resolve()+relative_to().
        stripped = parsed._replace(path=parsed.path[len("/session") :])
        return _serve_static(handler, stripped)

    # Firefox Android resolves <link rel="manifest"> against the page URL
    # before the dynamic <base href> script runs when installing from
    # /session/<id>, producing requests like /session/manifest.json.
    # Without this guard the catch-all below returns index.html instead of
    # the manifest, and Firefox falls back to a generated letter icon.
    # See #2226.
    if parsed.path in ("/session/manifest.json", "/session/manifest.webmanifest"):
        return serve_manifest(handler)

    if parsed.path in ("/", "/index.html") or parsed.path.startswith("/session/"):
        try:
            from api.extensions import inject_extension_tags

            csrf_token = ""
            try:
                from api.auth import (
                    csrf_token_for_session,
                    is_auth_enabled,
                    parse_cookie,
                    verify_session,
                )

                if is_auth_enabled():
                    cookie_val = parse_cookie(handler)
                    if not cookie_val:
                        cookie_val = getattr(
                            handler, "_trusted_auth_session_cookie_value", None
                        )
                    if cookie_val and verify_session(cookie_val):
                        csrf_token = csrf_token_for_session(cookie_val) or ""
            except Exception:
                csrf_token = ""

            # The disk read + process-constant token substitutions are cached;
            # only the per-session CSRF token and per-request extension tags are
            # applied here (see _render_index_shell_base).
            html = render_index_shell_base().replace(
                "__CSRF_TOKEN_JSON__", json.dumps(csrf_token)
            )
            return t(
                handler,
                inject_extension_tags(html),
                content_type="text/html; charset=utf-8",
            )
        except Exception as exc:
            return serve_unavailable(handler, exc)

    if parsed.path == "/share" or parsed.path.startswith("/share/"):
        share_path = (Path(__file__).parent.parent / "static" / "share.html").resolve()
        return t(
            handler,
            share_path.read_text(encoding="utf-8"),
            content_type="text/html; charset=utf-8",
            extra_headers={
                "X-Robots-Tag": "noindex, nofollow",
            },
        )

    if parsed.path == "/login":
        _settings = load_settings()
        _bn = _html.escape(_settings.get("bot_name") or "Hermes")
        _lang = _settings.get("language", "en")
        _login_strings = _LOGIN_LOCALE[_resolve_login_locale_key(_lang)]
        from urllib.parse import quote
        from api.updates import WEBUI_VERSION

        version_token = quote(WEBUI_VERSION, safe="")
        _page = (
            _LOGIN_PAGE_HTML.replace("{{BOT_NAME}}", _bn)
            .replace("{{BOT_NAME_INITIAL}}", _bn[0].upper())
            .replace("{{WEBUI_VERSION}}", version_token)
            .replace("{{LANG}}", _html.escape(_login_strings["lang"]))
            .replace("{{LOGIN_TITLE}}", _html.escape(_login_strings["title"]))
            .replace("{{LOGIN_SUBTITLE}}", _html.escape(_login_strings["subtitle"]))
            .replace(
                "{{LOGIN_PLACEHOLDER}}", _html.escape(_login_strings["placeholder"])
            )
            .replace("{{LOGIN_BTN}}", _html.escape(_login_strings["btn"]))
            .replace("{{LOGIN_INVALID_PW}}", _html.escape(_login_strings["invalid_pw"]))
            .replace(
                "{{LOGIN_CONN_FAILED}}", _html.escape(_login_strings["conn_failed"])
            )
            .replace("{{OIDC_LOGIN_HTML}}", _oidc_login_html(parsed))
        )
        return t(handler, _page, content_type="text/html; charset=utf-8")

    if parsed.path == "/api/auth/oidc/start":
        from api.auth import (
            OIDCAuthError,
            OIDCConfigError,
            build_authorization_redirect,
        )

        next_path = _safe_login_redirect_path(
            parse_qs(parsed.query or "").get("next", [""])[0]
        )
        try:
            location = build_authorization_redirect(
                _request_base_url(handler), next_path
            )
        except OIDCConfigError as exc:
            return j(handler, {"error": str(exc)}, status=404)
        except OIDCAuthError as exc:
            return j(handler, {"error": str(exc)}, status=exc.status_code)
        handler.send_response(302)
        handler.send_header("Location", location)
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", "0")
        _security_headers(handler)
        handler.end_headers()
        return True

    if parsed.path == "/api/auth/oidc/callback":
        from api.auth import create_session, set_auth_cookie
        from api.auth import (
            OIDCAuthError,
            OIDCConfigError,
            complete_authorization_code_flow,
        )

        query = parse_qs(parsed.query or "")
        error = str(query.get("error", [""])[0] or "").strip()
        if error:
            description = str(query.get("error_description", [""])[0] or "").strip()
            return j(handler, {"error": description or error}, status=401)
        state = str(query.get("state", [""])[0] or "").strip()
        code = str(query.get("code", [""])[0] or "").strip()
        if not state or not code:
            return j(
                handler, {"error": "Missing OIDC callback state or code"}, status=400
            )
        try:
            result = complete_authorization_code_flow(
                _request_base_url(handler), state, code
            )
        except OIDCConfigError as exc:
            return j(handler, {"error": str(exc)}, status=404)
        except OIDCAuthError as exc:
            return j(handler, {"error": str(exc)}, status=exc.status_code)
        cookie_val = create_session()
        handler.send_response(302)
        handler.send_header(
            "Location",
            _safe_login_redirect_path(result.get("next_path")),
        )
        handler.send_header("Cache-Control", "no-store")
        _security_headers(handler)
        set_auth_cookie(handler, cookie_val)
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return True

    if parsed.path == "/api/auth/status":
        from api.auth import (
            passkey_feature_enabled,
            ensure_trusted_auth_session,
            get_password_hash,
            is_auth_enabled,
            is_oidc_auth_enabled,
            is_trusted_auth_enabled,
        )
        from api.auth import registered_credentials

        logged_in = False
        session_info = None
        auth_enabled = is_auth_enabled()
        oidc_enabled = is_oidc_auth_enabled()
        if auth_enabled:
            session_info = ensure_trusted_auth_session(handler)
            logged_in = bool(session_info)
        passkey_flag = passkey_feature_enabled()
        passkeys = registered_credentials() if passkey_flag else []
        password_auth_enabled = get_password_hash() is not None
        payload = {
            "auth_enabled": auth_enabled,
            "logged_in": logged_in,
            "oidc_enabled": oidc_enabled,
            "password_auth_enabled": password_auth_enabled,
            "passwordless_enabled": bool(passkeys) and not password_auth_enabled,
            "passkeys_enabled": bool(passkeys),
            "passkeys_count": len(passkeys),
            "passkey_feature_flag": passkey_flag,
            "auth_disabled_acknowledged": bool(
                load_settings().get("auth_disabled_acknowledged")
            )
            if not auth_enabled
            else False,
        }
        if is_trusted_auth_enabled() or (
            session_info and session_info.get("auth_type") == "trusted"
        ):
            payload["trusted_auth_enabled"] = True
        if session_info and session_info.get("auth_type") == "trusted":
            payload["auth_type"] = session_info.get("auth_type")
            payload["user"] = session_info.get("username")
            payload["bound_profile"] = session_info.get("bound_profile")
        return j(handler, payload)

    if parsed.path.startswith("/api/share/"):
        token = parsed.path[len("/api/share/") :].strip()
        share = load_share(token)
        if not share:
            return bad(handler, "Shared conversation not found", 404)
        return j(
            handler,
            {"share": share},
            extra_headers={
                "Cache-Control": "no-store",
                "X-Robots-Tag": "noindex, nofollow",
            },
        )

    if parsed.path in ("/manifest.json", "/manifest.webmanifest"):
        return serve_manifest(handler)

    if parsed.path == "/sw.js":
        static_root = api_config.get_static_root()
        sw_path = (static_root / "sw.js").resolve()
        if sw_path.exists():
            # Inject the current git-derived version as the cache name so the
            # service worker cache busts automatically on every new deploy.
            from urllib.parse import quote
            from api.updates import WEBUI_VERSION

            version_token = quote(WEBUI_VERSION, safe="")
            text = sw_path.read_text(encoding="utf-8").replace(
                "__WEBUI_VERSION__", version_token
            )
            data = text.encode("utf-8")
            handler.send_response(200)
            handler.send_header("Content-Type", "application/javascript; charset=utf-8")
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Service-Worker-Allowed", "/")
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
            return True
        return j(handler, {"error": "not found"}, status=404)

    if parsed.path == "/favicon.ico":
        static_root = api_config.get_static_root()
        ico_path = (static_root / "favicon.ico").resolve()
        if ico_path.exists() and ico_path.is_file():
            data = ico_path.read_bytes()
            handler.send_response(200)
            handler.send_header("Content-Type", "image/x-icon")
            handler.send_header("Content-Length", str(len(data)))
            handler.send_header("Cache-Control", "public, max-age=86400")
            handler.end_headers()
            handler.wfile.write(data)
        else:
            handler.send_response(204)
            handler.end_headers()
        return True
    return UNHANDLED
