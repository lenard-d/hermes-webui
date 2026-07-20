"""Authentication policy and request authorization.

This module owns which authentication mechanisms enable the gate, trusted-header
identity reconciliation, profile-cookie authorization, and HTTP gate decisions.
Credential storage and cookie cryptography remain in cookies_password.
"""
from __future__ import annotations

import json
import logging
import os
import re

from api.config import get_config
from .cookies_password import (
    _auth_cookie_header,
    _COOKIE_NAME_RE,
    create_session,
    get_session_info,
    invalidate_session,
    parse_cookie,
    sign_profile_cookie_value,
    verify_profile_cookie_value,
    verify_session,
    is_password_auth_enabled,
)

logger = logging.getLogger(__name__)

PUBLIC_PATHS = frozenset({
    '/login', '/health', '/favicon.ico', '/sw.js',
    '/api/auth/login', '/api/auth/status',
    '/api/auth/oidc/start', '/api/auth/oidc/callback',
    '/api/auth/passkey/options', '/api/auth/passkey/login',
    '/share',
    '/manifest.json', '/manifest.webmanifest',
    '/session/manifest.json', '/session/manifest.webmanifest',
})


_TRUSTED_AUTH_HEADER_ENV = 'HERMES_WEBUI_TRUSTED_AUTH_HEADER'
_TRUSTED_GROUPS_HEADER_ENV = 'HERMES_WEBUI_TRUSTED_GROUPS_HEADER'
_TRUSTED_GROUP_PROFILE_MAP_ENV = 'HERMES_WEBUI_GROUP_PROFILE_MAP'
_TRUSTED_AUTH_LOGOUT_URL_ENV = 'HERMES_WEBUI_TRUSTED_AUTH_LOGOUT_URL'
_TRUSTED_AUTH_WARNINGS_EMITTED: set[str] = set()


def _warn_trusted_auth_once(key: str, message: str, *args) -> None:
    if key in _TRUSTED_AUTH_WARNINGS_EMITTED:
        return
    _TRUSTED_AUTH_WARNINGS_EMITTED.add(key)
    logger.warning(message, *args)


def _passkey_feature_flag_enabled() -> bool:
    """Return True if the passkey/WebAuthn surface is enabled for this deployment.

    Passkey support is opt-in default-off behind a feature flag so deployments
    that don't want the WebAuthn surface (or whose RP-ID setup isn't ready for
    non-localhost hosts) can disable it entirely with no UI surface, no
    endpoints, no credential storage. To enable:

      - Set ``HERMES_WEBUI_PASSKEY=1`` in the environment, OR
      - Set ``webui_passkey_enabled: true`` in the per-profile config.yaml

    With the flag off, ``are_passkeys_enabled()`` always returns False even if
    credentials were registered in the past, and ``/login`` shows password-only.
    """
    env_value = os.getenv("HERMES_WEBUI_PASSKEY", "")
    if env_value:
        return env_value.strip().lower() in {"1", "true", "yes", "on"}
    try:
        from api.config import get_config

        cfg = get_config()
        if isinstance(cfg, dict):
            raw = cfg.get("webui_passkey_enabled")
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str):
                return raw.strip().lower() in {"1", "true", "yes", "on"}
    except Exception:
        pass
    return False


def are_passkeys_enabled() -> bool:
    """True if the passkey feature flag is on AND at least one local passkey credential is registered."""
    if not _passkey_feature_flag_enabled():
        return False
    try:
        from .passkeys import passkeys_available

        return passkeys_available()
    except Exception as exc:
        logger.debug("Failed to inspect passkey availability: %s", exc)
        return False


def is_oidc_auth_enabled() -> bool:
    """True if native OIDC login is configured for WebUI sessions."""
    try:
        from .oidc import is_oidc_enabled

        return is_oidc_enabled()
    except Exception as exc:
        logger.debug("Failed to inspect OIDC availability: %s", exc)
        return False


