"""Conservative LRU eviction policy for the in-process session cache."""

from __future__ import annotations

import logging
import time

import api.config as _cfg
from api.config import (
    SESSIONS, SESSIONS_MAX,
)
from .session_cache_freshness import _persisted_message_count, _session_sidecar_exists

logger = logging.getLogger(__name__)

_UNSAVED_SHELL_GRACE_S = 1800


def _session_is_evictable(s) -> bool:
    """Return True only when *s* can be safely dropped from the LRU (#4765).

    Eviction must never lose data or interrupt a live turn. A session is
    evictable ONLY when ALL of the following hold:

      * It is not streaming (no ``active_stream_id``).
      * It has no in-flight/queued turn (no ``pending_user_message`` and no
        ``pending_started_at``).
      * Its full state is already persisted to the JSON sidecar, proven by the
        on-disk ``message_count`` being at least the in-memory message count.
        A metadata-only stub is inherently backed by disk, so it is evictable.

    The persistence requirement holds even for a session with ZERO messages,
    but only for a bounded grace window. ``new_session()`` deliberately does not
    touch disk until the first message (#1171), so between "New Conversation" and
    the first send this cache is the session's ONLY copy. Evicting it there
    discards an un-recreatable shell: ``get_session()`` has no recreate path and
    raises ``KeyError``, so the very next ``/api/session/draft`` or
    ``/api/chat/start`` 404s and the session can never be started (#6083). We
    therefore protect a never-persisted empty shell while it is fresh (the user
    just opened it and is composing) OR while it has an active composer draft.

    A never-persisted empty shell that is BOTH stale (older than
    ``_UNSAVED_SHELL_GRACE_S``) AND draftless is treated as an abandoned
    "New Conversation" tab the user opened and walked away from — it becomes
    evictable again so ``sessions_cache_max`` still bounds these shells and they
    cannot accumulate without limit (a slow leak / OOM on installs that open many
    empty chats). Anything the user is actually composing persists a draft via
    ``s.save()`` on the first keystroke, so it is disk-backed and reloadable well
    before the grace window expires; the window only covers the empty-and-untouched
    gap right after "New Conversation".

    Anything else we cannot positively prove is safe stays resident. Using
    slightly more RAM for a session we are unsure about is strictly better than
    evicting an active or unsaved session (task safety invariant: a half-done
    memory fix that loses a session is worse than none).
    """
    if s is None:
        return True  # nothing to protect; let the caller drop it
    if getattr(s, 'active_stream_id', None):
        return False
    if getattr(s, 'pending_user_message', None):
        return False
    if getattr(s, 'pending_started_at', None):
        return False
    sid = getattr(s, 'session_id', None)
    if not sid:
        return False
    # Metadata-only stubs never carry unsaved messages (messages=[] by design),
    # so they are always disk-backed and safe to drop.
    if getattr(s, '_loaded_metadata_only', False):
        return True
    in_memory_count = len(getattr(s, 'messages', None) or [])
    disk_count = _persisted_message_count(sid)
    if disk_count is None:
        # disk_count is None for TWO distinct reasons: the sidecar is confirmed
        # absent (truly never persisted → this cache is the only copy), OR the
        # sidecar exists but its count could not be read this pass (transient I/O,
        # mid-write). Only the CONFIRMED-ABSENT case is eligible for grace-based
        # shell eviction; an indeterminate existing-sidecar session stays resident
        # (conservative — never grace-evict something we cannot prove is gone).
        if in_memory_count > 0:
            return False  # holds unsaved messages → never drop
        composer_draft = getattr(s, 'composer_draft', None)
        if composer_draft:
            return False  # user is composing (draft present) → keep resident
        if _session_sidecar_exists(sid) is not False:
            # Sidecar present or existence indeterminate → not a never-persisted
            # shell; do not enter the abandoned-shell grace path.
            return False
        # Confirmed never-persisted empty draftless shell. Protect it while fresh
        # (the compose window right after "New Conversation"); once stale it is an
        # abandoned tab and becomes evictable so these shells cannot accumulate
        # unbounded past the cache cap (#6083 follow-up).
        created_at = getattr(s, 'created_at', None)
        if isinstance(created_at, (int, float)):
            if (time.time() - created_at) <= _UNSAVED_SHELL_GRACE_S:
                return False  # fresh empty shell → protect the compose window
            return True  # stale, empty, draftless, never-saved → abandoned, evictable
        # No usable created_at timestamp → be conservative, keep it resident.
        return False
    if in_memory_count == 0:
        # Persisted and empty → trivially clean, nothing to lose.
        return True
    return disk_count >= in_memory_count


def _evict_sessions_over_cap(cap: int | None = None) -> int:
    """Evict clean, persisted, non-active sessions until len(SESSIONS) <= cap.

    Replaces the previous blind ``SESSIONS.popitem(last=False)`` loops (#4765).
    The blind loops could evict the least-recently-used entry even if it was
    actively streaming or held unsaved messages, risking a dropped turn or lost
    conversation. This walks the LRU from oldest to newest and removes only
    entries that ``_session_is_evictable()`` proves are safe. An evicted session
    transparently lazily reloads from its sidecar on the next ``get_session()``.

    CALLER CONTRACT: the global ``LOCK`` MUST already be held (every call site
    mutates ``SESSIONS`` under ``LOCK``). This function never acquires ``LOCK``
    or any stream lock itself, so it cannot introduce a lock-ordering deadlock.

    Returns the number of sessions evicted. If every over-cap candidate is
    active/unsaved, the cache may temporarily exceed ``cap`` — that is the
    intended safe behavior (never lose an active/unsaved session).
    """
    if cap is None:
        try:
            cap = _cfg.get_sessions_cache_max()
        except Exception:
            cap = SESSIONS_MAX
    if not isinstance(cap, int) or cap < 1:
        cap = SESSIONS_MAX if isinstance(SESSIONS_MAX, int) and SESSIONS_MAX >= 1 else 1
    evicted = 0
    # Iterate over a snapshot of ids in LRU order (oldest first). We stop as
    # soon as we are at/below the cap. Skipping a non-evictable oldest entry and
    # moving on lets us reclaim a slightly-newer clean entry instead of blocking
    # eviction entirely behind one pinned active session.
    for sid in list(SESSIONS.keys()):
        if len(SESSIONS) <= cap:
            break
        candidate = SESSIONS.get(sid)
        if _session_is_evictable(candidate):
            SESSIONS.pop(sid, None)
            evicted += 1
    if len(SESSIONS) > cap:
        logger.debug(
            "SESSIONS cache above cap (%d > %d) after eviction pass: remaining "
            "entries are active or unsaved and were preserved (#4765)",
            len(SESSIONS), cap,
        )
    return evicted
