"""Compatibility imports for the former ``api.passkeys`` module."""

from api.auth.passkeys import (
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

__all__ = [
    "PasskeyError",
    "PasskeyRateLimitError",
    "authentication_options",
    "clear_credentials",
    "delete_credential",
    "finish_login",
    "finish_registration",
    "passkeys_available",
    "registered_credentials",
    "registration_options",
    "rp_context",
]
