"""In-memory session cache, disk freshness checks, and collection loading."""

from __future__ import annotations

import collections
import copy
import datetime
import hashlib
import inspect
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from contextlib import closing, contextmanager
from importlib.util import find_spec
from pathlib import Path

import api.config as _cfg
from api.config import (
    DEFAULT_MODEL, DEFAULT_WORKSPACE, HOME, LOCK, SESSIONS, SESSIONS_MAX,
    SESSION_DIR, SESSION_INDEX_FILE, get_effective_default_model,
)
from api.workspace import get_last_workspace
from .records import (
    Session,
    _active_stream_ids,
    _clear_webui_deleted_session_tombstone,
    _clear_webui_zero_message_orphan_tombstone,
    _disk_scene_fingerprint,
    _legacy_sidecar_facts_get,
    _lookup_index_message_count,
    _message_role,
    _message_timestamp,
    _parse_nonnegative_int,
    _persisted_session_ids_snapshot,
    _read_metadata_json_prefix,
    _sidecar_stat_signature,
    is_safe_session_id,
)
from .state_db import (  # noqa: F401 - compatibility re-exports
    get_state_db_session_messages,
    get_state_db_session_summary,
)
from .message_identity import _message_content_text
from .pending_recovery import (
    _repair_stale_pending,
    _session_has_pending_journal_retry,
    _sync_sidecar_from_state_db_if_newer,
    _try_retry_journal_recovery_in_place,
)
from .process_wakeup import _get_profile_home
from .reconciliation import reconciled_state_db_messages_for_session

logger = logging.getLogger(__name__)

def _last_non_tool_role(messages) -> str:
    if not isinstance(messages, list):
        return ''
    for message in reversed(messages):
        role = _message_role(message)
        if role and role != 'tool':
            return role
    return ''


def _last_non_tool_message(messages):
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        role = _message_role(message)
        if role and role != 'tool':
            return message
    return None


def _inactive_cache_tail_needs_disk_check(cached) -> bool:
    if cached is None:
        return False
    if getattr(cached, 'active_stream_id', None) or getattr(cached, 'pending_user_message', None):
        return False
    return _last_non_tool_role(getattr(cached, 'messages', None) or []) == 'user'


def _cache_has_stale_unsaved_user_tail(cached, disk_session) -> bool:
    """Return True when an inactive cached session has an unsaved user tail.

    A completed turn is saved to the sidecar before the browser reloads it.  In
    rare compaction/reconnect paths the in-process cache can retain a recovered
    or optimistic user row after the saved assistant tail even though the row was
    never persisted.  If /api/session serves that cache entry, the visible
    transcript appears to end on the old prompt and the saved assistant answer
    looks missing until a fork/reload resets the cache.
    """
    if cached is None or disk_session is None:
        return False
    if getattr(cached, 'active_stream_id', None) or getattr(cached, 'pending_user_message', None):
        return False
    cached_messages = getattr(cached, 'messages', None) or []
    disk_messages = getattr(disk_session, 'messages', None) or []
    if _last_non_tool_role(cached_messages) != 'user':
        return False
    if _last_non_tool_role(disk_messages) != 'assistant':
        return False
    if len(cached_messages) < len(disk_messages):
        return True
    if len(cached_messages) == len(disk_messages):
        # Same-length divergence is still stale: a completed assistant turn can
        # be persisted through a sibling Session object while this inactive LRU
        # entry still ends on the optimistic/recovered user row.
        #
        # Keep this narrow: only evict when the shared prefix is the same and
        # the cached user tail is not newer than the persisted assistant.  A
        # genuine just-submitted user message can exist briefly before the
        # stream id is attached, and that must not be replaced by older disk
        # state.
        cached_tail = _last_non_tool_message(cached_messages)
        disk_tail = _last_non_tool_message(disk_messages)
        cached_prefix = [
            (_message_role(message), _message_content_text(message))
            for message in cached_messages[:-1]
        ]
        disk_prefix = [
            (_message_role(message), _message_content_text(message))
            for message in disk_messages[:-1]
        ]
        if cached_prefix != disk_prefix:
            return False
        cached_tail_ts = _message_timestamp(cached_tail)
        disk_tail_ts = _message_timestamp(disk_tail)
        if cached_tail_ts is not None and disk_tail_ts is not None and cached_tail_ts > disk_tail_ts:
            return False
        return True

    cached_tail = _last_non_tool_message(cached_messages)
    previous_disk_user = None
    for message in reversed(disk_messages):
        if _message_role(message) == 'user':
            previous_disk_user = message
            break
    if previous_disk_user is None:
        return False

    # Only drop tails that look like a duplicated optimistic/recovered user row.
    # A genuinely new concurrent user edit must stay in memory so stale-session
    # guards can report and preserve it.
    return _message_content_text(cached_tail) == _message_content_text(previous_disk_user)
