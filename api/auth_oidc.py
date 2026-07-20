"""Compatibility imports for the former ``api.auth_oidc`` module."""

from api.auth.oidc import (
    OIDCAuthError,
    OIDCConfigError,
    build_authorization_redirect,
    complete_authorization_code_flow,
    is_oidc_enabled,
)

__all__ = [
    "OIDCAuthError",
    "OIDCConfigError",
    "build_authorization_redirect",
    "complete_authorization_code_flow",
    "is_oidc_enabled",
]