def get_oidc_startup_warning() -> str | None:
    """Return a startup warning when OIDC auth is only partially configured."""
    try:
        cfg = get_config()
        raw = cfg.get("webui_oidc") if isinstance(cfg, dict) else {}
        if not isinstance(raw, dict):
            raw = {}
    except Exception:
        logger.debug("Failed to read webui_oidc config", exc_info=True)
        raw = {}

    def pick(name: str, env_name: str) -> str:
        env_value = os.getenv(env_name)
        value = env_value if env_value is not None else raw.get(name)
        return str(value or "").strip()

    issuer = bool(pick("issuer", "HERMES_WEBUI_OIDC_ISSUER"))
    client_id = bool(pick("client_id", "HERMES_WEBUI_OIDC_CLIENT_ID"))
    allow_claim = bool(pick("allow_claim", "HERMES_WEBUI_OIDC_ALLOW_CLAIM"))
    allow_values = bool(pick("allow_values", "HERMES_WEBUI_OIDC_ALLOW_VALUES"))

    if not any((issuer, client_id, allow_claim, allow_values)):
        return None
    if issuer and client_id and allow_claim and allow_values:
        return None

    missing = []
    if not issuer:
        missing.append("issuer")
    if not client_id:
        missing.append("client_id")
    if not allow_claim:
        missing.append("allow_claim")
    if not allow_values:
        missing.append("allow_values")

    joined = ", ".join(missing)
    return (
        "Native OIDC login is only partially configured; missing "
        f"{joined}. The WebUI will not enable OIDC auth until all four fields are set."
    )


def is_auth_enabled() -> bool:
    """True if password auth, passkeys, OIDC login, or trusted-header auth is configured."""
    return (
        is_password_auth_enabled()
        or are_passkeys_enabled()
        or is_oidc_auth_enabled()
        or is_trusted_auth_enabled()
    )

PROFILE_COOKIE_NAME = 'hermes_profile'
_PROFILE_COOKIE_ENV = 'HERMES_WEBUI_PROFILE_COOKIE_NAME'
_LEGACY_PROFILE_COOKIE_ENV = 'WEBUI_PROFILE_COOKIE_NAME'
_legacy_profile_cookie_warned = False


def get_profile_cookie_name() -> str:
    """Return the cookie name used to persist the active WebUI profile.

    Honours ``HERMES_WEBUI_PROFILE_COOKIE_NAME`` so multiple WebUI instances
    sharing a hostname (different ports) can use distinct profile-cookie names
    instead of trampling each other; browsers scope cookies by host, not
    host+port (RFC 6265). The original ``WEBUI_PROFILE_COOKIE_NAME`` is still
    honoured as a deprecated fallback (warned once per process, since this is
    called on every request).
    """
    name = os.getenv(_PROFILE_COOKIE_ENV, '').strip()
    if name:
        return name
    legacy = os.getenv(_LEGACY_PROFILE_COOKIE_ENV, '').strip()
    if legacy:
        global _legacy_profile_cookie_warned
        if not _legacy_profile_cookie_warned:
            logger.warning(
                '%s is deprecated; use %s instead.',
                _LEGACY_PROFILE_COOKIE_ENV,
                _PROFILE_COOKIE_ENV,
            )
            _legacy_profile_cookie_warned = True
        return legacy
    return PROFILE_COOKIE_NAME


