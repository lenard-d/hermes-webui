"""HTTP trust-boundary helpers for origin, proxying, client identity, and telemetry."""

from __future__ import annotations

import json
import logging
import os
import re as _re
import threading
import time
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

if TYPE_CHECKING:
    from api.helpers import MAX_BODY_BYTES, _security_headers, bad, j
    from api.routes import logger

_CSP_REPORT_LOGGER = logging.getLogger("csp_report")
_CSP_REPORT_RATE_LIMIT: dict[str, list[float]] = {}
_CSP_REPORT_RATE_LIMIT_LOCK = threading.Lock()
_CSP_REPORT_RATE_LIMIT_WINDOW_SECONDS = 60
_CSP_REPORT_RATE_LIMIT_MAX = 100
_CSP_REPORT_MAX_BODY_BYTES = 64 * 1024
_CLIENT_EVENT_LOGGER = logging.getLogger("client_event")
_CLIENT_EVENT_RATE_LIMIT: dict[str, list[float]] = {}
_CLIENT_EVENT_RATE_LIMIT_LOCK = threading.Lock()
_CLIENT_EVENT_RATE_LIMIT_WINDOW_SECONDS = 60
_CLIENT_EVENT_RATE_LIMIT_MAX = 30
_CLIENT_EVENT_MAX_BODY_BYTES = 4 * 1024
_EXTENSION_SIDECAR_PROXY_MAX_RESPONSE_BYTES = 512 * 1024
_CLIENT_EVENT_ALLOWED_FIELDS = {
    "event": 64,
    "source": 80,
    "session_id": 128,
    "stream_id": 128,
    "visibility_state": 32,
    "url_path": 256,
    "reason": 160,
}

def _normalize_host_port(value: str) -> tuple[str, str | None]:
    """Split a host or host:port string into (hostname, port|None).
    Handles IPv6 bracket notation, e.g. [::1]:8080."""
    value = value.strip().lower()
    if not value:
        return '', None
    if value.startswith('['):
        end = value.find(']')
        if end != -1:
            host = value[1:end]
            rest = value[end + 1 :]
            if rest.startswith(':') and rest[1:].isdigit():
                return host, rest[1:]
            return host, None
    if value.count(':') == 1:
        host, port = value.rsplit(':', 1)
        if port.isdigit():
            return host, port
    return value, None


def _ports_match(origin_scheme: str, origin_port: str | None, allowed_port: str | None) -> bool:
    """Return True when two ports should be considered equivalent, scheme-aware.

    Treats an absent port as the scheme default: port 80 for http, port 443 for https.
    Port 80 is NOT treated as equivalent to 443 (different protocols = different origins).
    """
    if origin_port == allowed_port:
        return True
    # Determine the default port for the origin's scheme
    default = '443' if origin_scheme == 'https' else '80'
    if not origin_port and allowed_port == default:
        return True
    if not allowed_port and origin_port == default:
        return True
    return False


def _allowed_public_origins() -> set[str]:
    """Parse HERMES_WEBUI_ALLOWED_ORIGINS env var (comma-separated) into a set.

    Each entry must include the scheme, e.g. https://myapp.example.com:8000.
    Entries without a scheme are silently skipped and a warning is printed.
    """
    raw = os.getenv('HERMES_WEBUI_ALLOWED_ORIGINS', '')
    result = set()
    for value in raw.split(','):
        value = value.strip().rstrip('/').lower()
        if not value:
            continue
        if not (value.startswith('http://') or value.startswith('https://')):
            import sys
            print(
                f"[webui] WARNING: HERMES_WEBUI_ALLOWED_ORIGINS entry {value!r} is missing "
                f"the scheme (expected https://hostname or http://hostname). Entry ignored.",
                flush=True, file=sys.stderr,
            )
            continue
        result.add(value)
    return result


def _is_browser_unsafe_request(handler) -> bool:
    """Return True when request headers identify a browser unsafe request.

    Non-browser API clients, including the MCP bridge and curl-style scripts,
    normally send no Origin/Referer and remain compatible with the existing
    same-machine API contract. Browsers send Origin for unsafe fetch/form POSTs;
    Referer is retained for older paths and proxies.
    """
    return bool(handler.headers.get("Origin") or handler.headers.get("Referer"))


