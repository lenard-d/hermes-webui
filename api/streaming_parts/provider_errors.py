"""Provider failure classification and safe browser error payloads."""

from __future__ import annotations

from types import ModuleType


def provider_error_probe_text(api: ModuleType, value) -> tuple[str, int | None]:
    """Flatten structured provider-error payloads into searchable text."""
    _texts: list[str] = []
    _status_code: int | None = None
    _seen: set[int] = set()

    def _walk(node):
        nonlocal _status_code
        if node is None:
            return
        if isinstance(node, (dict, list, tuple, set)):
            _node_id = id(node)
            if _node_id in _seen:
                return
            _seen.add(_node_id)
        if isinstance(node, dict):
            for _key in ('type', 'code', 'message', 'detail', 'details', 'name', 'status', 'status_code'):
                _val = node.get(_key)
                if _val is None:
                    continue
                if _key in ('status', 'status_code') and _status_code is None:
                    try:
                        _status_code = int(_val)
                    except Exception:
                        pass
                _texts.append(str(_val))
            for _key, _val in node.items():
                if _key in ('type', 'code', 'message', 'detail', 'details', 'name', 'status', 'status_code'):
                    continue
                _walk(_val)
            return
        if isinstance(node, (list, tuple, set)):
            for _item in node:
                _walk(_item)
            return
        _texts.append(str(node))

    _walk(value)
    return ' '.join(t for t in _texts if t).strip(), _status_code