def get_profile_cookie(handler) -> str | None:
    """Extract and authenticate the active-profile cookie value.

    When WebUI auth is enabled, the profile cookie is treated as an
    authorization input for profile-scoped routes. Require it to be signed for
    the current auth session so clients cannot forge ``hermes_profile`` to
    impersonate another profile. In no-auth deployments, keep the historical
    plain profile-name cookie behavior.
    """
    cookie_header = handler.headers.get('Cookie', '')
    if not cookie_header:
        return None
    import http.cookies as _hc
    cookie = _hc.SimpleCookie()
    try:
        cookie.load(cookie_header)
    except _hc.CookieError:
        return None
    cookie_name = get_profile_cookie_name()
    morsel = cookie.get(cookie_name)
    if not (morsel and morsel.value):
        return None

    from api.profiles import _PROFILE_ID_RE

    def _valid_profile_name(val: str) -> bool:
        return val == 'default' or bool(_PROFILE_ID_RE.fullmatch(val))

    raw_val = morsel.value
    try:
        if is_auth_enabled():
            val = verify_profile_cookie_value(raw_val, parse_cookie(handler))
            return val if val and _valid_profile_name(val) else None
    except Exception:
        logger.warning("Failed to verify active profile cookie", exc_info=True)
        return None

    # No-auth mode: the cookie is a per-browser UI preference, not an authz
    # boundary, so retain the legacy plain profile-name format.
    return raw_val if _valid_profile_name(raw_val) else None


def build_profile_cookie(name: str, handler=None, *, session_cookie_value: str | None = None) -> str:
    """Build a Set-Cookie header value for the active-profile cookie.

    Always persist the selected profile in the cookie, including 'default'.
    Clearing the cookie causes the backend to fall back to process-global
    _active_profile, which can unexpectedly switch clients back to another
    profile.

    Set HttpOnly because the UI reads the active profile from
    /api/profile/active JSON and does not need to access this cookie via
    document.cookie.
    """
    import http.cookies as _hc
    cookie = _hc.SimpleCookie()
    cookie_name = get_profile_cookie_name()
    value = name
    # Guard against a future call site silently emitting an UNSIGNED profile
    # cookie while auth is enabled (which a client could then... not forge, but
    # it would weaken the binding). If auth is on we require a handler so the
    # cookie is bound to the session. (#4023 Opus hardening.)
    try:
        _auth_on = is_auth_enabled()
    except Exception:
        _auth_on = False
    if _auth_on and handler is None:
        if session_cookie_value is None:
            raise RuntimeError("build_profile_cookie requires a request handler when auth is enabled (to bind the profile cookie to the session)")
    if session_cookie_value is not None:
        try:
            value = sign_profile_cookie_value(name, session_cookie_value)
        except Exception as exc:
            logger.warning("Failed to sign active profile cookie", exc_info=True)
            raise RuntimeError("could not sign active profile cookie") from exc
    elif handler is not None:
        try:
            if is_auth_enabled():
                value = sign_profile_cookie_value(name, parse_cookie(handler))
        except Exception as exc:
            logger.warning("Failed to sign active profile cookie", exc_info=True)
            raise RuntimeError("could not sign active profile cookie") from exc
    cookie[cookie_name] = value
    cookie[cookie_name]['path'] = '/'
    cookie[cookie_name]['httponly'] = True
    cookie[cookie_name]['samesite'] = 'Lax'
    return cookie[cookie_name].OutputString()


def clear_profile_cookie(handler) -> None:
    import http.cookies as _hc

    cookie = _hc.SimpleCookie()
    cookie_name = get_profile_cookie_name()
    cookie[cookie_name] = ''
    cookie[cookie_name]['path'] = '/'
    cookie[cookie_name]['httponly'] = True
    cookie[cookie_name]['samesite'] = 'Lax'
    cookie[cookie_name]['max-age'] = '0'
    handler.send_header('Set-Cookie', cookie[cookie_name].OutputString())

def _request_client_ip(handler) -> str:
    try:
        address = getattr(handler, "client_address", None)
        if address:
            return str(address[0] or "")
    except Exception:
        pass
    return ""


def _ip_is_loopback_or_private(raw: str):
    """Parse an IP string; return (parsed_ok, is_loopback_or_private).

    Returns (False, False) for empty/malformed input so callers fail closed.
    """
    import ipaddress

    raw = (raw or "").strip()
    if not raw:
        return (False, False)
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return (False, False)
    return (True, bool(addr.is_loopback or addr.is_private))


