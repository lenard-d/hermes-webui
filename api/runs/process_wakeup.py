"""Process-wakeup pause revalidation and serialized admission policy."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from api.config import canonical_model_provider_lane
from api.profiles import is_root_profile, profile_scope_for_detached_worker
from api.providers import provider_has_process_wakeup_recovery_credential
from api.session_state import PENDING_BG_TASK_COMPLETIONS, session_agent_lock
from api.sessions import get_session
from api.sessions.process_wakeup import (
    PROCESS_WAKEUP_PAUSE_ERROR,
    clear_process_wakeup_pause,
    clear_process_wakeup_pause_if_model_changed,
    process_wakeup_credential_state_fingerprint,
    process_wakeup_pause_credential_state_changed,
    process_wakeup_pause_matches,
    suppress_process_wakeup_for_provider_pause,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcessWakeupAdmission:
    """Authoritative session generation plus an optional typed rejection."""

    session: object
    rejection: dict | None = None


def revalidation_provider(model, provider) -> str:
    """Return the canonical provider id used for credential revalidation."""
    try:
        _resolved_model, resolved_provider = canonical_model_provider_lane(
            model,
            provider,
        )
    except Exception:
        logger.debug(
            "failed to canonicalize process_wakeup revalidation lane for "
            "model=%r provider=%r",
            model,
            provider,
            exc_info=True,
        )
        resolved_provider = None
    candidate = resolved_provider if resolved_provider else provider
    return str(candidate or "").strip()


def provider_has_recovery_credential(
    session,
    *,
    model,
    provider,
    provider_id: str | None = None,
    credential_available: Callable[..., bool] | None = None,
) -> bool:
    """Check credential-pool recovery in the owning session profile."""
    provider_id = str(
        provider_id or revalidation_provider(model, provider) or ""
    ).strip()
    if not provider_id:
        return False
    if credential_available is None:
        credential_available = (
            provider_has_process_wakeup_recovery_credential
        )
    profile_name = str(getattr(session, "profile", "") or "").strip()
    if profile_name and not is_root_profile(profile_name):
        with profile_scope_for_detached_worker(
            profile_name,
            "process_wakeup credential revalidation",
            logger_override=logger,
        ):
            return credential_available(
                provider_id,
                refresh=True,
            )
    return credential_available(provider_id, refresh=True)


def refresh_pause_credential_fingerprint(session) -> bool:
    """Refresh the stored credential fingerprint without clearing the pause."""
    pause = getattr(session, "process_wakeup_pause", None)
    if not isinstance(pause, dict) or not pause.get("paused"):
        return False
    updated = dict(pause)
    updated["credential_state_fingerprint"] = (
        process_wakeup_credential_state_fingerprint(session)
    )
    session.process_wakeup_pause = updated
    return True


def _save_pause_state(session, log_message: str) -> None:
    try:
        session.save(touch_updated_at=False)
    except Exception:
        logger.debug(log_message, session.session_id, exc_info=True)


def revalidate_process_wakeup(
    session_id: str,
    *,
    model,
    provider,
    source: str,
    load_session: Callable[[str], object] | None = None,
    recovery_probe: Callable[..., bool] | None = None,
) -> ProcessWakeupAdmission:
    """Revalidate and, when necessary, suppress one autonomous wakeup.

    The complete check/update/save transition runs under the session owner lock.
    This prevents a credential recovery clear from racing a concurrent wakeup
    that increments the same persisted pause window.
    """
    if load_session is None:
        load_session = get_session
    if recovery_probe is None:
        recovery_probe = provider_has_recovery_credential
    with session_agent_lock(session_id):
        session = load_session(session_id)
        if clear_process_wakeup_pause_if_model_changed(
            session,
            model=model,
            provider=provider,
        ):
            _save_pause_state(
                session,
                "failed to persist process_wakeup pause reset for session %s",
            )

        if source != "process_wakeup":
            return ProcessWakeupAdmission(session)

        credential_state_changed = False
        try:
            credential_state_changed = (
                process_wakeup_pause_credential_state_changed(session)
            )
        except Exception:
            logger.debug(
                "failed to compare process_wakeup credential state for session %s",
                session_id,
                exc_info=True,
            )

        if process_wakeup_pause_matches(
            session,
            model=model,
            provider=provider,
            classification="credential_pool_empty",
        ):
            recovered = False
            provider_id = revalidation_provider(model, provider)
            try:
                recovered = recovery_probe(
                    session,
                    model=model,
                    provider=provider,
                    provider_id=provider_id,
                )
            except Exception:
                logger.debug(
                    "failed to revalidate process_wakeup credential availability "
                    "for session %s",
                    session_id,
                    exc_info=True,
                )
            if recovered:
                reason = (
                    "credential_state_changed"
                    if credential_state_changed
                    else "credential_recovered"
                )
                if clear_process_wakeup_pause(session, reason=reason):
                    _save_pause_state(
                        session,
                        "failed to persist process_wakeup credential recovery reset "
                        "for session %s",
                    )
            elif credential_state_changed and refresh_pause_credential_fingerprint(
                session
            ):
                _save_pause_state(
                    session,
                    "failed to persist process_wakeup credential-state fingerprint "
                    "refresh for session %s",
                )

        pause = suppress_process_wakeup_for_provider_pause(
            session,
            model=model,
            provider=provider,
            classification="credential_pool_empty",
        )
        if pause is None:
            return ProcessWakeupAdmission(session)

        try:
            PENDING_BG_TASK_COMPLETIONS.discard(session.session_id)
        except Exception:
            logger.debug(
                "failed to discard pending bg-task marker for paused wakeup %s",
                session_id,
                exc_info=True,
            )
        _save_pause_state(
            session,
            "failed to persist process_wakeup suppression for session %s",
        )
        return ProcessWakeupAdmission(
            session,
            {
                "error": PROCESS_WAKEUP_PAUSE_ERROR,
                "message": (
                    "Automatic process wakeups are paused for this session because "
                    "the provider credential pool is unavailable."
                ),
                "process_wakeup_pause": pause,
                "_status": 409,
            },
        )
