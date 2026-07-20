# ruff: noqa: F401 - this module is the deliberate authentication import facade
"""Authentication interface for Hermes WebUI.

Callers import authentication behavior from this package. Implementations are
grouped by responsibility so HTTP transport never needs to know their storage
or protocol details.
"""

from .authorization import (
    PUBLIC_PATHS,
    PROFILE_COOKIE_NAME,
    _TRUSTED_AUTH_WARNINGS_EMITTED,
    _ip_in_networks,
    _passkey_feature_flag_enabled,
    _raw_peer_is_trusted_proxy,
    _request_client_ip,
    _safe_login_inner_next,
    _trusted_proxy_networks,
    are_passkeys_enabled,
    build_profile_cookie,
    check_auth,
    clear_profile_cookie,
    ensure_trusted_auth_session,
    get_oidc_startup_warning,
    get_profile_cookie,
    get_profile_cookie_name,
    get_trusted_auth_logout_url,
    is_auth_enabled,
    is_oidc_auth_enabled,
    is_trusted_auth_enabled,
    reset_trusted_auth_request_state,
    trusted_session_allows_active_profile,
)
from .cookies_password import (
    COOKIE_NAME,
    CSRF_HEADER_NAME,
    SESSION_TTL,
    STATE_DIR,
    _AUTH_HASH_CACHE,
    _AUTH_HASH_COMPUTED,
    _LOGIN_ATTEMPTS_FILE,
    _LOGIN_MAX_ATTEMPTS,
    _LOGIN_WINDOW,
    _PBKDF2_KEY_CACHE,
    _SESSIONS_FILE,
    _SIGNING_KEY_CACHE,
    _check_login_rate,
    _clear_login_attempts,
    _hash_password,
    _invalidate_password_hash_cache,
    _is_loopback,
    _is_secure_context,
    _load_login_attempts,
    _login_attempts,
    _record_login_attempt,
    _resolve_cookie_name,
    _resolve_session_ttl,
    _sessions,
    _signing_key,
    clear_auth_cookie,
    create_session,
    csrf_token_for_session,
    get_password_hash,
    get_session_info,
    invalidate_session,
    is_password_auth_enabled,
    parse_cookie,
    session_bound_profile,
    set_auth_cookie,
    sign_profile_cookie_value,
    verify_csrf_token,
    verify_password,
    verify_profile_cookie_value,
    verify_session,
)
from .oauth import (
    cancel_onboarding_oauth_flow,
    poll_codex_token,
    poll_onboarding_oauth_flow,
    read_auth_json,
    resolve_runtime_provider_with_anthropic_env_lock,
    start_codex_device_code,
    start_onboarding_oauth_flow,
)
from .oidc import (
    OIDCAuthError,
    OIDCConfigError,
    build_authorization_redirect,
    complete_authorization_code_flow,
    is_oidc_enabled,
)
from .passkeys import (
    PasskeyError,
    PasskeyRateLimitError,
    authentication_options,
    clear_credentials,
    delete_credential,
    finish_login,
    finish_registration,
    passkeys_available,
    registered_credentials,
    registration_options,
    rp_context,
)


def login_rate_allowed(client_ip):
    """Return whether the client may attempt login through the public auth API."""
    return _check_login_rate(client_ip)


def record_login_attempt(client_ip):
    """Record a failed login attempt through the public auth API."""
    return _record_login_attempt(client_ip)


def clear_login_attempts(client_ip):
    """Clear a client's failed-login history through the public auth API."""
    return _clear_login_attempts(client_ip)


def passkey_feature_enabled():
    """Return whether passkey routes should be exposed."""
    return _passkey_feature_flag_enabled()


__all__ = [
    "COOKIE_NAME",
    "CSRF_HEADER_NAME",
    "OIDCAuthError",
    "OIDCConfigError",
    "PUBLIC_PATHS",
    "PROFILE_COOKIE_NAME",
    "PasskeyError",
    "PasskeyRateLimitError",
    "SESSION_TTL",
    "STATE_DIR",
    "are_passkeys_enabled",
    "authentication_options",
    "build_authorization_redirect",
    "build_profile_cookie",
    "cancel_onboarding_oauth_flow",
    "check_auth",
    "clear_auth_cookie",
    "clear_credentials",
    "clear_login_attempts",
    "clear_profile_cookie",
    "complete_authorization_code_flow",
    "create_session",
    "csrf_token_for_session",
    "delete_credential",
    "ensure_trusted_auth_session",
    "finish_login",
    "finish_registration",
    "get_oidc_startup_warning",
    "get_password_hash",
    "get_profile_cookie",
    "get_profile_cookie_name",
    "get_session_info",
    "get_trusted_auth_logout_url",
    "invalidate_session",
    "is_auth_enabled",
    "is_oidc_auth_enabled",
    "is_oidc_enabled",
    "is_password_auth_enabled",
    "is_trusted_auth_enabled",
    "login_rate_allowed",
    "parse_cookie",
    "passkeys_available",
    "passkey_feature_enabled",
    "poll_codex_token",
    "poll_onboarding_oauth_flow",
    "read_auth_json",
    "registered_credentials",
    "record_login_attempt",
    "registration_options",
    "reset_trusted_auth_request_state",
    "resolve_runtime_provider_with_anthropic_env_lock",
    "rp_context",
    "session_bound_profile",
    "set_auth_cookie",
    "sign_profile_cookie_value",
    "start_codex_device_code",
    "start_onboarding_oauth_flow",
    "trusted_session_allows_active_profile",
    "verify_csrf_token",
    "verify_password",
    "verify_profile_cookie_value",
    "verify_session",
]
