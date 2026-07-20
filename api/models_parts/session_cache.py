"""Persisted metadata, LRU admission, and session lookup.

Loaded behind :mod:`api.models`; use that compatibility facade in callers.
"""
# The facade seeds the original monolith namespace dynamically.  Pyflakes cannot
# see those names, while the copied implementation intentionally keeps its old
# global lookups so api.models monkeypatch seams remain effective.
# ruff: noqa: F401, F811, F821, F841, B007, B023, B904, B905
from api.models_parts._compat import seed_module_globals

seed_module_globals(globals())

def _persisted_message_count(sid) -> int | None:
    """Return the on-disk message count for *sid* without a full load (#4765).

    Reads only the sidecar metadata prefix (and falls back to the sidebar
    ``_index.json`` count) so the eviction safety check stays cheap even while
    the global ``LOCK`` is held. Returns ``None`` when the sidecar is missing or
    its count cannot be determined — callers treat that as "do not evict",
    because we must never drop an in-memory session we cannot prove is on disk.
    """
    if not is_safe_session_id(sid):
        return None
    p = SESSION_DIR / f'{sid}.json'
    if not p.exists():
        return None
    try:
        prefix = _read_metadata_json_prefix(p)
        if prefix:
            parsed = json.loads(prefix)
            count = _parse_nonnegative_int(parsed.get('message_count'))
            if count is not None:
                return count
            # #5854: a legacy sidecar (scenes-before-count) yields a prefix with
            # no message_count and no modern anchor_scene_index. The _index.json
            # count can lag behind external appends, so trusting it here risks
            # under-reporting (and dropping an unsaved tail on cache-replace).
            # Consult the authoritative legacy-facts cache (populated by a full
            # load) before giving up; only return None (→ caller full-loads) on
            # a miss. This keeps clean legacy sessions LRU-evictable without
            # trusting a stale index. A MODERN prefix always carries
            # message_count, so it returns above.
            if 'anchor_scene_index' not in parsed:
                _facts = _legacy_sidecar_facts_get(sid)
                if _facts is not None:
                    cached_count = _parse_nonnegative_int(_facts.get('message_count'))
                    if cached_count is not None:
                        return cached_count
                # Cache miss (never full-loaded yet, or the facts LRU evicted
                # this entry). Returning None here would make the session
                # non-evictable (the eviction check treats an unknown disk count
                # as "do not evict"), which can let new_session() evict its own
                # unsaved session and 404 the first send. Full-parse to get the
                # authoritative count (Session.load re-caches the facts for
                # legacy files under a TOCTOU-guarded signature). Re-verify the
                # stat signature is stable across the parse so we never return a
                # count from a file that was atomically replaced mid-read; retry
                # boundedly on mismatch, then fall through to None. Bounded
                # overall: the next save() rewrites the modern layout.
                for _attempt in range(3):
                    sig_before = _sidecar_stat_signature(p)
                    try:
                        _full = Session.load(sid)
                    except Exception:
                        _full = None
                    sig_after = _sidecar_stat_signature(p)
                    if _full is None:
                        return None
                    if sig_before is not None and sig_before == sig_after:
                        return len(getattr(_full, 'messages', None) or [])
                    # File changed during the parse — the count is uncertain;
                    # retry with a fresh snapshot.
                return None
    except Exception:
        # Fall through to the index-based fallback below.
        pass
    return _parse_nonnegative_int(_lookup_index_message_count(sid))


def _persisted_session_meta_prefix(sid) -> dict | None:
    """Return the parsed metadata prefix dict for *sid*, or None on error.

    Used by ``_cached_session_lags_disk`` to compare additional fields (e.g.
    anchor scene records) cheaply against the cached in-memory session without
    paying for a full ``Session.load_metadata_only`` parse. Returns the same
    shape as ``_persisted_message_count`` callers would expect — a dict that
    only contains the metadata-prefix fields, NOT ``messages`` / ``tool_calls``.
    """
    if not is_safe_session_id(sid):
        return None
    p = SESSION_DIR / f'{sid}.json'
    if not p.exists():
        return None
    try:
        prefix = _read_metadata_json_prefix(p)
        if not prefix:
            return None
        return json.loads(prefix)
    except Exception:
        return None


def _session_sidecar_exists(sid) -> bool | None:
    """Return whether *sid*'s sidecar file exists on disk.

    True  = the sidecar is confirmed present.
    False = the sidecar is confirmed absent (a truly never-persisted session).
    None  = existence is indeterminate (unsafe id, or the stat raised).

    ``_session_is_evictable`` uses this to distinguish a genuinely
    never-persisted empty shell (safe to grace-evict once abandoned) from a
    session whose count merely could not be read this pass (stay resident).
    """
    if not is_safe_session_id(sid):
        return None
    try:
        return (SESSION_DIR / f'{sid}.json').exists()
    except OSError:
        return None


# Grace window (seconds) during which a never-persisted, empty, draftless session
# shell is protected from LRU eviction. new_session() defers the first disk write
# only in the SESSIONS cache; evicting it there permanently 404s the chat (#6083).
# After this window a still-empty, still-draftless, never-saved shell is treated as
# abandoned and becomes evictable again, so these shells can't grow unbounded past
# the cache cap. Generous (30 min) so a user composing slowly is never dropped;
# a real draft persists to disk on the first keystroke and is reloadable anyway.
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

__all__ = ['_persisted_message_count', '_persisted_session_meta_prefix', '_session_sidecar_exists', '_UNSAVED_SHELL_GRACE_S', '_session_is_evictable', '_evict_sessions_over_cap', 'cache_full_session', 'get_session']
