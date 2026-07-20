"""Full-session cache publication and lazy-loading repository."""

from __future__ import annotations

import logging

from api.config import (
    LOCK, SESSIONS,
)
from .records import (
    Session,
    _active_stream_ids,
)
from .pending_recovery.journal_retry import (
    _session_has_pending_journal_retry,
    _try_retry_journal_recovery_in_place,
)
from .pending_recovery.sidecar_recovery import _repair_stale_pending
from .pending_recovery.state_db_recovery import _sync_sidecar_from_state_db_if_newer
from .session_cache_eviction import _evict_sessions_over_cap
from .session_cache_freshness import (
    _cache_has_stale_unsaved_user_tail,
    _cached_session_lags_disk,
    _inactive_cache_tail_needs_disk_check,
)

logger = logging.getLogger(__name__)


def cache_full_session(sid: str, session):
    """Publish one fully loaded session into the process LRU.

    This is the single cache-publication Interface used by persistence owners.
    It rejects cross-session objects and metadata-only projections so a caller
    cannot poison later full-session reads.
    """
    sid = str(sid or "")
    actual_sid = str(getattr(session, "session_id", "") or "")
    if not sid or actual_sid != sid:
        raise ValueError("session cache key does not match the session object")
    if getattr(session, "_loaded_metadata_only", False):
        raise ValueError("metadata-only sessions cannot enter the full-session cache")
    with LOCK:
        SESSIONS[sid] = session
        SESSIONS.move_to_end(sid)
        _evict_sessions_over_cap()
    return session


def get_session(sid, metadata_only=False):
    """Load a session, optionally with metadata only (skipping the messages array).

    Metadata-only loads intentionally do not populate the full-session cache.
    Otherwise a later full load could return a compact object with an empty
    messages list. Use this when you only need compact() metadata and not the
    actual message history (e.g., for fast sidebar switching).
    """
    with LOCK:
        cached = SESSIONS.get(sid)
        if cached is not None:
            SESSIONS.move_to_end(sid)  # LRU: mark as recently used
    if cached is not None:
        # Defensive cache ownership check: compression/continuation and recovery
        # paths can temporarily juggle Session objects across lineage ids.  A
        # stale object stored under the wrong key makes GET /api/session return
        # a different transcript than the requested sid, which looks exactly
        # like a disappeared session.  Evict instead of trusting the LRU.
        if str(getattr(cached, 'session_id', '') or '') != str(sid):
            logger.warning(
                "evicting mismatched cached session: requested %s but cached object is %s",
                sid,
                getattr(cached, 'session_id', None),
            )
            with LOCK:
                if SESSIONS.get(sid) is cached:
                    SESSIONS.pop(sid, None)
            cached = None
    if cached is not None:
        if not metadata_only and _cached_session_lags_disk(cached):
            try:
                disk_session = Session.load(sid)
                with LOCK:
                    SESSIONS[sid] = disk_session
                    SESSIONS.move_to_end(sid)
                cached = disk_session
            except Exception:
                logger.debug(
                    "cached session disk-freshness check failed for session %s",
                    sid, exc_info=True,
                )
        if not metadata_only and _inactive_cache_tail_needs_disk_check(cached):
            try:
                disk_session = Session.load(sid)
                if _cache_has_stale_unsaved_user_tail(cached, disk_session):
                    with LOCK:
                        SESSIONS[sid] = disk_session
                        SESSIONS.move_to_end(sid)
                    cached = disk_session
            except Exception:
                logger.debug(
                    "stale cached user-tail check failed for session %s",
                    sid, exc_info=True,
                )
        if not metadata_only and _session_has_pending_journal_retry(cached):
            try:
                _try_retry_journal_recovery_in_place(cached)
            except Exception:
                logger.debug(
                    "lazy journal-retry failed on cache hit for session %s",
                    sid, exc_info=True,
                )
        if not metadata_only:
            try:
                _sync_sidecar_from_state_db_if_newer(cached)
            except Exception:
                logger.debug(
                    "state.db newer-sidecar sync failed on cache hit for session %s",
                    sid, exc_info=True,
                )
        return cached
    if metadata_only:
        s = Session.load_metadata_only(sid)
        if s:
            return s
    else:
        s = Session.load(sid)
    if s:
        with LOCK:
            SESSIONS[sid] = s
            SESSIONS.move_to_end(sid)
            _evict_sessions_over_cap()  # #4765: safe LRU eviction (never active/unsaved)
        if not metadata_only:
            try:
                synced_from_state = _sync_sidecar_from_state_db_if_newer(s)
                repaired = False if synced_from_state else _repair_stale_pending(s)
                # If the stale-pending repair did not fire but the session
                # already carries a pending-journal-retry marker (e.g. set on
                # a previous repair pass), give the lazy-retry path one
                # chance to self-heal on this read.
                if not repaired and not synced_from_state and _session_has_pending_journal_retry(s):
                    try:
                        _try_retry_journal_recovery_in_place(s)
                    except Exception:
                        logger.debug(
                            "lazy journal-retry failed on cold load for session %s",
                            sid, exc_info=True,
                        )
                # If repair had to bail because the per-session lock was held,
                # do not pin the still-stale sidecar in the LRU cache forever.
                # Leaving it cached would prevent future get_session() calls from
                # re-entering the cache-miss repair path after the lock holder exits.
                if not repaired and (len(s.messages) == 0
                        and s.pending_user_message
                        and s.active_stream_id
                        and s.active_stream_id not in _active_stream_ids()):
                    with LOCK:
                        if SESSIONS.get(sid) is s:
                            SESSIONS.pop(sid, None)
            except Exception:
                pass  # repair is best-effort
        return s
    raise KeyError(sid)