def _check_same_origin_browser_request(handler, *, require_provenance: bool = False) -> bool:
    _clear_csrf_failure_reason(handler)
    origin = handler.headers.get("Origin", "")
    referer = handler.headers.get("Referer", "")
    host = handler.headers.get("Host", "")
    sec_fetch_site = handler.headers.get("Sec-Fetch-Site", "").strip().lower()
    if not (origin or referer or sec_fetch_site):
        return not require_provenance or _set_csrf_failure_reason(handler, "origin_mismatch")
    if sec_fetch_site == "cross-site":
        return _set_csrf_failure_reason(handler, "origin_mismatch")
    target = origin or referer
    if not target:
        if sec_fetch_site == "none":
            return True
        if sec_fetch_site == "same-origin":
            return not require_provenance or _set_csrf_failure_reason(
                handler, "origin_mismatch"
            )
        return _set_csrf_failure_reason(handler, "origin_mismatch")
    m = _re.match(r"^https?://([^/]+)", target)
    if not m:
        return _set_csrf_failure_reason(handler, "origin_mismatch")
    origin_host = m.group(1)
    origin_scheme = m.group(0).split('://')[0].lower()
    origin_name, origin_port = _normalize_host_port(origin_host)
    origin_allowed = False
    origin_value = m.group(0).rstrip('/').lower()
    if origin_value in _allowed_public_origins():
        origin_allowed = True
    if not origin_allowed:
        allowed_hosts = [h.strip() for h in [host] if h.strip()]
        trust_forwarded_host = os.getenv("HERMES_WEBUI_TRUST_FORWARDED_HOST", "").strip().lower()
        if trust_forwarded_host in ("1", "true", "yes", "on"):
            allowed_hosts.extend(
                h.strip()
                for h in [
                    handler.headers.get("X-Forwarded-Host", ""),
                    handler.headers.get("X-Real-Host", ""),
                ]
                if h.strip()
            )
        for allowed in allowed_hosts:
            allowed_name, allowed_port = _normalize_host_port(allowed)
            if origin_name == allowed_name and _ports_match(origin_scheme, origin_port, allowed_port):
                origin_allowed = True
                break
    if not origin_allowed:
        return _set_csrf_failure_reason(handler, "origin_mismatch")
    return True


def apply_cors_preflight_headers(handler) -> None:
    """Emit CORS preflight headers on ``handler`` for a same-origin/allowlisted
    request; emit nothing for a disallowed origin (browser treats the header-less
    200 as a preflight denial).

    Echoes the request Origin only when it is same-origin or explicitly
    allowlisted via HERMES_WEBUI_ALLOWED_ORIGINS — the exact policy the CSRF gate
    enforces for real requests. Reuses _check_same_origin_browser_request so the
    preflight can never advertise wider access (`*`) than an actual request would
    be granted. A wildcard here would let any site read authenticated responses
    on a deployment with no password set. Kept in api/ so server.py stays a thin
    dispatcher.
    """
    origin = handler.headers.get("Origin", "").strip()
    if not origin or not _check_same_origin_browser_request(handler):
        return
    handler.send_header("Access-Control-Allow-Origin", origin)
    handler.send_header("Vary", "Origin")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")


def _csrf_exempt_path(path: str) -> bool:
    """Paths that cannot or must not carry a session CSRF token."""
    return path in {
        "/api/auth/login",
        "/api/auth/passkey/options",
        "/api/auth/passkey/login",
        "/api/csp-report",
    }


_CSRF_FAILURE_ATTR = "_hermes_csrf_failure_reason"


def _set_csrf_failure_reason(handler, reason: str) -> bool:
    try:
        setattr(handler, _CSRF_FAILURE_ATTR, reason)
    except Exception:
        pass
    return False


def _clear_csrf_failure_reason(handler) -> None:
    try:
        if hasattr(handler, _CSRF_FAILURE_ATTR):
            delattr(handler, _CSRF_FAILURE_ATTR)
    except Exception:
        pass


def _csrf_rejection_error(handler) -> str:
    reason = getattr(handler, _CSRF_FAILURE_ATTR, "")
    if reason == "origin_mismatch":
        return "Cross-origin mismatch - check reverse proxy headers"
    if reason == "token_mismatch":
        return "Session expired - reload the page"
    return "Cross-origin request rejected"