def _anchor_scene_record_keys(session) -> set[str]:
    records = getattr(session, 'anchor_activity_scenes', None)
    if not isinstance(records, dict):
        return set()
    return {str(key) for key, value in records.items() if key and isinstance(value, dict)}


def _anchor_scene_records_updated_at(session) -> float:
    records = getattr(session, 'anchor_activity_scenes', None)
    if not isinstance(records, dict):
        return 0.0
    latest = 0.0
    for record in records.values():
        if not isinstance(record, dict):
            continue
        try:
            updated_at = float(record.get('updated_at') or 0)
        except (TypeError, ValueError):
            updated_at = 0.0
        if updated_at > latest:
            latest = updated_at
    return latest


def _session_scene_keys(session) -> set[str]:
    """Scene keys for a session object, fingerprint-aware (#5854).

    The ``_anchor_scene_index`` fingerprint is authoritative ONLY on a
    metadata-only stub (whose full ``anchor_activity_scenes`` are not
    materialized because they serialize after ``messages``). A FULLY-LOADED
    session carries a load-time fingerprint that goes stale the moment its
    records are mutated in place (the scene-persist path does exactly that
    without refreshing it), so for a full session we MUST read the real records.
    """
    if getattr(session, '_loaded_metadata_only', False):
        index = getattr(session, '_anchor_scene_index', None)
        if isinstance(index, dict):
            return {str(k) for k in index}
    return _anchor_scene_record_keys(session)


def _session_scene_updated_at(session) -> float:
    """Max scene ``updated_at`` for a session object, fingerprint-aware (#5854).

    Fingerprint is authoritative only on a metadata-only stub; a fully-loaded
    session always compares its real records (see ``_session_scene_keys``).
    """
    if getattr(session, '_loaded_metadata_only', False):
        index = getattr(session, '_anchor_scene_index', None)
        if isinstance(index, dict):
            latest = 0.0
            for v in index.values():
                try:
                    fv = float(v or 0)
                except (TypeError, ValueError):
                    fv = 0.0
                if fv > latest:
                    latest = fv
            return latest
    return _anchor_scene_records_updated_at(session)


