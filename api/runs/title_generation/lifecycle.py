"""Scheduling and durable publication of generated session titles.

Slow provider calls happen outside the session edit lock.  Every write reloads
the authoritative session under its owner lock so manual or newer titles win
over stale background workers.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Optional

from api.sessions.cache import get_session
from api.sessions.operations import mark_session_title_generated, session_has_manual_title
from api.sessions.repository import edit_session

from ..thinking_content import _looks_invalid_generated_title
from .policy import (
    _count_exchanges,
    _fallback_title_from_exchange,
    _first_exchange_snippets,
    _get_title_refresh_interval,
    _is_generic_fallback_title,
    _is_provisional_title,
    _latest_exchange_snippets,
)
from .provider_invocation import (
    _aux_title_configured,
    _generate_llm_session_title_for_agent,
    _generate_llm_session_title_via_aux,
)


logger = logging.getLogger(__name__)


def _put_title_status(put_event, session_id: str, status: str, reason: str = '', title: str = '', raw_preview: str = '') -> None:
    payload = {'session_id': session_id, 'status': status}
    if reason:
        payload['reason'] = reason
    if title:
        payload['title'] = title
    if raw_preview:
        payload['raw_preview'] = raw_preview
    put_event('title_status', payload)
    logger.info(
        "title_status session=%s status=%s reason=%s title=%r raw_preview=%r",
        session_id,
        status,
        reason or '-',
        title or '',
        (raw_preview or '')[:120],
    )


def _run_background_title_update(session_id: str, user_text: str, assistant_text: str, placeholder_title: str, put_event, agent=None):
    """Generate and publish a better title after `done`, then end the stream."""
    try:
        try:
            s = get_session(session_id)
        except KeyError:
            _put_title_status(put_event, session_id, 'skipped', 'missing_session')
            return
        # Allow self-heal when a previously generated title leaked thinking text.
        _invalid_existing = _looks_invalid_generated_title(s.title)
        if getattr(s, 'llm_title_generated', False) and not _invalid_existing:
            _put_title_status(put_event, session_id, 'skipped', 'already_generated', str(s.title or ''))
            return
        current = str(s.title or '').strip()
        if session_has_manual_title(s):
            _put_title_status(put_event, session_id, 'skipped', 'manual_title', current)
            return
        still_auto = (
            current == placeholder_title
            or current in ('Untitled', 'New Chat', '')
            or _is_provisional_title(current, s.messages)
            or _invalid_existing
        )
        if not still_auto:
            _put_title_status(put_event, session_id, 'skipped', 'manual_title', current)
            return
        from api import profiles as profiles_api

        with profiles_api.profile_env_for_background_worker(s, "background title", logger_override=logger):
            aux_title_configured = _aux_title_configured()
            if agent and not aux_title_configured:
                next_title, llm_status, raw_preview = _generate_llm_session_title_for_agent(agent, user_text, assistant_text)
                if not next_title and llm_status in ('llm_error', 'llm_invalid'):
                    next_title, llm_status, raw_preview = _generate_llm_session_title_via_aux(user_text, assistant_text, agent=agent, use_agent_model=True)
            else:
                next_title, llm_status, raw_preview = _generate_llm_session_title_via_aux(user_text, assistant_text)
                if not next_title and agent and llm_status in ('llm_error_aux', 'llm_invalid_aux'):
                    next_title, llm_status, raw_preview = _generate_llm_session_title_for_agent(agent, user_text, assistant_text)
            source = llm_status
            if not next_title:
                fallback_title = _fallback_title_from_exchange(user_text, assistant_text)
                if fallback_title and not _is_generic_fallback_title(fallback_title):
                    logger.debug("Using local fallback for session title generation")
                    next_title = fallback_title
                    source = 'fallback'
                elif fallback_title:
                    logger.debug("Skipping generic local fallback for session title generation: %r", fallback_title)
        fallback_reason = (
            f'local_summary:{llm_status}'
            if source == 'fallback' and llm_status
            else 'local_summary'
        )
        wrote_title = False
        effective_title = current
        if next_title:
            skip_for_newer_title = False
            try:
                with edit_session(
                    session_id,
                    touch_updated_at=False,
                    save_when=lambda _session: wrote_title,
                ) as current_session:
                    effective_title = str(current_session.title or '').strip()
                    manual_title = session_has_manual_title(current_session)
                    invalid_existing_now = _looks_invalid_generated_title(current_session.title)
                    still_auto = (
                        effective_title == placeholder_title
                        or effective_title in ('Untitled', 'New Chat', '')
                        or _is_provisional_title(effective_title, current_session.messages)
                        or invalid_existing_now
                    )
                    if manual_title or not still_auto:
                        skip_for_newer_title = True
                    elif next_title != effective_title:
                        current_session.title = next_title
                        mark_session_title_generated(current_session)
                        effective_title = current_session.title
                        wrote_title = True
            except KeyError:
                _put_title_status(put_event, session_id, 'skipped', 'missing_session')
                return
            if skip_for_newer_title:
                _put_title_status(put_event, session_id, 'skipped', 'manual_title', effective_title)
                return

        if wrote_title:
            if source == 'fallback':
                _put_title_status(put_event, session_id, source, fallback_reason, effective_title, raw_preview)
            else:
                _put_title_status(put_event, session_id, source, llm_status, effective_title, raw_preview)
            put_event('title', {'session_id': session_id, 'title': effective_title})
        else:
            _put_title_status(put_event, session_id, 'skipped', source or 'unchanged', effective_title, raw_preview)
    finally:
        put_event('stream_end', {'session_id': session_id})


def _run_background_title_refresh(session_id: str, user_text: str, assistant_text: str, current_title: str, put_event, agent=None):
    """Refresh an existing LLM-generated title using the latest exchange text.

    Unlike _run_background_title_update, this does NOT guard on
    llm_title_generated — it assumes the title was already LLM-generated
    and the session has progressed enough to warrant a refresh.
    It does NOT emit stream_end (the caller already did).
    """
    try:
        try:
            s = get_session(session_id)
        except KeyError:
            return
        # Safety: skip if user manually renamed since the check
        effective = str(s.title or '').strip()
        if session_has_manual_title(s):
            _put_title_status(put_event, session_id, 'skipped', 'manual_title', effective)
            return
        if effective != current_title:
            _put_title_status(put_event, session_id, 'skipped', 'manual_title', effective)
            return
        if not effective or effective in ('Untitled', 'New Chat'):
            return
        from api import profiles as profiles_api

        with profiles_api.profile_env_for_background_worker(s, "background title", logger_override=logger):
            aux_title_configured = _aux_title_configured()
            if agent and not aux_title_configured:
                next_title, llm_status, raw_preview = _generate_llm_session_title_for_agent(agent, user_text, assistant_text)
                if not next_title and llm_status in ('llm_error', 'llm_invalid'):
                    next_title, llm_status, raw_preview = _generate_llm_session_title_via_aux(user_text, assistant_text, agent=agent, use_agent_model=True)
            else:
                next_title, llm_status, raw_preview = _generate_llm_session_title_via_aux(user_text, assistant_text)
                if not next_title and agent and llm_status in ('llm_error_aux', 'llm_invalid_aux'):
                    next_title, llm_status, raw_preview = _generate_llm_session_title_for_agent(agent, user_text, assistant_text)
        if not next_title:
            _put_title_status(put_event, session_id, 'refresh_skipped', llm_status or 'empty', effective, raw_preview)
            return
        # Skip if the new title is essentially the same (after normalization)
        normalized_current = re.sub(r'\s+', ' ', effective).strip().lower()
        normalized_new = re.sub(r'\s+', ' ', next_title).strip().lower()
        if normalized_current == normalized_new:
            _put_title_status(put_event, session_id, 'refresh_skipped', 'same_title', effective, raw_preview)
            return
        wrote_title = False
        skip_for_newer_title = False
        try:
            with edit_session(
                session_id,
                touch_updated_at=False,
                save_when=lambda _session: wrote_title,
            ) as current_session:
                effective_title = str(current_session.title or '').strip()
                # Re-check under the session owner lock: a user rename or a
                # newer automatic publication must win over this slow worker.
                if session_has_manual_title(current_session) or effective_title != current_title:
                    skip_for_newer_title = True
                else:
                    current_session.title = next_title
                    mark_session_title_generated(current_session)
                    effective_title = current_session.title
                    wrote_title = True
        except KeyError:
            return
        if skip_for_newer_title:
            _put_title_status(put_event, session_id, 'skipped', 'manual_title', effective_title)
            return
        _put_title_status(put_event, session_id, 'refreshed', llm_status, effective_title, raw_preview)
        put_event('title', {'session_id': session_id, 'title': effective_title})
        logger.info("Adaptive title refresh: session=%s new_title=%r", session_id, effective_title)
    except Exception:
        logger.debug("Background title refresh failed for session %s", session_id, exc_info=True)


def generate_session_title_for_session(session, *, prefer_latest: bool = False, agent=None) -> tuple[Optional[str], str, str]:
    """Generate a session title on demand from persisted conversation messages.

    This helper powers explicit UI title-regeneration controls. It intentionally
    does not inspect or mutate ``llm_title_generated``; callers decide whether
    replacing the current title is allowed, then persist the returned title.
    """
    messages = getattr(session, 'messages', None) or []
    if prefer_latest:
        user_text, assistant_text = _latest_exchange_snippets(messages)
    else:
        user_text, assistant_text = _first_exchange_snippets(messages)
    if not user_text:
        return None, 'empty_user_message', ''
    from api import profiles as profiles_api

    with profiles_api.profile_env_for_background_worker(session, "manual title regeneration", logger_override=logger):
        next_title, llm_status, raw_preview = _generate_llm_session_title_via_aux(user_text, assistant_text, agent=agent)
    if next_title:
        return next_title, llm_status, raw_preview
    fallback_title = _fallback_title_from_exchange(user_text, assistant_text)
    if fallback_title and not _is_generic_fallback_title(fallback_title):
        reason = f'local_summary:{llm_status}' if llm_status else 'local_summary'
        return fallback_title, reason, raw_preview
    return None, llm_status or 'empty_title', raw_preview


def _maybe_schedule_title_refresh(session, put_event, agent):
    """Check if the session is due for an adaptive title refresh and schedule it."""
    refresh_interval = _get_title_refresh_interval()
    if refresh_interval <= 0:
        return
    current_title = str(session.title or '').strip()
    if not current_title or current_title in ('Untitled', 'New Chat'):
        return
    if session_has_manual_title(session):
        return
    if not getattr(session, 'llm_title_generated', False):
        return
    exchange_count = _count_exchanges(session.messages)
    if exchange_count <= 0 or exchange_count % refresh_interval != 0:
        return
    last_u, last_a = _latest_exchange_snippets(session.messages)
    if not last_u and not last_a:
        return
    threading.Thread(
        target=_run_background_title_refresh,
        args=(session.session_id, last_u, last_a, current_title, put_event, agent),
        daemon=True,
    ).start()