def _trusted_proxy_networks():
    """Networks whose socket peer is allowed to assert a forwarded client IP.

    Loopback is ALWAYS trusted implicitly (the common same-host reverse-proxy
    deployment). Operators fronting the WebUI with a LAN/remote proxy add its
    address(es) via HERMES_WEBUI_TRUSTED_PROXY_CIDRS (comma-separated CIDRs or
    bare IPs). Malformed entries are skipped, never widening trust.
    """
    import ipaddress

    nets = [
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("::1/128"),
        ipaddress.ip_network("::ffff:127.0.0.0/104"),
    ]
    raw = os.getenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", "") or ""
    for token in raw.replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            nets.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            # Invalid CIDR/IP → skip (fail closed: never widens trust).
            continue
    return nets


def _ip_in_networks(addr, networks) -> bool:
    """Family-aware membership test.

    Checks the parsed address against each network, and — for an IPv4-mapped
    IPv6 address (e.g. ``::ffff:10.9.9.9``) — ALSO checks its embedded IPv4 form
    against IPv4 networks. Without this, a mapped-IPv6 proxy peer would never
    match an IPv4 CIDR allowlist: the trusted proxy would be treated as
    untrusted (locking out legitimate clients behind it) and, inside an XFF
    chain, a mapped trusted hop would be mis-returned as the client (admitting a
    public client that preceded it). See #5764.
    """
    candidates = [addr]
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        candidates.append(mapped)
    for cand in candidates:
        for net in networks:
            try:
                if cand in net:
                    return True
            except TypeError:
                # IPv4/IPv6 family mismatch between candidate and net → skip.
                continue
    return False


def _raw_peer_is_trusted_proxy(handler) -> bool:
    """True when the immediate socket peer is loopback or an allowlisted proxy.

    Only such a peer is allowed to assert a forwarded client IP. Judged on the
    RAW socket address (never a header), so it cannot be spoofed.
    """
    import ipaddress

    raw = _request_client_ip(handler)
    if not raw:
        return False
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return False
    return _ip_in_networks(addr, _trusted_proxy_networks())

def _trusted_auth_header_name() -> str | None:
    name = os.getenv(_TRUSTED_AUTH_HEADER_ENV, '').strip()
    if not name:
        return None
    if not _COOKIE_NAME_RE.match(name):
        _warn_trusted_auth_once(
            'trusted-auth-header',
            'Ignoring invalid %s=%r; trusted-header auth rejects every request',
            _TRUSTED_AUTH_HEADER_ENV,
            name,
        )
        return None
    return name


def _trusted_auth_header_configured() -> bool:
    return bool(os.getenv(_TRUSTED_AUTH_HEADER_ENV, '').strip())


def _trusted_group_profile_map() -> dict[str, str] | None:
    raw = os.getenv(_TRUSTED_GROUP_PROFILE_MAP_ENV, '').strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        _warn_trusted_auth_once(
            'trusted-group-map',
            'Ignoring invalid %s JSON; trusted-header auth falls back to default profile binding',
            _TRUSTED_GROUP_PROFILE_MAP_ENV,
        )
        return {}
    if not isinstance(data, dict):
        _warn_trusted_auth_once(
            'trusted-group-map-type',
            'Ignoring non-dict %s; trusted-header auth falls back to default profile binding',
            _TRUSTED_GROUP_PROFILE_MAP_ENV,
        )
        return {}
    mapping: dict[str, str] = {}
    for group, profile in data.items():
        group_name = str(group or '').strip()
        profile_name = str(profile or '').strip()
        if not group_name or not profile_name:
            _warn_trusted_auth_once(
                'trusted-group-map-entry',
                'Ignoring invalid entry in %s; trusted-header auth falls back to default profile binding',
                _TRUSTED_GROUP_PROFILE_MAP_ENV,
            )
            continue
        mapping[group_name] = profile_name
    return mapping