def _cached_session_lags_disk(cached) -> bool:
    """Return True when a cached full session is older than its sidecar.

    Active/reconnect paths can update the persisted sidecar through another
    Session object while the LRU cache still holds an older object for the same
    id. Serving the cache then makes recent assistant results disappear from
    GET /api/session even though disk and _index.json are correct. Compare only
    cheap metadata here; full reload happens only if disk is strictly ahead.

    perf(webui/session-load-latency) cheap-first ordering: the function used to
    call Session.load_metadata_only(sid) on every cache hit, which parses the
    full sidecar JSON (~15-20ms even for 1.3MB sidecars on Celeron+ eMMC).
    For draft auto-saves that hit get_session() on every keystroke debounce
    (every ~400ms while typing), that 15-20ms multiplied out to ~75% of the
    request's wall time on the Chromebook. We now do a single fast check
    first: read only the JSON metadata prefix to compare message counts.
    The full Session.load_metadata_only() and its anchor-scene comparisons
    only run when the count check is inconclusive or when disk appears to be
    ahead of cache.
    """
    if cached is None:
        return False
    sid = getattr(cached, 'session_id', None)
    if not sid:
        return False
    cached_count = len(getattr(cached, 'messages', None) or [])
    # Fast path: prefix read of just the metadata header.
    disk_count = _persisted_message_count(sid)
    if disk_count is not None:
        if disk_count > cached_count:
            return True
        # Disk is at most as far as cache. Even when counts match, anchor scene
        # records can advance independently (api/routes.py saves a session
        # with `s.save(touch_updated_at=False, skip_index=True)` after editing
        # only the scene dict; message_count is len(messages) so it stays the
        # same). Greptile flagged this in PR review. Cheaply check the disk's
        # scene records from the same prefix we already read.
        cached_scenes = getattr(cached, 'anchor_activity_scenes', None) or {}
        if not isinstance(cached_scenes, dict):
            cached_scenes = {}
        # Track whether the cheap scene check was inconclusive — when it is,
        # we must fall through to the full metadata comparison instead of
        # returning False for inactive sessions with matching counts.
        _scene_check_inconclusive = False
        if cached_scenes:
            disk_meta_quick = _persisted_session_meta_prefix(sid)
            # #5854: modern prefixes carry only the anchor_scene_index
            # fingerprint (keys + updated_at), not the full scene bodies (those
            # now serialize after `messages`). _disk_scene_fingerprint resolves
            # the (keys, max_updated_at) signal from either the modern
            # fingerprint or a legacy file's inline bodies, and returns None
            # when the prefix carries NEITHER (a legacy large-scene file whose
            # scenes fall after the prefix) so we fall through rather than
            # assuming "no scenes".
            disk_fp = _disk_scene_fingerprint(disk_meta_quick) if disk_meta_quick is not None else None
            if disk_fp is not None:
                disk_keys, disk_latest = disk_fp
                if disk_keys:
                    # Directional: only reload when disk is strictly ahead
                    # of cache. Mirror master's subset comparison — cache
                    # that is ahead of disk must NOT force a reload, or
                    # un-persisted scene data is silently dropped.
                    cached_keys = _anchor_scene_record_keys(cached)
                    if not disk_keys.issubset(cached_keys):
                        return True
                    # Same key set (or disk is subset): check the latest
                    # updated_at timestamp.
                    if disk_latest > _anchor_scene_records_updated_at(cached):
                        return True
                # disk has no scenes, cache does -> cache is ahead; keep it.
            else:
                # Can't cheaply verify scene freshness from disk (prefix carried
                # neither fingerprint nor inline scenes, or the prefix read
                # failed while _persisted_message_count succeeded via index
                # fallback). Fall through to the full metadata load so we don't
                # serve stale scenes on the next equal-count inactive path.
                # Greptile P1.
                _scene_check_inconclusive = True
        else:
            # Cached session has no scene records. Check if disk has gained
            # the first scene record — without this the fast-path would miss
            # a newly persisted scene and return the stale cache. Greptile P1.
            disk_meta_quick = _persisted_session_meta_prefix(sid)
            disk_fp = _disk_scene_fingerprint(disk_meta_quick) if disk_meta_quick is not None else None
            if disk_fp is not None:
                disk_keys, _disk_latest = disk_fp
                if disk_keys:
                    return True
            else:
                # Prefix read failed / carried no scene signal (may still
                # succeed via index fallback for message count). Mark
                # inconclusive so we fall through to the full metadata
                # comparison instead of returning False with stale cache.
                # Greptile P1 (discussion_r3548650345).
                _scene_check_inconclusive = True
        if getattr(cached, 'active_stream_id', None) or getattr(cached, 'pending_user_message', None):
            # Active session: messages may be in flight; fall through to the
            # full check to be safe.
            pass
        elif _scene_check_inconclusive:
            # Could not cheaply verify scene freshness from disk; fall through
            # to the full metadata comparison rather than returning False
            # (which would serve a potentially stale cache). Greptile P1.
            pass
        else:
            # Inactive session, count matches, scene records match — cache is
            # at parity with disk.
            return False
    try:
        disk_meta = Session.load_metadata_only(sid)
    except Exception:
        return False
    if disk_meta is None:
        return False
    if disk_count is None:
        disk_count = _parse_nonnegative_int(getattr(disk_meta, '_metadata_message_count', None))
        if disk_count is None:
            disk_count = _lookup_index_message_count(sid)
        if disk_count is not None and disk_count > cached_count:
            return True
    if not getattr(cached, 'active_stream_id', None) and not getattr(cached, 'pending_user_message', None):
        # #5854: disk_meta is a metadata-only stub whose scenes now live after
        # `messages` and so are NOT materialized on it — read its scene freshness
        # from the _anchor_scene_index fingerprint. `cached` is ALWAYS a full
        # in-memory session (get_session never caches metadata-only stubs), and
        # its records are mutated in place by the scene-persist path without
        # refreshing the load-time fingerprint — so the cached side must read the
        # REAL records (master parity), never the fingerprint, or a parity cache
        # looks disk-behind after a scene write and forces a spurious full reload.
        #
        # A LEGACY stub (no anchor_scene_index fingerprint AND scenes not in the
        # prefix) carries no scene signal at all — comparing it blind would miss
        # a genuine disk-ahead scene change (stale worklog served). Full-load the
        # sidecar once to compare real scene records; the next save() rewrites
        # the modern layout so this legacy full-load doesn't recur.
        if (
            getattr(disk_meta, '_loaded_metadata_only', False)
            and getattr(disk_meta, '_anchor_scene_index', None) is None
        ):
            try:
                disk_full = Session.load(sid)
            except Exception:
                disk_full = None
            if disk_full is not None:
                disk_meta = disk_full
        cached_scene_keys = _anchor_scene_record_keys(cached)
        disk_scene_keys = _session_scene_keys(disk_meta)
        if disk_scene_keys and not disk_scene_keys.issubset(cached_scene_keys):
            return True
        if (
            disk_scene_keys
            and _session_scene_updated_at(disk_meta) > _anchor_scene_records_updated_at(cached)
        ):
            return True
    return False


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


