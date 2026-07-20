"""Provider-unavailable wakeup pause state and credential fingerprints."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path

import api.config as _cfg
from api.config import HOME

logger = logging.getLogger(__name__)

PROCESS_WAKEUP_PROVIDER_UNAVAILABLE_TYPES = frozenset({
    'credential_pool_empty',
})
PROCESS_WAKEUP_PAUSE_ERROR = 'process_wakeup_paused'
_PROCESS_WAKEUP_PAUSE_VERSION = 1


def _process_wakeup_pause_part(value) -> str:
    return str(value or '').strip().lower()


def _process_wakeup_pause_provider_part(value) -> str:
    provider = _process_wakeup_pause_part(value)
    if not provider:
        return ''
    try:
        return _process_wakeup_pause_part(_cfg._resolve_provider_alias(provider))
    except Exception:
        return provider


def _process_wakeup_pause_lane(model=None, provider=None) -> tuple[str, str]:
    model_part = _process_wakeup_pause_part(model)
    provider_part = _process_wakeup_pause_provider_part(provider)
    try:
        resolved_model, resolved_provider = _cfg.canonical_model_provider_lane(model, provider)
    except Exception:
        logger.debug(
            "failed to canonicalize process_wakeup pause lane for model=%r provider=%r",
            model,
            provider,
            exc_info=True,
        )
        resolved_model, resolved_provider = None, None
    if resolved_model:
        model_part = _process_wakeup_pause_part(resolved_model)
    if resolved_provider:
        provider_part = _process_wakeup_pause_provider_part(resolved_provider)
    return model_part, provider_part


def _process_wakeup_pause_int(value, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _process_wakeup_pause_float(value, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _process_wakeup_pause_key(model=None, provider=None, classification=None) -> dict:
    model_part, provider_part = _process_wakeup_pause_lane(model, provider)
    return {
        'model': model_part,
        'provider': provider_part,
        'classification': _process_wakeup_pause_part(classification),
    }


def process_wakeup_pause_matches(session, *, model=None, provider=None, classification=None) -> bool:
    """Return True when the session has an active pause for this wakeup lane."""
    pause = getattr(session, 'process_wakeup_pause', None)
    if not isinstance(pause, dict) or not pause.get('paused'):
        return False
    expected = _process_wakeup_pause_key(model, provider, classification)
    for key, expected_value in expected.items():
        if key == 'classification' and not expected_value:
            continue
        if _process_wakeup_pause_part(pause.get(key)) != expected_value:
            return False
    return True


def clear_process_wakeup_pause(session, *, reason: str = '') -> bool:
    """Clear any persisted process-wakeup pause metadata.

    Returns True when a pause was present. Callers decide whether and how to
    persist, because some paths are already inside a larger session writeback.
    """
    pause = getattr(session, 'process_wakeup_pause', None)
    if not isinstance(pause, dict) or not pause:
        return False
    session.process_wakeup_pause = {}
    if reason:
        session._last_process_wakeup_pause_clear_reason = str(reason)
    return True


def clear_process_wakeup_pause_if_model_changed(session, *, model=None, provider=None) -> bool:
    """Reset a wakeup pause when the resolved model/provider lane changed."""
    pause = getattr(session, 'process_wakeup_pause', None)
    if not isinstance(pause, dict) or not pause.get('paused'):
        return False
    current = _process_wakeup_pause_key(model, provider, pause.get('classification'))
    if (
        _process_wakeup_pause_part(pause.get('model')) == current['model']
        and _process_wakeup_pause_part(pause.get('provider')) == current['provider']
    ):
        return False
    return clear_process_wakeup_pause(session, reason='model_or_provider_changed')


def record_process_wakeup_provider_unavailable_pause(
    session,
    *,
    classification: str,
    model=None,
    provider=None,
) -> dict | None:
    """Record the first visible provider-unavailable wakeup failure.

    The persisted object is deliberately metadata-only: it records the lane and
    counters, not the wakeup prompt or provider response body, so it remains
    auditable without copying process output or credentials into diagnostics.
    """
    classification = _process_wakeup_pause_part(classification)
    if classification not in PROCESS_WAKEUP_PROVIDER_UNAVAILABLE_TYPES:
        return None
    now = time.time()
    key = _process_wakeup_pause_key(model, provider, classification)
    credential_state_fingerprint = process_wakeup_credential_state_fingerprint(session)
    existing = getattr(session, 'process_wakeup_pause', None)
    same_window = (
        isinstance(existing, dict)
        and existing.get('paused')
        and _process_wakeup_pause_part(existing.get('model')) == key['model']
        and _process_wakeup_pause_part(existing.get('provider')) == key['provider']
        and _process_wakeup_pause_part(existing.get('classification')) == key['classification']
    )
    visible_error_count = 1
    suppressed_count = 0
    first_paused_at = now
    if same_window:
        first_paused_at = _process_wakeup_pause_float(existing.get('first_paused_at'), now)
        visible_error_count = _process_wakeup_pause_int(
            existing.get('visible_error_count'),
            1,
        ) + 1
        suppressed_count = _process_wakeup_pause_int(existing.get('suppressed_count'), 0)
    session.process_wakeup_pause = {
        'version': _PROCESS_WAKEUP_PAUSE_VERSION,
        'paused': True,
        'source': 'process_wakeup',
        'classification': key['classification'],
        'model': key['model'],
        'provider': key['provider'],
        'first_paused_at': first_paused_at,
        'last_error_at': now,
        'visible_error_count': visible_error_count,
        'suppressed_count': suppressed_count,
        'credential_state_fingerprint': credential_state_fingerprint,
    }
    return session.process_wakeup_pause


def suppress_process_wakeup_for_provider_pause(
    session,
    *,
    model=None,
    provider=None,
    classification: str = 'credential_pool_empty',
) -> dict | None:
    """Increment suppression metadata if this automatic wakeup is paused."""
    if not process_wakeup_pause_matches(
        session,
        model=model,
        provider=provider,
        classification=classification,
    ):
        return None
    pause = dict(getattr(session, 'process_wakeup_pause', {}) or {})
    pause['suppressed_count'] = _process_wakeup_pause_int(pause.get('suppressed_count'), 0) + 1
    pause['last_suppressed_at'] = time.time()
    pause['last_suppressed_reason'] = 'provider_unavailable_pause'
    session.process_wakeup_pause = pause
    return pause


_PROCESS_WAKEUP_AUTH_ROTATION_KEYS = frozenset({
    'expires_at',
    'expires_at_ms',
    'expires_in',
    'last_status',
    'last_status_at',
    'last_error_code',
    'last_error_reason',
    'last_error_message',
    'last_error_reset_at',
    'request_count',
    'updated_at',
})
_PROCESS_WAKEUP_AUTH_SECRET_PRESENCE_KEYS = frozenset({
    'access_token',
    'refresh_token',
    'id_token',
    'api_key',
    'secret',
    'client_secret',
    'runtime_api_key',
    'token',
})


def _process_wakeup_secret_presence(value):
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _process_wakeup_auth_fingerprint_payload(value):
    if isinstance(value, dict):
        payload = {}
        for key, child in value.items():
            key_text = str(key)
            key_norm = key_text.strip().lower()
            if key_norm in _PROCESS_WAKEUP_AUTH_ROTATION_KEYS:
                continue
            if key_norm in _PROCESS_WAKEUP_AUTH_SECRET_PRESENCE_KEYS:
                payload[key_text] = _process_wakeup_secret_presence(child)
            else:
                payload[key_text] = _process_wakeup_auth_fingerprint_payload(child)
        return payload
    if isinstance(value, list):
        return [_process_wakeup_auth_fingerprint_payload(item) for item in value]
    return value


def _process_wakeup_auth_store_fingerprint(path: Path) -> dict:
    p = Path(path).expanduser()
    payload: dict = {'path': str(p)}
    try:
        stat = p.stat()
    except FileNotFoundError:
        payload['missing'] = True
        return payload
    except OSError as exc:
        payload['error'] = exc.__class__.__name__
        return payload
    if not p.is_file():
        payload['kind'] = 'other'
        return payload
    try:
        raw = json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        payload['kind'] = 'file'
        payload['semantic'] = 'unparsed-fallback'
        payload['mtime_ns'] = int(stat.st_mtime_ns)
        payload['size'] = int(stat.st_size)
        return payload
    sanitized = _process_wakeup_auth_fingerprint_payload(raw)
    try:
        encoded = json.dumps(
            sanitized,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True,
            default=str,
        ).encode('utf-8')
        payload['semantic_sha256'] = hashlib.sha256(encoded).hexdigest()
    except Exception:
        payload['kind'] = 'file'
        payload['semantic'] = 'encode-fallback'
        payload['mtime_ns'] = int(stat.st_mtime_ns)
        payload['size'] = int(stat.st_size)
    return payload


def process_wakeup_credential_state_fingerprint(session) -> str:
    """Return a metadata-only fingerprint for credential/config state.

    auth.json is rewritten by OAuth/token-refresh and request telemetry churn.
    Hash its semantic content instead of mtime/size so those rewrites do not
    clear a credential-exhausted process-wakeup pause. Secret fields are
    represented only by presence booleans: adding a credential changes the
    fingerprint, while rotating an existing token value does not persist or
    compare secret material.
    """
    try:
        hermes_home = _get_profile_home(getattr(session, 'profile', None))
    except Exception:
        hermes_home = Path(os.environ.get('HERMES_HOME') or HOME).expanduser()
    files = []
    for name in ('auth.json', 'config.yaml', 'config.yml', '.env'):
        path = hermes_home / name
        if name == 'auth.json':
            files.append((name, _process_wakeup_auth_store_fingerprint(path)))
            continue
        try:
            stat = path.stat()
        except FileNotFoundError:
            files.append((name, 'missing'))
        except OSError as exc:
            files.append((name, 'error', exc.__class__.__name__))
        else:
            kind = 'file' if path.is_file() else 'other'
            files.append((name, kind, int(stat.st_mtime_ns), int(stat.st_size)))
    payload = {
        'version': 2,
        'profile': _process_wakeup_pause_part(getattr(session, 'profile', None)),
        'files': files,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def process_wakeup_pause_credential_state_changed(session) -> bool:
    """Return True when a stored credential pause should be revalidated."""
    pause = getattr(session, 'process_wakeup_pause', None)
    if not isinstance(pause, dict) or not pause.get('paused'):
        return False
    classification = _process_wakeup_pause_part(pause.get('classification'))
    if classification not in PROCESS_WAKEUP_PROVIDER_UNAVAILABLE_TYPES:
        return False
    previous = str(pause.get('credential_state_fingerprint') or '').strip()
    if not previous:
        return True
    return process_wakeup_credential_state_fingerprint(session) != previous


def _get_profile_home(profile) -> Path:
    """Resolve the hermes agent home directory for the given profile.

    Prefers the profile-specific helper from api.profiles; falls back to the
    HERMES_HOME environment variable or ~/.hermes, expanding ~ correctly.
    """
    try:
        from api.profiles import get_hermes_home_for_profile
        return Path(get_hermes_home_for_profile(profile))
    except ImportError:
        return Path(os.environ.get('HERMES_HOME') or '~/.hermes').expanduser()
