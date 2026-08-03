"""Regression tests for v0.50.258 Opus pre-release follow-up.

PR #1419 introduced server-side `?next=` redirect after session expiry. The
initial implementation built the outer `next` parameter via:

    _next = quote(path, safe='/:@!$&\'()*+,;=')
    if query:
        _next += '?' + query
    location = 'login?next=' + quote(_next, safe='/:@!$&\'()*+,;=?')

Two problems with this shape:

1. The inner `?` was kept literal because both `quote()` calls had `?` in
   their `safe` set. Combined with `&` also being kept literal, paths with
   multi-param queries (`/api/sessions?limit=50&offset=0`) round-tripped as
   `/api/sessions?limit=50` — the rest got eaten as a top-level outer query
   parameter the login page ignored.

2. Attacker-controlled paths with embedded `&next=...` could inject a second
   top-level `next` parameter. Browsers' URLSearchParams.get() returns the
   first-match (safe), Python's parse_qs returns last-match (unsafe). The
   downstream `_safeNextPath()` rejects non-`/` prefixes which closed the
   actual exploit, but the parser-divergence is a footgun.

Fix: percent-encode the entire `path?query` blob with `safe='/'`, so `?`,
`&`, `=` all get encoded. The outer `next` then holds exactly one
path-with-query string that the browser auto-decodes once.
"""

from __future__ import annotations

import urllib.parse as _urlparse
from types import SimpleNamespace


class _RedirectHandler:
    def __init__(self):
        self.status = None
        self.headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.headers.append((name, value))

    def end_headers(self):
        pass

    def header(self, name):
        return next(value for key, value in self.headers if key.lower() == name.lower())


def _login_redirect(path: str, query: str, monkeypatch) -> str:
    """Run the live authorization owner through its unauthenticated page path."""
    from api.auth import authorization

    monkeypatch.setattr(authorization, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(authorization, "parse_cookie", lambda _handler: None)
    monkeypatch.setattr(
        authorization, "ensure_trusted_auth_session", lambda _handler: None
    )
    handler = _RedirectHandler()
    assert authorization.check_auth(handler, SimpleNamespace(path=path, query=query)) is False
    assert handler.status == 302
    return handler.header("Location")


def _browser_searchparams_get_next(location: str) -> str:
    """Mirror the browser's URLSearchParams.get('next') behaviour."""
    parsed = _urlparse.urlparse("https://host" + location)
    qs = _urlparse.parse_qs(parsed.query, keep_blank_values=True)
    values = qs.get("next", [])
    return values[0] if values else None


def test_redirect_roundtrip_simple_path(monkeypatch):
    location = _login_redirect("/foo/bar", "", monkeypatch)
    assert _browser_searchparams_get_next(location) == "/foo/bar"


def test_redirect_roundtrip_single_query_param(monkeypatch):
    location = _login_redirect("/foo/bar", "baz=qux", monkeypatch)
    assert _browser_searchparams_get_next(location) == "/foo/bar?baz=qux"


def test_redirect_roundtrip_multi_query_params(monkeypatch):
    """REGRESSION: pre-fix, this round-tripped to `/api/sessions?limit=50`
    (offset got eaten as a top-level outer query)."""
    location = _login_redirect("/sessions", "limit=50&offset=0", monkeypatch)
    got = _browser_searchparams_get_next(location)
    assert got == "/sessions?limit=50&offset=0", (
        f"multi-param query round-trip broken: got {got!r}, expected the full string"
    )


def test_redirect_roundtrip_attacker_controlled_next_injection_neutralized(monkeypatch):
    """REGRESSION: pre-fix, an attacker-controlled `&next=https://evil.com`
    in the source query injected a second top-level `next` parameter.
    Browsers parse first-match (benign), Python parses last-match (the evil
    value) — parser-divergence footgun even if downstream guards reject it."""
    location = _login_redirect("/admin", "action=foo&next=https://evil.com", monkeypatch)
    got = _browser_searchparams_get_next(location)
    # The entire string is preserved as the FIRST `next` value.
    assert got == "/admin?action=foo&next=https://evil.com"
    # And there is exactly ONE top-level `next` parameter.
    parsed = _urlparse.urlparse("https://host" + location)
    qs = _urlparse.parse_qs(parsed.query, keep_blank_values=True)
    assert len(qs.get("next", [])) == 1, (
        f"expected exactly one top-level `next` parameter, got {qs.get('next')}"
    )
    # _safeNextPath() in login.js (charAt(0)==='/' and charAt(1)!=='/') would
    # accept this as a valid same-origin path. The /admin page receives the
    # benign embedded query and the evil URL never becomes a redirect target.


def test_redirect_session_ttl_30_days():
    """Pin the SESSION_TTL constant to the 30-day value introduced by #1419."""
    from api.auth import SESSION_TTL

    assert SESSION_TTL == 86400 * 30, (
        "SESSION_TTL must be 30 days (86400 * 30) per #1419. Reverting to "
        "24h would re-introduce the daily-kick-out UX regression."
    )