def _check_csrf(handler) -> bool:
    """Reject cross-origin or tokenless authenticated browser unsafe requests."""
    if not _check_same_origin_browser_request(handler):
        return False
    if not _is_browser_unsafe_request(handler):
        return True  # non-browser clients (curl, MCP, agent) have no Origin/Referer

    from api.auth import CSRF_HEADER_NAME, is_auth_enabled, parse_cookie, verify_csrf_token

    if not is_auth_enabled():
        return True
    cookie_val = parse_cookie(handler)
    submitted = handler.headers.get(CSRF_HEADER_NAME) or handler.headers.get("X-CSRF-Token")
    if verify_csrf_token(cookie_val or "", submitted or ""):
        return True
    return _set_csrf_failure_reason(handler, "token_mismatch")


_EXTENSION_SIDECAR_PROXY_RE = _re.compile(
    r"^/api/extensions/(?P<extension_id>[^/]+)/sidecar(?:/(?P<proxy_path>.*))?$"
)
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-connection",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _connection_bound_header_names(headers) -> set[str]:
    names = set(_HOP_BY_HOP_HEADERS)
    if not headers or not hasattr(headers, "items"):
        return names
    connection_values = []
    if hasattr(headers, "get_all"):
        connection_values.extend(headers.get_all("Connection", []))
    else:
        for name, value in headers.items():
            if str(name).lower() == "connection":
                connection_values.append(value)
    for value in connection_values:
        for token in str(value).split(","):
            normalized = token.strip().lower()
            if normalized:
                names.add(normalized)
    return names


def _match_extension_sidecar_proxy_path(path: str) -> tuple[str, str] | None:
    match = _EXTENSION_SIDECAR_PROXY_RE.match(path or "")
    if not match:
        return None
    return match.group("extension_id"), match.group("proxy_path") or ""


def _read_body_bytes(handler) -> bytes:
    raw_length = handler.headers.get("Content-Length", 0)
    try:
        length = int(raw_length)
    except (TypeError, ValueError):
        try:
            handler.close_connection = True
        except Exception:
            pass
        raise ValueError(f"Invalid Content-Length: {raw_length!r}") from None
    if length < 0:
        try:
            handler.close_connection = True
        except Exception:
            pass
        raise ValueError(f"Invalid Content-Length: {length}")
    if length > MAX_BODY_BYTES:
        try:
            handler.close_connection = True
        except Exception:
            pass
        raise ValueError(f"Request body too large ({length} bytes, max {MAX_BODY_BYTES})")
    return handler.rfile.read(length) if length else b""


def _extension_sidecar_proxy_request_headers(handler) -> dict[str, str]:
    headers = {}
    raw_headers = getattr(handler, "headers", None)
    if not raw_headers or not hasattr(raw_headers, "items"):
        return headers
    blocked_headers = _connection_bound_header_names(raw_headers)
    for name, value in raw_headers.items():
        lower = str(name).lower()
        if (
            lower in blocked_headers
            or lower in {"authorization", "cookie", "content-length", "host", "origin", "referer"}
            or lower.startswith("x-csrf")
        ):
            continue
        headers[str(name)] = str(value)
    return headers


def _send_extension_sidecar_proxy_response(handler, status: int, body: bytes, headers) -> bool:
    handler.send_response(status)
    sent_content_type = False
    blocked_headers = _connection_bound_header_names(headers)
    if headers and hasattr(headers, "items"):
        for name, value in headers.items():
            lower = str(name).lower()
            if lower in blocked_headers or lower in {"content-length", "set-cookie"}:
                continue
            if lower == "content-type":
                sent_content_type = True
            handler.send_header(str(name), str(value))
    if not sent_content_type:
        handler.send_header("Content-Type", "application/octet-stream")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    _security_headers(handler)
    handler.end_headers()
    handler.wfile.write(body)
    return True


def _read_extension_sidecar_proxy_body(stream) -> bytes:
    body = stream.read(_EXTENSION_SIDECAR_PROXY_MAX_RESPONSE_BYTES + 1)
    if len(body) > _EXTENSION_SIDECAR_PROXY_MAX_RESPONSE_BYTES:
        raise ValueError("Extension sidecar response too large")
    return body


