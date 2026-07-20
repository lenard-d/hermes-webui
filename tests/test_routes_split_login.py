"""Compatibility checks for the login and OIDC presentation extraction."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from api import routes
from api.routes_parts import login


REPO = Path(__file__).resolve().parents[1]

_LOGIN_FUNCTION_EXPORTS = (
    "_resolve_login_locale_key",
    "_safe_login_redirect_path",
    "_request_base_url",
    "_oidc_login_html",
)


def test_login_part_declares_the_complete_presentation_owner():
    assert login.__routes_exports__ == (
        "_LOGIN_LOCALE",
        "_resolve_login_locale_key",
        "_LOGIN_PAGE_HTML",
        "_safe_login_redirect_path",
        "_request_base_url",
        "_oidc_login_html",
    )


def test_login_function_exports_remain_owned_by_routes_facade():
    for name in _LOGIN_FUNCTION_EXPORTS:
        route_export = getattr(routes, name)
        assert route_export.__module__ == "api.routes"
        assert route_export.__globals__ is vars(routes)

    assert routes._LOGIN_LOCALE is login._LOGIN_LOCALE
    assert routes._LOGIN_PAGE_HTML is login._LOGIN_PAGE_HTML


def test_oidc_login_html_resolves_facade_redirect_guard_at_call_time(monkeypatch):
    seen = []
    monkeypatch.setattr("api.auth_oidc.is_oidc_enabled", lambda: True)
    monkeypatch.setattr(
        routes,
        "_safe_login_redirect_path",
        lambda value: seen.append(value) or "/after-login",
    )

    rendered = routes._oidc_login_html(SimpleNamespace(query="next=%2Frequested"))

    assert seen == ["/requested"]
    assert 'href="/api/auth/oidc/start?next=/after-login"' in rendered


def test_request_base_url_keeps_secure_context_and_host_contract(monkeypatch):
    monkeypatch.setattr("api.auth._is_secure_context", lambda _handler: True)
    handler = SimpleNamespace(headers={"Host": "hermes.example:9443"})

    assert routes._request_base_url(handler) == "https://hermes.example:9443"


def test_login_part_imports_without_loading_routes_facade():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import api.routes_parts.login as owner; "
                "assert owner.__routes_exports__; "
                "assert 'api.routes' not in sys.modules"
            ),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_login_owner_stops_before_logs_domain():
    owner_source = Path(login.__file__).read_text(encoding="utf-8")
    facade_source = Path(routes.__file__).read_text(encoding="utf-8")

    assert "_LOG_FILE_WHITELIST" not in owner_source
    assert "def _handle_logs(" not in owner_source
    assert "_LOG_FILE_WHITELIST" in facade_source
    assert "def _handle_logs(" in facade_source
    assert "exec(" not in owner_source