def _trusted_groups_header_value(handler) -> list[str]:
    header_name = os.getenv(_TRUSTED_GROUPS_HEADER_ENV, '').strip()
    if not header_name:
        return []
    try:
        raw = handler.headers.get(header_name, '')
    except Exception:
        return []
    if not raw:
        return []
    values = []
    for part in str(raw).replace('\n', ',').split(','):
        part = part.strip()
        if part:
            values.append(part)
    return values


def _trusted_auth_username(handler) -> str | None:
    header_name = _trusted_auth_header_name()
    if not header_name:
        return None
    try:
        raw = handler.headers.get(header_name, '')
    except Exception:
        return None
    username = str(raw or '').strip()
    return username or None


def _trusted_auth_bound_profile(handler) -> str | None:
    mapping = _trusted_group_profile_map()
    if mapping is None:
        return None
    groups = set(_trusted_groups_header_value(handler))
    for group, profile in mapping.items():
        if group in groups:
            return profile
    return 'default'


def _queue_pending_cookie(handler, cookie_header: str) -> None:
    if not cookie_header:
        return
    pending = getattr(handler, '_pending_set_cookies', None)
    if pending is None:
        pending = []
        handler._pending_set_cookies = pending
    pending.append(cookie_header)


def _build_profile_cookie_header(name: str, session_cookie_value: str | None) -> str:
    return build_profile_cookie(name, session_cookie_value=session_cookie_value)


def _request_profile_matches_bound(bound_profile: str | None) -> bool:
    if not bound_profile:
        return True
    try:
        from api.profiles import get_active_profile_name, _profiles_match

        return _profiles_match(bound_profile, get_active_profile_name())
    except Exception:
        return False


def is_trusted_auth_enabled() -> bool:
    return _trusted_auth_header_configured()


def get_trusted_auth_logout_url() -> str | None:
    value = os.getenv(_TRUSTED_AUTH_LOGOUT_URL_ENV, '').strip()
    return value or None


def _remember_trusted_auth_session(handler, info: dict | None, cookie_value: str | None = None) -> dict | None:
    handler._trusted_auth_session_reconciled = info
    if info and info.get('auth_type') == 'trusted':
        handler._trusted_auth_session_info = info
        handler._trusted_auth_session_cookie_value = cookie_value
    return info


def reset_trusted_auth_request_state(handler) -> None:
    for name in (
        '_trusted_auth_session_reconciled',
        '_trusted_auth_session_rejected',
        '_trusted_auth_session_info',
        '_trusted_auth_session_cookie_value',
        # Clear any auth cookie queued by a prior request but not yet flushed.
        # The handler is reused across HTTP/1.1 keep-alive requests, so a stale
        # queued Set-Cookie would otherwise cross the request boundary and be
        # emitted by a later response — e.g. after trusted-identity rotation on
        # logout it could overwrite a subsequent valid login cookie and 401 the
        # user. Reset it at the per-request boundary (server.py do_GET/do_POST).
        '_pending_set_cookies',
    ):
        try:
            delattr(handler, name)
        except AttributeError:
            pass


def _apply_trusted_session_profile(handler, bound_profile: str | None, cookie_value: str) -> None:
    if bound_profile is None:
        return
    from api.profiles import set_request_profile

    set_request_profile(bound_profile)
    if get_profile_cookie(handler) != bound_profile:
        _queue_pending_cookie(handler, _build_profile_cookie_header(bound_profile, cookie_value))