def _extension_sidecar_proxy_redirect_url(
    allowed_origin: str,
    request_url: str,
    redirect_url: str,
) -> str | None:
    resolved = urljoin(request_url, redirect_url or "")
    allowed = urlsplit(allowed_origin or "")
    parts = urlsplit(resolved)
    if not allowed.scheme or not allowed.netloc or not parts.scheme or not parts.netloc:
        return None
    allowed_scheme = allowed.scheme.lower()
    redirect_scheme = parts.scheme.lower()
    if redirect_scheme != allowed_scheme:
        return None
    allowed_name, allowed_port = _normalize_host_port(allowed.netloc)
    redirect_name, redirect_port = _normalize_host_port(parts.netloc)
    if redirect_name != allowed_name or not _ports_match(
        allowed_scheme,
        redirect_port,
        allowed_port,
    ):
        return None
    return resolved


def _extension_sidecar_proxy_same_origin_opener(allowed_origin: str):
    class _SameOriginRedirectHandler(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            resolved = _extension_sidecar_proxy_redirect_url(
                allowed_origin,
                req.full_url,
                newurl,
            )
            if not resolved:
                raise URLError("Extension sidecar redirect crossed declared origin")
            return super().redirect_request(req, fp, code, msg, headers, resolved)

    return build_opener(ProxyHandler({}), _SameOriginRedirectHandler)


def _handle_extension_sidecar_proxy(
    handler,
    parsed,
    method: str,
    *,
    read_request_body: bool = False,
):
    matched = _match_extension_sidecar_proxy_path(parsed.path)
    if matched is None:
        return False
    # Require same-origin browser provenance on EVERY proxied method, not just
    # GET. Browser extensions (the only legitimate caller) always send Origin/
    # Referer/Sec-Fetch-Site, so this costs nothing on the real path while
    # closing the GET-vs-unsafe-method asymmetry: without it, POST/PATCH/PUT/
    # DELETE fell through the CSRF compatibility path that intentionally admits
    # non-browser clients, giving unsafe methods weaker provenance than GET.
    if not _check_same_origin_browser_request(handler, require_provenance=True):
        return j(handler, {"error": _csrf_rejection_error(handler)}, status=403)
    try:
        request_body = _read_body_bytes(handler) if read_request_body else None
    except ValueError as exc:
        status = 413 if "too large" in str(exc).lower() else 400
        return bad(handler, str(exc), status=status)
    from api.extensions import (
        ExtensionSidecarProxyError,
        resolve_extension_sidecar_proxy_target,
    )

    extension_id, proxy_path = matched
    try:
        target = resolve_extension_sidecar_proxy_target(
            extension_id,
            proxy_path,
            query=parsed.query,
        )
        request = Request(
            target["upstream_url"],
            data=request_body,
            headers=_extension_sidecar_proxy_request_headers(handler),
            method=method,
        )
        opener = _extension_sidecar_proxy_same_origin_opener(target["origin"])
        with opener.open(request, timeout=10) as response:
            body = _read_extension_sidecar_proxy_body(response)
            return _send_extension_sidecar_proxy_response(
                handler,
                getattr(response, "status", 200),
                body,
                response.headers,
            )
    except ExtensionSidecarProxyError as exc:
        return bad(handler, str(exc), status=exc.status)
    except ValueError as exc:
        return bad(handler, str(exc), status=502)
    except HTTPError as exc:
        try:
            body = _read_extension_sidecar_proxy_body(exc)
        except ValueError as read_exc:
            return bad(handler, str(read_exc), status=502)
        return _send_extension_sidecar_proxy_response(
            handler,
            exc.code,
            body,
            exc.headers,
        )
    except (TimeoutError, URLError, OSError):
        logger.warning(
            "extension sidecar proxy failed for %s %s",
            method,
            parsed.path,
            exc_info=True,
        )
        return bad(handler, "Failed to reach extension sidecar", status=502)


def _client_ip_for_rate_limit(handler) -> str:
    try:
        address = getattr(handler, "client_address", None)
        if address:
            return str(address[0])
    except Exception:
        pass
    return "unknown"


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


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


def _forwarded_client_ip_from_trusted_proxy(handler):
    """Resolve the real client IP from a chain fronted by a trusted proxy.

    Precondition: the caller has verified the raw socket peer is a trusted proxy.
    Consumes ALL X-Forwarded-For values (across repeated headers), preserves wire
    order, walks RIGHT-TO-LEFT skipping hops that are themselves trusted-proxy
    addresses, and returns the first non-trusted (i.e. real-client) hop. Falls
    back to X-Real-IP, then the raw socket peer. Returns None when the chain is
    present-but-empty / malformed so the caller fails closed.
    """
    import ipaddress

    try:
        xff_values = handler.headers.get_all("X-Forwarded-For") or []
    except AttributeError:
        single = handler.headers.get("X-Forwarded-For", "")
        xff_values = [single] if single else []

    hops: list[str] = []
    for header_value in xff_values:
        for token in str(header_value or "").split(","):
            hops.append(token.strip())

    if xff_values:
        # A present-but-empty / all-blank XFF is malformed → fail closed.
        if not any(hops):
            return None
        trusted_nets = _trusted_proxy_networks()

        def _is_trusted_hop(ip_str: str) -> bool:
            try:
                addr = ipaddress.ip_address(ip_str)
            except ValueError:
                return False
            return _ip_in_networks(addr, trusted_nets)

        for hop in reversed(hops):
            if not hop:
                # An empty hop inside the chain is malformed → fail closed
                # rather than skip past it (an attacker could inject blanks).
                return None
            try:
                ipaddress.ip_address(hop)
            except ValueError:
                # Non-IP token in the chain → malformed → fail closed.
                return None
            if _is_trusted_hop(hop):
                continue
            return hop
        # Every hop was a trusted proxy → no distinct client; treat as the proxy
        # tier itself (loopback/private), i.e. resolve to the raw peer below.
        return _request_client_ip(handler)

    real_ip = handler.headers.get("X-Real-IP", "").strip()
    if real_ip:
        return real_ip
    # No forwarded header at all → the trusted proxy is speaking for itself.
    return _request_client_ip(handler)


def _onboarding_request_is_local(handler) -> bool:
    """Return True when an unauthenticated onboarding request is local/private.

    Trust model (single, symmetric — see the full truth table in
    tests/test_cvd3_terminal_local_origin_gate.py):

    * Forwarded client-IP headers are honored ONLY when the RAW socket peer is a
      trusted proxy (loopback, or an address in HERMES_WEBUI_TRUSTED_PROXY_CIDRS).
      This is checked on the un-spoofable socket address, so a direct client
      cannot promote itself to "local" by sending X-Forwarded-For: 127.0.0.1.
    * When the peer is NOT a trusted proxy, forwarded headers are ignored and the
      request is classified by the raw socket peer directly. A direct loopback or
      private/LAN client (no proxy) is therefore still correctly local — so
      onboarding, first-password/passkey setup, and passwordless embedded-terminal
      access keep working on the common direct-LAN deployment.
    * HERMES_WEBUI_TRUST_FORWARDED_FOR=1 is the opt-in that makes us CONSULT the
      forwarded chain at all; without it the raw peer is authoritative. Either
      way the classification fails closed on malformed/empty chains.
    """
    trust_forwarded = _truthy_env("HERMES_WEBUI_TRUST_FORWARDED_FOR")
    peer_is_trusted_proxy = _raw_peer_is_trusted_proxy(handler)

    if trust_forwarded and peer_is_trusted_proxy:
        client_ip = _forwarded_client_ip_from_trusted_proxy(handler)
        if client_ip is None:
            # Malformed/empty forwarded chain from a trusted proxy → fail closed.
            return False
        parsed_ok, is_local = _ip_is_loopback_or_private(client_ip)
        return parsed_ok and is_local

    # Not consulting the forwarded chain (either the opt-in is off, or the raw
    # peer is not a trusted proxy). Classify by the raw socket peer — it cannot
    # be spoofed by a header. A public peer sending X-Forwarded-For: 127.0.0.1 is
    # therefore correctly rejected (its raw peer is public).
    raw = _request_client_ip(handler)
    parsed_ok, is_local = _ip_is_loopback_or_private(raw)
    if not parsed_ok:
        return False

    import ipaddress

    addr = ipaddress.ip_address(raw.strip())
    if addr.is_loopback:
        # A loopback TCP source is genuinely same-host and unspoofable → local
        # even if a (ignored) forwarded header is present.
        return True

    # Non-loopback raw peer. A forwarded header being PRESENT here means the
    # request most likely arrived through a proxy we have NOT been told to trust
    # (no trusted-proxy env, or the peer isn't in the allowlist) — so a
    # private/LAN raw peer could be an untrusted proxy relaying an arbitrary
    # (public) client we can't see. Deny in that case; require the operator to
    # opt in via HERMES_WEBUI_TRUST_FORWARDED_FOR (+ HERMES_WEBUI_TRUSTED_PROXY_CIDRS
    # for a non-loopback proxy). With NO forwarded header, a direct private/LAN
    # client (the common direct-LAN deployment) stays local so onboarding,
    # first-password/passkey setup, and passwordless terminal keep working.
    forwarded_present = bool(
        (handler.headers.get("X-Forwarded-For", "") or "").strip()
        or (handler.headers.get("X-Real-IP", "") or "").strip()
    )
    if forwarded_present:
        return False
    return bool(is_local)


def _onboarding_gate_allows(handler, auth_enabled: bool | None = None) -> bool:
    from api.auth import is_auth_enabled

    auth_enabled = is_auth_enabled() if auth_enabled is None else auth_enabled
    if auth_enabled or _truthy_env("HERMES_WEBUI_ONBOARDING_OPEN"):
        return True
    return _onboarding_request_is_local(handler)


# Operator-facing copy reused by every embedded-terminal endpoint refusal.
_EMBEDDED_TERMINAL_GATE_DENIED_MESSAGE = (
    "Embedded terminal is only available from local networks when authentication "
    "is not configured. Configure a password/passkey, or set "
    "HERMES_WEBUI_ONBOARDING_OPEN=1 to allow it on a deliberately-exposed server."
)


def _embedded_terminal_gate_allows(handler) -> bool:
    """Local-origin gate for the embedded-terminal endpoints.

    The embedded terminal spawns a PTY shell that runs arbitrary commands as the
    server-process user, so admitting an unauthenticated remote caller is remote
    code execution. When auth is enabled, ``check_auth()`` has already verified
    the session cookie before the request reaches these handlers, so this returns
    True. When auth is DISABLED (the default out-of-the-box state) ``check_auth()``
    admits every caller unconditionally, so restrict the terminal to local/private
    origins — the same trust model the onboarding/bootstrap endpoints use, ignoring
    spoofable forwarded headers unless an operator has opted into trusting them.
    A deliberately-exposed passwordless server (access secured at another layer)
    opts out with ``HERMES_WEBUI_ONBOARDING_OPEN=1``.
    """
    return _onboarding_gate_allows(handler)


# Above this many distinct client keys, sweep out entries whose timestamps have
# all aged past the window on the next update. Behind a reverse proxy the map
# holds a single key (the proxy IP) and never trips this; a directly-exposed
# deployment would otherwise keep one entry forever for every IP that ever hit
# the endpoint, since a key is only revisited when that same IP calls again.
_RATE_LIMIT_MAP_SWEEP_THRESHOLD = 4096


def _prune_stale_rate_limit_keys(mapping: dict, cutoff: float) -> None:
    """Drop keys whose newest timestamp has aged out of the window. Caller holds
    the map's lock. Size-gated so the common (few-key) path stays O(1)."""
    if len(mapping) <= _RATE_LIMIT_MAP_SWEEP_THRESHOLD:
        return
    stale = [k for k, ts in mapping.items() if not ts or ts[-1] < cutoff]
    for k in stale:
        del mapping[k]


def _csp_report_rate_limited(handler, *, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    key = _client_ip_for_rate_limit(handler)
    cutoff = now - _CSP_REPORT_RATE_LIMIT_WINDOW_SECONDS
    with _CSP_REPORT_RATE_LIMIT_LOCK:
        _prune_stale_rate_limit_keys(_CSP_REPORT_RATE_LIMIT, cutoff)
        timestamps = [ts for ts in _CSP_REPORT_RATE_LIMIT.get(key, []) if ts >= cutoff]
        if len(timestamps) >= _CSP_REPORT_RATE_LIMIT_MAX:
            _CSP_REPORT_RATE_LIMIT[key] = timestamps
            return True
        timestamps.append(now)
        _CSP_REPORT_RATE_LIMIT[key] = timestamps
    return False


def _client_event_rate_limited(handler, *, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    key = _client_ip_for_rate_limit(handler)
    cutoff = now - _CLIENT_EVENT_RATE_LIMIT_WINDOW_SECONDS
    with _CLIENT_EVENT_RATE_LIMIT_LOCK:
        _prune_stale_rate_limit_keys(_CLIENT_EVENT_RATE_LIMIT, cutoff)
        timestamps = [ts for ts in _CLIENT_EVENT_RATE_LIMIT.get(key, []) if ts >= cutoff]
        if len(timestamps) >= _CLIENT_EVENT_RATE_LIMIT_MAX:
            _CLIENT_EVENT_RATE_LIMIT[key] = timestamps
            return True
        timestamps.append(now)
        _CLIENT_EVENT_RATE_LIMIT[key] = timestamps
    return False


def _send_no_content(handler, status: int = 204) -> bool:
    handler.send_response(status)
    handler.send_header("Content-Length", "0")
    handler.end_headers()
    return True


def _safe_content_length(handler, max_bytes: int) -> int:
    raw_length = handler.headers.get("Content-Length", 0)
    try:
        length = int(raw_length)
    except (TypeError, ValueError):
        try:
            handler.close_connection = True
        except Exception:
            pass
        raise ValueError(f"Invalid Content-Length: {raw_length!r}") from None
    if length < 0:
        try:
            handler.close_connection = True
        except Exception:
            pass
        raise ValueError(f"Invalid Content-Length: {length}")
    if length > max_bytes:
        try:
            handler.close_connection = True
        except Exception:
            pass
        raise OverflowError(f"Request body too large ({length} bytes, max {max_bytes})")
    return length


def _read_csp_report_payload(handler):
    try:
        length = _safe_content_length(handler, _CSP_REPORT_MAX_BODY_BYTES)
    except OverflowError as exc:
        try:
            handler.rfile.read(_CSP_REPORT_MAX_BODY_BYTES)
        except Exception:
            pass
        return {"discarded": "body_too_large", "error": str(exc)}
    except ValueError as exc:
        return {"discarded": "invalid_content_length", "error": str(exc)}
    raw = handler.rfile.read(length) if length else b"{}"
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {"invalid": True, "bytes": len(raw)}


def _handle_csp_report(handler) -> bool:
    """Collect browser CSP report-only violations without requiring auth."""
    if _csp_report_rate_limited(handler):
        _CSP_REPORT_LOGGER.warning(
            "Dropped CSP report from %s: rate limit exceeded",
            _client_ip_for_rate_limit(handler),
        )
        return _send_no_content(handler)

    payload = _read_csp_report_payload(handler)
    _CSP_REPORT_LOGGER.info("CSP report from %s: %s", _client_ip_for_rate_limit(handler), payload)
    return _send_no_content(handler)


def _bounded_client_event_string(value, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


def _sanitize_client_event_url_path(value) -> str | None:
    text = _bounded_client_event_string(value, 1024)
    if not text:
        return None
    try:
        parsed = urlsplit(text)
        path = parsed.path or "/"
    except Exception:
        path = text.split("?", 1)[0] or "/"
    if not path.startswith("/"):
        path = "/" + path.lstrip("/")
    return path[: _CLIENT_EVENT_ALLOWED_FIELDS["url_path"]]


def _sanitize_client_event_payload(payload: dict | None) -> dict:
    """Whitelist tiny browser diagnostic events and discard sensitive content.

    Client-side SSE diagnostics should explain transport failures without
    persisting prompts, cookies, query strings, headers, or arbitrary browser
    payloads. This helper intentionally keeps only bounded scalar metadata.
    """
    if not isinstance(payload, dict):
        return {"event": "unknown"}
    sanitized: dict[str, object] = {}
    for field, limit in _CLIENT_EVENT_ALLOWED_FIELDS.items():
        if field == "url_path":
            value = _sanitize_client_event_url_path(payload.get(field))
        else:
            value = _bounded_client_event_string(payload.get(field), limit)
        if value is not None:
            sanitized[field] = value
    ready_state = payload.get("ready_state")
    if isinstance(ready_state, bool):
        pass
    elif isinstance(ready_state, int) and 0 <= ready_state <= 3:
        sanitized["ready_state"] = ready_state
    online = payload.get("online")
    if isinstance(online, bool):
        sanitized["online"] = online
    elif isinstance(online, str):
        lowered = online.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            sanitized["online"] = True
        elif lowered in {"false", "0", "no", "off"}:
            sanitized["online"] = False
    if "event" not in sanitized:
        sanitized["event"] = "unknown"
    return sanitized


def _read_client_event_payload(handler) -> dict:
    try:
        length = _safe_content_length(handler, _CLIENT_EVENT_MAX_BODY_BYTES)
    except OverflowError:
        try:
            handler.rfile.read(_CLIENT_EVENT_MAX_BODY_BYTES)
        except Exception:
            pass
        return {"event": "discarded", "reason": "body_too_large"}
    except ValueError:
        return {"event": "invalid", "reason": "invalid_content_length"}
    raw = handler.rfile.read(length) if length else b"{}"
    try:
        decoded = raw.decode("utf-8")
        payload = json.loads(decoded)
    except Exception:
        return {"event": "invalid", "reason": "invalid_json"}
    return payload if isinstance(payload, dict) else {"event": "invalid", "reason": "not_object"}


def _handle_client_event_log(handler, body: dict) -> bool:
    if _client_event_rate_limited(handler):
        _CLIENT_EVENT_LOGGER.warning(
            "Dropped client event from %s: rate limit exceeded",
            _client_ip_for_rate_limit(handler),
        )
        return j(handler, {"ok": False, "error": "rate_limited"}, status=429) or True
    payload = _sanitize_client_event_payload(body)
    _CLIENT_EVENT_LOGGER.info("Client event from %s: %s", _client_ip_for_rate_limit(handler), payload)
    return j(handler, {"ok": True, "event": payload.get("event")}) or True

__routes_exports__ = ('_CSP_REPORT_LOGGER', '_CSP_REPORT_RATE_LIMIT', '_CSP_REPORT_RATE_LIMIT_LOCK', '_CSP_REPORT_RATE_LIMIT_WINDOW_SECONDS', '_CSP_REPORT_RATE_LIMIT_MAX', '_CSP_REPORT_MAX_BODY_BYTES', '_CLIENT_EVENT_LOGGER', '_CLIENT_EVENT_RATE_LIMIT', '_CLIENT_EVENT_RATE_LIMIT_LOCK', '_CLIENT_EVENT_RATE_LIMIT_WINDOW_SECONDS', '_CLIENT_EVENT_RATE_LIMIT_MAX', '_CLIENT_EVENT_MAX_BODY_BYTES', '_EXTENSION_SIDECAR_PROXY_MAX_RESPONSE_BYTES', '_CLIENT_EVENT_ALLOWED_FIELDS', '_normalize_host_port', '_ports_match', '_allowed_public_origins', '_is_browser_unsafe_request', '_check_same_origin_browser_request', 'apply_cors_preflight_headers', '_csrf_exempt_path', '_CSRF_FAILURE_ATTR', '_set_csrf_failure_reason', '_clear_csrf_failure_reason', '_csrf_rejection_error', '_check_csrf', '_EXTENSION_SIDECAR_PROXY_RE', '_HOP_BY_HOP_HEADERS', '_connection_bound_header_names', '_match_extension_sidecar_proxy_path', '_read_body_bytes', '_extension_sidecar_proxy_request_headers', '_send_extension_sidecar_proxy_response', '_read_extension_sidecar_proxy_body', '_extension_sidecar_proxy_redirect_url', '_extension_sidecar_proxy_same_origin_opener', '_handle_extension_sidecar_proxy', '_client_ip_for_rate_limit', '_truthy_env', '_request_client_ip', '_ip_is_loopback_or_private', '_trusted_proxy_networks', '_ip_in_networks', '_raw_peer_is_trusted_proxy', '_forwarded_client_ip_from_trusted_proxy', '_onboarding_request_is_local', '_onboarding_gate_allows', '_EMBEDDED_TERMINAL_GATE_DENIED_MESSAGE', '_embedded_terminal_gate_allows', '_RATE_LIMIT_MAP_SWEEP_THRESHOLD', '_prune_stale_rate_limit_keys', '_csp_report_rate_limited', '_client_event_rate_limited', '_send_no_content', '_safe_content_length', '_read_csp_report_payload', '_handle_csp_report', '_bounded_client_event_string', '_sanitize_client_event_url_path', '_sanitize_client_event_payload', '_read_client_event_payload', '_handle_client_event_log')