_COMPRESSION_RECOVERY_PROFILE_UNSET = object()


def _compression_recovery_child_matches(
    session,
    source_session_id: str,
    action: str,
    source_profile=_COMPRESSION_RECOVERY_PROFILE_UNSET,
) -> bool:
    if source_profile is not _COMPRESSION_RECOVERY_PROFILE_UNSET:
        try:
            from api.profiles import profiles_match as _profiles_match
        except (ImportError, AttributeError):
            logger.debug("Failed to profile-check compression recovery session", exc_info=True)
            return False
        if not _profiles_match(getattr(session, "profile", None), source_profile):
            return False
    return (
        str(getattr(session, "compression_recovery_source_session_id", "") or "").strip() == source_session_id
        and str(getattr(session, "compression_recovery_action", "") or "").strip() == action
    )


def find_compression_recovery_session(
    source_session_id: str,
    action: str,
    source_profile=_COMPRESSION_RECOVERY_PROFILE_UNSET,
):
    """Return an existing focused recovery child for ``source_session_id``.

    The recovery-start endpoint is a retryable UI action. A persisted marker on
    the child session makes double-clicks, repeated calls, and cache reloads
    converge on the same continuation instead of creating duplicate siblings.
    """

    source_sid = str(source_session_id or "").strip()
    recovery_action = str(action or "").strip()
    if not source_sid or not recovery_action:
        return None

    matches = []
    seen_ids: set[str] = set()
    try:
        with LOCK:
            memory_sessions = list(SESSIONS.values())
        for session in memory_sessions:
            sid = str(getattr(session, "session_id", "") or "").strip()
            if sid:
                seen_ids.add(sid)
            if _compression_recovery_child_matches(session, source_sid, recovery_action, source_profile):
                matches.append(session)
    except Exception:
        logger.debug("Failed to scan cached compression recovery sessions", exc_info=True)

    try:
        persisted_ids = _persisted_session_ids_snapshot()
    except Exception:
        persisted_ids = frozenset()
    for sid in persisted_ids:
        if sid in seen_ids:
            continue
        try:
            meta = Session.load_metadata_only(sid)
        except Exception:
            logger.debug("Failed to inspect compression recovery session %s", sid, exc_info=True)
            continue
        if not meta or not _compression_recovery_child_matches(meta, source_sid, recovery_action, source_profile):
            continue
        try:
            matches.append(get_session(sid))
        except Exception:
            matches.append(meta)

    if not matches:
        return None

    def _sort_key(session):
        try:
            created_at = float(getattr(session, "created_at", 0) or 0)
        except (TypeError, ValueError):
            created_at = 0.0
        try:
            updated_at = float(getattr(session, "updated_at", 0) or 0)
        except (TypeError, ValueError):
            updated_at = 0.0
        return (created_at, updated_at, str(getattr(session, "session_id", "") or ""))

    return sorted(matches, key=_sort_key)[0]