def ensure_trusted_auth_session(handler) -> dict | None:
    if hasattr(handler, '_trusted_auth_session_reconciled'):
        return handler._trusted_auth_session_reconciled
    cookie_value = parse_cookie(handler)
    info = get_session_info(cookie_value) if cookie_value and verify_session(cookie_value) else None
    if info and info.get('auth_type') != 'trusted':
        return _remember_trusted_auth_session(handler, info)
    if not is_trusted_auth_enabled():
        if info:
            invalidate_session(cookie_value)
            handler._trusted_auth_session_rejected = True
        return _remember_trusted_auth_session(handler, None)
    if not _raw_peer_is_trusted_proxy(handler):
        if info:
            invalidate_session(cookie_value)
            handler._trusted_auth_session_rejected = True
        return _remember_trusted_auth_session(handler, None)
    username = _trusted_auth_username(handler)
    if not username:
        if info:
            invalidate_session(cookie_value)
            handler._trusted_auth_session_rejected = True
        return _remember_trusted_auth_session(handler, None)
    bound_profile = _trusted_auth_bound_profile(handler)
    if info and info.get('username') == username and info.get('bound_profile') == bound_profile:
        _apply_trusted_session_profile(handler, bound_profile, cookie_value)
        return _remember_trusted_auth_session(handler, info, cookie_value)
    if info:
        invalidate_session(cookie_value)
    cookie_value = create_session(
        auth_type='trusted',
        username=username,
        bound_profile=bound_profile,
    )
    _queue_pending_cookie(handler, _auth_cookie_header(cookie_value, handler))
    _apply_trusted_session_profile(handler, bound_profile, cookie_value)
    info = get_session_info(cookie_value)
    return _remember_trusted_auth_session(handler, info, cookie_value)


def trusted_session_allows_active_profile(info: dict | None) -> bool:
    if not info:
        return True
    return _request_profile_matches_bound(str(info.get('bound_profile') or '') or None)

def _safe_login_inner_next(query: str | None) -> str:
    """#5578: extract a SAFE, non-login inner redirect from a login page's query.

    When an expired-auth bounce lands back on the login page (which already
    carries its own `next` in the query), we want to preserve a legitimate inner
    destination X across the redirect to the real login route — but only if X is
    itself safe (path-absolute, not protocol-relative/backslash, no control
    chars) AND not login-shaped / not itself carrying a nested next param.
    Anything else collapses to '' (no inner redirect), which kills the
    self-referential chain. Mirrors _safe_login_redirect_path().
    """
    import urllib.parse as _u
    raw = _u.parse_qs(query or "").get("next", [""])[0]
    path = str(raw or "").strip()
    if not path or path[0] != "/" or path[1:2] in {"/", "\\"}:
        return ""
    if re.search(r"[\x00-\x1f\x7f\s]", path) or len(path) > 2048:
        return ""
    # Collapse only login-route chains — decode a few levels so a nested
    # `/session/login%3Fnext%3D...` (encoded `?`) is still recognized by its
    # leading PATH — but preserve a legitimate non-login inner path that merely
    # carries its own `next=` query key (e.g. `/admin?next=/real/path`).
    _probe = path
    for _ in range(8):
        _p = _probe.split("?", 1)[0].split("#", 1)[0].split("&", 1)[0].rstrip("/")
        if _p == "/login" or _p.endswith("/login"):
            return ""
        _decoded = _u.unquote(_probe)
        if _decoded == _probe:
            break
        _probe = _decoded
    else:
        # Still decoding at the cap (pathologically deep encoding) → fail closed.
        _p = _probe.split("?", 1)[0].split("#", 1)[0].split("&", 1)[0].rstrip("/")
        if _p == "/login" or _p.endswith("/login"):
            return ""
        return ""
    return path