def classify_provider_error(api: ModuleType, err_str: str, exc=None, *, silent_failure: bool = False) -> dict:
    """Classify provider/agent failure text for WebUI apperror UX.

    Keep this string-based until hermes-agent exposes stable structured
    provider error classes for Codex OAuth plan limits.
    """
    _probe_text, _probe_status_code = api._provider_error_probe_text(err_str)
    if exc is not None:
        _exc_probe_text, _exc_status_code = api._provider_error_probe_text(exc)
        if _exc_probe_text:
            _probe_text = f"{_probe_text} {_exc_probe_text}".strip()
        if _probe_status_code is None:
            _probe_status_code = _exc_status_code
    err_str = str(_probe_text or err_str or '')
    _err_lower = err_str.lower()
    _exc_name = type(exc).__name__ if exc is not None else ''
    _is_cancelled = (
        'cancelled by user' in _err_lower
        or 'canceled by user' in _err_lower
        or 'user cancelled' in _err_lower
        or 'user canceled' in _err_lower
        or 'task cancelled' in _err_lower
        or 'task canceled' in _err_lower
        or 'cancellederror' in _err_lower
        or (exc is not None and _exc_name in ('CancelledError', 'CanceledError'))
    )
    _is_interrupted = (
        not _is_cancelled
        and (
            'interrupted by user' in _err_lower
            or 'response interrupted' in _err_lower
            or 'operation interrupted' in _err_lower
            or 'operation was interrupted' in _err_lower
            or 'operation aborted' in _err_lower
            or 'request was aborted' in _err_lower
            or 'aborterror' in _err_lower
            or (exc is not None and type(exc).__name__ in ('KeyboardInterrupt', 'AbortError'))
        )
    )
    if _is_cancelled:
        return {
            'label': 'Task cancelled',
            'type': 'cancelled',
            'hint': api._cancelled_turn_hint(),
        }
    if _is_interrupted:
        return {
            'label': 'Response interrupted',
            'type': 'interrupted',
            'hint': 'The run stopped before a provider response completed. If you did not cancel it, try again.',
        }
    _is_quota = api._is_quota_error_text(err_str)
    # A credential-POOL exhaustion ("All 0 credential(s) exhausted for <provider>")
    # is a distinct shape from account/plan quota: it means the profile's
    # credential pool has no usable keys for that provider (a config problem),
    # not that a funded account ran out of credits. It is NOT matched by
    # api._is_quota_error_text ('credential(s) exhausted' != 'credits exhausted'), so
    # without this it fell through to the generic error label/hint. Classify it
    # explicitly so the user gets a pool-specific, actionable hint. (#3929)
    _is_credential_pool_empty = (
        'credential(s) exhausted' in _err_lower
        or 'credentials exhausted' in _err_lower
        or ('credential' in _err_lower and 'exhausted' in _err_lower)
    )
    _is_auth = (
        not _is_quota and not _is_credential_pool_empty and (
            _probe_status_code == 401
            or
            '401' in err_str
            or (exc is not None and 'AuthenticationError' in _exc_name)
            or 'authentication' in _err_lower
            or 'unauthorized' in _err_lower
            or 'invalid api key' in _err_lower
            or 'invalid_api_key' in _err_lower
            or 'no cookie auth credentials' in _err_lower
        )
    )
    _is_not_found = (
        # model_not_found hints mention Settings / `hermes model` below.
        '404' in err_str
        or 'not found' in _err_lower
        or 'does not exist' in _err_lower
        or 'model not found' in _err_lower
        or 'model_not_found' in _err_lower  # hint below points to Settings / `hermes model`
        or 'invalid model' in _err_lower
        or 'does not match any known model' in _err_lower
        or 'unknown model' in _err_lower
    )
    _is_rate_limit = (not _is_quota) and (
        'rate limit' in _err_lower or '429' in err_str or (exc is not None and 'RateLimitError' in _exc_name)
    )
    _is_compression_exhausted = (
        'compression_exhausted' in _err_lower
        or 'compression exhausted' in _err_lower
        or ('context length exceeded' in _err_lower and 'cannot compress further' in _err_lower)
        or ('context compression' in _err_lower and 'max compression attempts' in _err_lower)
    )
    if _is_credential_pool_empty:
        return {
            'label': 'No usable credentials',
            'type': 'credential_pool_empty',
            'hint': 'The credential pool for this provider has no usable keys left (all entries exhausted or unconfigured). Add or refresh a key for this provider in your Hermes config / credential pool, or switch providers via `hermes model`.',
        }
    if _is_quota:
        return {
            'label': 'Out of credits',
            'type': 'quota_exhausted',
            'hint': 'Your provider account is out of credits or usage. Top up, wait for the plan window to reset, or switch providers via `hermes model`.',
        }
    if _is_rate_limit:
        return {
            'label': 'Rate limit reached',
            'type': 'rate_limit',
            'hint': 'Rate limit reached. The fallback model (if configured) was also exhausted. Try again in a moment.',
        }
    if _is_auth:
        return {
            'label': 'Authentication failed',
            'type': 'auth_mismatch',
            'hint': 'The selected model may not be supported by your configured provider or your API key is invalid. Run `hermes model` in your terminal to update credentials, then restart the WebUI.',
        }
    if _is_not_found:
        return {
            'label': 'Model not found',
            'type': 'model_not_found',
            'hint': 'The selected model was not found by the provider. Check the model ID in Settings or run `hermes model` to verify it exists for your provider.',
        }
    if _is_compression_exhausted:
        return {
            'label': 'Context compression exhausted',
            'type': 'compression_exhausted',
            'hint': 'The conversation context is too large to compress safely. Start a new conversation or retry with a narrower task.',
        }
    if silent_failure:
        return {
            'label': 'No response from provider',
            # Preserve the existing no_response event type (#373) while making
            # the catch-all silent-failure message more specific for #1765.
            'type': 'no_response',
            'hint': 'The provider returned no content and no error. This often means a usage/rate limit was hit silently. Check provider status, switch providers via `hermes model`, or try again in a moment.',
        }
    return {'label': 'Error', 'type': 'error', 'hint': ''}


def provider_error_payload(api: ModuleType, message: str, err_type: str, hint: str = '') -> dict:
    """Build a bounded, redacted apperror payload with provider details."""
    _message = str(message or '')
    _safe_message = api._redact_text(_message).strip() if _message else ''
    payload: dict = {'message': _safe_message or _message, 'type': err_type}
    if hint:
        payload['hint'] = hint
    if _safe_message:
        _details = _safe_message
        if len(_details) > 1200:
            _details = _details[:1197].rstrip() + '…'
        if _details:
            payload['details'] = _details
    return payload