def _profile_default_model_state(profile=None):
    """Return the default model/provider configured for *profile*."""
    default_model = ""
    default_provider = None
    try:
        from api.profiles import get_hermes_home_for_profile
        config_path = Path(get_hermes_home_for_profile(profile)) / "config.yaml"
        config_data = _cfg._load_yaml_config_file(config_path)
    except Exception:
        config_data = {}

    model_cfg = config_data.get("model", {}) if isinstance(config_data, dict) else {}
    if isinstance(model_cfg, str):
        default_model = model_cfg.strip()
    elif isinstance(model_cfg, dict):
        default_model = str(model_cfg.get("default") or "").strip()
        default_provider = str(model_cfg.get("provider") or "").strip() or None

    return default_model or get_effective_default_model(), default_provider


def new_session(workspace=None, model=None, profile=None, model_provider=None, project_id=None, worktree_info=None, enabled_toolsets=None):
    """Create a new in-memory session.

    The session lives in the SESSIONS dict only — no disk write happens until
    the first message is appended (#1171 follow-up).  This avoids the
    "ghost Untitled session on disk" pile-up that occurred when users clicked
    New Conversation, reloaded the page, or completed onboarding without ever
    sending a message.  Subsequent code paths that populate state immediately
    (btw / background agent at api/routes.py) call ``s.save()`` themselves
    after setting title/messages, and ``_handle_chat_start`` saves the
    session as soon as the user actually sends a message — both are the
    natural first-write moments for a real session.

    Crash-safety: if the process exits between session creation and first
    message, the session is lost.  Since it had no messages, there is
    nothing to lose.  Worktree-backed sessions are the exception: they are
    saved immediately because creating the session also creates real
    filesystem state that must remain discoverable after restart.

    *profile* — when supplied by the caller (e.g. from the request body sent
    by the active browser tab), it is used directly so that concurrent clients
    on different profiles don't fight over a shared process-global.  If not
    supplied, we fall back to the process-level active profile (the pre-#798
    behaviour, preserved for calls that originate outside a request context).
    """
    if profile is None:
        # Fallback: read process-level global (single-client or startup path)
        try:
            from api.profiles import get_active_profile_name
            profile = get_active_profile_name()
        except ImportError:
            profile = None
    if model:
        effective_model = model
        effective_model_provider = model_provider
    else:
        effective_model, effective_model_provider = _profile_default_model_state(profile)
        if model_provider:
            effective_model_provider = model_provider

    wt = worktree_info if isinstance(worktree_info, dict) else None
    workspace_path = (wt.get('path') if wt and wt.get('path') else workspace) if wt else workspace
    s = Session(
        workspace=workspace_path or get_last_workspace(),
        model=effective_model,
        model_provider=effective_model_provider,
        profile=profile,
        project_id=project_id,
        personality=None,
        worktree_path=wt.get('path') if wt else None,
        worktree_branch=wt.get('branch') if wt else None,
        worktree_repo_root=wt.get('repo_root') if wt else None,
        worktree_created_at=wt.get('created_at') if wt else None,
        enabled_toolsets=enabled_toolsets,
    )
    # #4985: defensive — auto-generated uuids don't collide with the
    # tombstone, but if a future caller ever passes an explicit id that
    # was previously pruned, clear the entry so the new session isn't
    # shadowed on the next poll. Wrapped because a tombstone failure
    # must never block new-session creation.
    try:
        _clear_webui_zero_message_orphan_tombstone(s.session_id)
        _clear_webui_deleted_session_tombstone(s.session_id)
    except Exception:
        logger.debug(
            "Failed to clear webui tombstone for %s",
            s.session_id,
            exc_info=True,
        )
    with LOCK:
        SESSIONS[s.session_id] = s
        SESSIONS.move_to_end(s.session_id)
        _evict_sessions_over_cap()  # #4765: safe LRU eviction (never active/unsaved)
    if wt:
        s.save()
    return s
