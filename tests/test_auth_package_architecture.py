"""Architecture contracts for the authentication package migration."""

from __future__ import annotations

import ast
from pathlib import Path

import api.auth as auth
import api.auth_oidc as legacy_oidc
import api.oauth as legacy_oauth
import api.passkeys as legacy_passkeys
from api.auth import oauth, oidc, passkeys


ROOT = Path(__file__).resolve().parents[1]


def test_auth_package_exposes_the_supported_interface():
    expected = {
        "check_auth",
        "create_session",
        "verify_session",
        "verify_csrf_token",
        "build_authorization_redirect",
        "complete_authorization_code_flow",
        "start_onboarding_oauth_flow",
        "poll_onboarding_oauth_flow",
        "registration_options",
        "authentication_options",
    }

    assert expected <= set(auth.__all__)
    assert all(callable(getattr(auth, name)) for name in expected)


def test_legacy_protocol_modules_keep_their_public_imports():
    assert legacy_oidc.build_authorization_redirect is oidc.build_authorization_redirect
    assert legacy_oidc.complete_authorization_code_flow is oidc.complete_authorization_code_flow
    assert legacy_oauth.start_onboarding_oauth_flow is oauth.start_onboarding_oauth_flow
    assert legacy_oauth.poll_onboarding_oauth_flow is oauth.poll_onboarding_oauth_flow
    assert legacy_passkeys.registration_options is passkeys.registration_options
    assert legacy_passkeys.authentication_options is passkeys.authentication_options


def test_auth_domain_does_not_import_http_routes_or_helpers():
    forbidden = {"api.routes", "api.routes_parts", "api.helpers"}
    offenders = []

    for path in sorted((ROOT / "api" / "auth").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = {node.module}
            else:
                continue
            if any(name in forbidden or name.startswith("api.routes_parts.") for name in imported):
                offenders.append(f"{path.name}:{node.lineno}")

    assert offenders == []