def check_auth(handler, parsed) -> bool:
    """Check if request is authorized. Returns True if OK.
    If not authorized, sends 401 (API) or 302 redirect (page) and returns False."""
    if not is_auth_enabled():
        return True
    # Public paths don't require auth
    if (
        parsed.path in PUBLIC_PATHS
        or parsed.path.startswith('/share/')
        or (
            parsed.path.startswith('/api/share/')
            and parsed.path not in {'/api/share/create', '/api/share/revoke'}
        )
        or parsed.path.startswith('/static/')
        or parsed.path.startswith('/session/static/')
    ):
        return True
    cookie_val = parse_cookie(handler)
    has_session = bool(cookie_val and verify_session(cookie_val))
    if parsed.path == '/api/auth/logout':
        if has_session:
            return True
        body = b'{"error":"Authentication required"}'
        handler.send_response(401)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        return False
    session_info = ensure_trusted_auth_session(handler)
    if session_info:
        if not trusted_session_allows_active_profile(session_info):
            if parsed.path.startswith('/api/'):
                body = b'{"error":"Profile access forbidden"}'
                handler.send_response(403)
                handler.send_header('Content-Type', 'application/json')
            else:
                body = b'Profile access forbidden'
                handler.send_response(403)
                handler.send_header('Content-Type', 'text/plain; charset=utf-8')
            handler.send_header('Content-Length', str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            return False
        return True
    # Not authorized
    if parsed.path.startswith('/api/'):
        body = b'{"error":"Authentication required"}'
        handler.send_response(401)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    else:
        handler.send_response(302)
        # Pass the original path as ?next= so login.js redirects back after auth.
        # SECURITY/CORRECTNESS: the inner `?` and `&` MUST be percent-encoded
        # when stuffed into the outer `?next=` parameter, otherwise:
        #   (a) multi-param query strings get truncated at the first inner `&`
        #       (e.g. `/api/sessions?limit=50&offset=0` would round-trip as
        #       just `/api/sessions?limit=50` after the browser parses the
        #       outer URL — `offset=0` becomes a separate top-level query
        #       parameter that the login page ignores).
        #   (b) attacker-controlled paths could inject a second `next=`
        #       parameter; per RFC 3986 the duplicate behaviour is undefined
        #       and parsers diverge (Python's parse_qs returns last-match,
        #       URLSearchParams returns first-match), opening a query-pollution
        #       footgun even though _safeNextPath() rejects most malicious
        #       shapes downstream.
        # Encoding the entire `path?query` blob with quote(safe='/') turns
        # `?` → `%3F` and `&` → `%26`, so the outer parameter holds exactly
        # one path-with-query string and `searchParams.get('next')` returns
        # the full original URL (the browser auto-decodes once).
        # (Opus pre-release advisor finding for v0.50.258.)
        import urllib.parse as _urlparse
        # #5578: if the page being redirected is ALREADY login-shaped, do NOT
        # wrap its full `path?query` into a fresh `next=` — that query already
        # carries a `next=`, so quoting the whole thing nests the login URL into
        # itself and re-encodes it on every expired-auth bounce, exploding the
        # URL until the tab breaks. This guard runs in check_auth() (BEFORE
        # route handling), the actual source of the server-side loop.
        #
        # The login page is served ONLY at the public `/login` route (see
        # PUBLIC_PATHS + the routes.py `/login` handler); the app's client route
        # `/session/login` is NOT public, so a bare relative `login` from
        # `/session/login` resolves to `/session/login` again and re-triggers
        # check_auth() — an infinite redirect. Resolve to the real login route
        # with `../login`, which lands on `/login` from a `/session/*` scope and
        # on `<mount>/login` under a subpath mount (verified via urljoin). Carry
        # through only a validated, non-login inner `next` so a legitimate
        # post-login destination still survives a bounce that happened to land
        # on the login page.
        _login_path = (parsed.path or '/').rstrip('/')
        if _login_path == '/login' or _login_path.endswith('/login'):
            # /login itself is public → check_auth never redirects it; this only
            # fires for the non-public client login route (e.g. /session/login).
            _target = '../login' if '/' in _login_path.lstrip('/') else 'login'
            _inner = _safe_login_inner_next(parsed.query)
            if _inner:
                _target += '?next=' + _urlparse.quote(_inner, safe='/')
            handler.send_header('Location', _target)
            handler.send_header('Content-Length', '0')
            handler.end_headers()
            return False
        _path_with_query = parsed.path or '/'
        if parsed.query:
            _path_with_query += '?' + parsed.query
        # safe='/' keeps path separators readable; everything else (including
        # `?`, `&`, `=`) gets percent-encoded.
        _next = _urlparse.quote(_path_with_query, safe='/')
        handler.send_header('Location', 'login?next=' + _next)
        handler.send_header('Content-Length', '0')
        handler.end_headers()
    return False
