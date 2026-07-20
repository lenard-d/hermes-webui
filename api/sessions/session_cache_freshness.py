"""Disk freshness checks for cached full-session records."""

from __future__ import annotations

import json
import logging

from api.config import (
    SESSION_DIR,
)
from .records import (
    Session,
    _disk_scene_fingerprint,
    _legacy_sidecar_facts_get,
    _lookup_index_message_count,
    _message_role,
    _message_timestamp,
    _parse_nonnegative_int,
    _read_metadata_json_prefix,
    _sidecar_stat_signature,
    is_safe_session_id,
)
from .message_identity import _message_content_text

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
