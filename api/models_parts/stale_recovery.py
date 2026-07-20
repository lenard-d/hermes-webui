"""Stale pending and state-database recovery.

Loaded behind :mod:`api.models`; use that compatibility facade in callers.
"""
# The facade seeds the original monolith namespace dynamically.  Pyflakes cannot
# see those names, while the copied implementation intentionally keeps its old
# global lookups so api.models monkeypatch seams remain effective.
# ruff: noqa: F401, F811, F821, F841, B007, B023, B904, B905
from api.models_parts._compat import seed_module_globals

seed_module_globals(globals())

def _has_compression_continuation(session) -> bool:
    """Return True when ``session`` is an archived compression parent.

    Context compression rotates the live WebUI session id: the old sidecar is
    preserved for lineage while the new child owns the running/completed turn.
    Stale-pending repair must not append an interruption marker to that old
    parent just because its stream bookkeeping disappeared after the rotation.
    """
    sid = getattr(session, 'session_id', None)
    if not sid:
        return False

    def _row_is_continuation(row) -> bool:
        if not isinstance(row, dict):
            return False
        child_sid = row.get('session_id')
        if not child_sid or child_sid == sid:
            return False
        if row.get('parent_session_id') != sid:
            return False
        # Any child row is enough evidence that this pending state belongs to a
        # compression lineage, not a dead standalone turn. The child may itself
        # temporarily carry a bad pre_compression_snapshot flag from older code;
        # do not filter it out here or the guard misses the exact regression.
        return True

    try:
        with LOCK:
            for child in SESSIONS.values():
                if getattr(child, 'session_id', None) == sid:
                    continue
                if getattr(child, 'parent_session_id', None) == sid:
                    return True
    except Exception:
        pass

    try:
        if SESSION_INDEX_FILE.exists():
            entries = json.loads(SESSION_INDEX_FILE.read_bytes())
            if isinstance(entries, list) and any(_row_is_continuation(e) for e in entries):
                return True
    except Exception:
        logger.debug("Failed to inspect session index for compression continuation", exc_info=True)

    # Index rows can lag behind rapid compression/save races. Fall back to a
    # shallow JSON metadata scan; session files write parent_session_id before
    # the messages array, so this avoids loading multi-MB transcripts.
    try:
        needle = f'"parent_session_id": "{sid}"'
        for path in SESSION_DIR.glob('*.json'):
            if path.name.startswith('_') or path.stem == sid:
                continue
            try:
                # Preserve the old read_text()[:4096] CHARACTER-prefix semantics
                # with bounded I/O: a UTF-8 char is at most 4 bytes, so 4096 chars
                # fit in <=16384 bytes. Reading bytes then slicing to 4096 chars
                # avoids a regression where a multi-byte (e.g. emoji) compression
                # summary written before parent_session_id pushes the needle past a
                # 4096-BYTE cutoff even though it was within the old 4096-CHAR one.
                head = _read_file_head(path, max_prefix_bytes=16384)[:4096]
            except OSError:
                continue
            if needle in head:
                return True
    except Exception:
        logger.debug("Failed to scan session files for compression continuation", exc_info=True)

    return False


def _repair_stale_pending(session) -> bool:
    """Recover a sidecar stuck with messages=[] and stale pending state.

    Fires only when messages is empty, pending_user_message is set,
    active_stream_id is set, the stream is no longer alive, AND the turn is
    older than _REPAIR_STALE_PENDING_GRACE_SECONDS (#1624).

    Uses a non-blocking lock acquire so a caller that already holds the
    per-session lock (e.g. retry_last, undo_last, cancel_stream) cannot
    deadlock when get_session() triggers this on a cache miss.

    Returns True if repair was applied, False otherwise.
    Must never raise — all errors are caught and logged.
    """
    # Capture the stream id seen at pre-check time; the under-lock re-check in
    # _apply_core_sync_or_error_marker uses this to detect a rotated active_stream_id
    # (e.g. context compression) or a stream that came back alive.
    _seen_stream_id = session.active_stream_id
    if (not session.pending_user_message
            or not _seen_stream_id
            or _seen_stream_id in _active_stream_ids()):
        return False
    if getattr(session, 'pre_compression_snapshot', False):
        logger.debug(
            "_repair_stale_pending: skipping pre-compression snapshot %s",
            getattr(session, 'session_id', '?'),
        )
        return False
    if _has_compression_continuation(session):
        logger.debug(
            "_repair_stale_pending: skipping compression parent %s with continuation",
            getattr(session, 'session_id', '?'),
        )
        return False

    # Grace-period guard: bail if the turn is too fresh to be a real crash.
    # Falsy pending_started_at (None, 0, missing) means "old enough" — preserve
    # legacy-data recovery semantics for sessions that pre-date the field.
    _started = getattr(session, 'pending_started_at', None)
    if _started:
        try:
            _age = time.time() - float(_started)
        except (TypeError, ValueError):
            _age = float('inf')
        if _age < _REPAIR_STALE_PENDING_GRACE_SECONDS:
            logger.debug(
                "_repair_stale_pending: skipping repair for session %s — "
                "pending_started_at age=%.1fs < %ds grace window",
                session.session_id, _age, _REPAIR_STALE_PENDING_GRACE_SECONDS,
            )
            return False
    else:
        # Treat missing/falsy pending_started_at as "old enough" (legacy data).
        _age = float('inf')

    sid = session.session_id
    if not is_safe_session_id(sid):
        return False

    try:
        profile_home = _get_profile_home(session.profile)
        core_path = profile_home / 'sessions' / f'session_{sid}.json'

        lock = _get_session_agent_lock(sid)
        # Non-blocking acquire: bail immediately if the caller already holds this
        # lock (e.g. retry_last, undo_last, cancel_stream). Blocking would deadlock
        # because _get_session_agent_lock returns a non-reentrant threading.Lock.
        if not lock.acquire(blocking=False):
            logger.debug(
                "_repair_stale_pending: lock contended, skipping repair for session %s", sid,
            )
            return False
        try:
            # Telemetry (#1624): log legitimate repair firings so the next batch
            # of user reports tells us whether the underlying race still fires
            # post-fix. Rate-limit by age (Opus pre-release SHOULD-FIX): WARNING
            # for the diagnostically valuable race window (< 5 min — actual
            # leak-path candidates that slipped past the grace guard) and DEBUG
            # for the long-tail (orphaned sidecars from prior process lifetimes)
            # so reconnect loops on stuck sessions don't flood the log.
            _DIAG_WARN_WINDOW_SECONDS = 300  # 5 min
            _age_str = ('inf' if _age == float('inf') else f'{_age:.1f}s')
            _log = logger.warning if _age < _DIAG_WARN_WINDOW_SECONDS else logger.debug
            _log(
                "_repair_stale_pending firing: session=%s stream_id=%s pending_age=%s",
                sid, _seen_stream_id, _age_str,
            )
            return _apply_core_sync_or_error_marker(
                session, core_path, stream_id_for_recheck=_seen_stream_id,
            )
        finally:
            lock.release()
    except Exception:
        logger.exception("_repair_stale_pending failed for session %s", sid)
        return False


def _sync_sidecar_from_state_db_if_newer(session) -> bool:
    """Read-side self-heal when WebUI sidecar lags Hermes state.db.

    A WebUI stream can lose its terminal ``done``/``stream_end`` path while the
    underlying agent continues writing messages to ``state.db``. In that shape
    the browser briefly shows live SSE output, but a refresh reloads the stale
    sidecar JSON and the already-produced text appears to vanish. Reconcile the
    sidecar from state.db whenever the state transcript is visibly newer than
    the sidecar, even if the sidecar still carries an ``active_stream_id``.

    This deliberately reuses the existing append-only reconciler so workspace
    prefixes, timestamp drift, compaction watermarks, and tool metadata keep the
    same semantics as normal WebUI/state.db display merging.
    """
    if session is None or getattr(session, '_loaded_metadata_only', False):
        return False
    sid = getattr(session, 'session_id', None)
    if not sid or not is_safe_session_id(sid):
        return False
    seen_stream_id = getattr(session, 'active_stream_id', None)
    has_unfinished_sidecar_turn = bool(
        seen_stream_id or getattr(session, 'pending_user_message', None)
    )
    if not has_unfinished_sidecar_turn:
        return False
    # Never reconcile while the sidecar's stream is still a LIVE in-process
    # worker. A running turn owns the final writeback (it merges the agent
    # result and clears pending state itself); racing it here would drop its
    # active_stream_id mid-run and make the normal terminal writeback skip as
    # "stale". Only self-heal once the worker is gone from both the SSE
    # (STREAMS) and worker-lifecycle (ACTIVE_RUNS) registries.
    if seen_stream_id and seen_stream_id in _active_stream_ids():
        return False
    # Registration-window grace guard (mirrors _repair_stale_pending). A turn is
    # registered in STREAMS/ACTIVE_RUNS by the worker thread a moment AFTER the
    # request handler persists active_stream_id + pending_started_at to the
    # sidecar. Within that window the stream is legitimately in flight yet not
    # yet visible in the registries, so the liveness check above would
    # mis-classify it as a dead stream. A recent pending_started_at means "still
    # starting up" — bail. This also covers cross-process / gateway turns the
    # local registries cannot see. Falsy pending_started_at (None/0/missing) is
    # treated as "old enough" so legacy/orphaned sidecars still self-heal.
    if seen_stream_id:
        _started = getattr(session, 'pending_started_at', None)
        if _started:
            try:
                _age = time.time() - float(_started)
            except (TypeError, ValueError):
                _age = float('inf')
            if _age < _REPAIR_STALE_PENDING_GRACE_SECONDS:
                return False

    try:
        state_summary = get_state_db_session_summary(
            sid,
            profile=getattr(session, 'profile', None),
        )
        state_count = int(state_summary.get('message_count') or 0)
        state_last = float(state_summary.get('last_message_at') or 0.0)
    except Exception:
        logger.debug("state.db summary check failed for session %s", sid, exc_info=True)
        return False
    if state_count <= 0:
        return False

    sidecar_messages = list(getattr(session, 'messages', None) or [])
    sidecar_count = len(sidecar_messages)
    sidecar_last = _last_message_timestamp(sidecar_messages) or 0.0

    # Fast negative (pre-lock): if state.db is not ahead by either count or
    # timestamp, do not pay for the lock. This keeps normal reads cheap.
    if state_count <= sidecar_count and state_last <= sidecar_last:
        return False

    # ── Under-lock critical section ──────────────────────────────────────────
    # The merge + sidecar write must hold the per-session lock so a concurrent
    # worker/checkpoint save can neither (a) be clobbered by a stale full-record
    # write here, nor (b) revive the stream between our liveness check and our
    # write. Non-blocking acquire: if a caller already holds the lock (retry_last,
    # undo_last, cancel_stream, the streaming worker's own finalize), bail rather
    # than deadlock — a later read will retry the self-heal.
    lock = _get_session_agent_lock(sid)
    if not lock.acquire(blocking=False):
        logger.debug(
            "state.db newer-sidecar sync: lock contended, skipping for session %s", sid,
        )
        return False
    try:
        # Re-load the authoritative on-disk session under the lock so we both
        # validate against (and write back) the very latest sidecar — never a
        # snapshot captured before the lock that could clobber a newer write.
        try:
            locked = Session.load(sid)
        except Exception:
            logger.debug(
                "state.db newer-sidecar sync: locked reload failed for session %s",
                sid, exc_info=True,
            )
            return False
        if locked is None:
            return False

        # Re-check liveness conditions against the freshly-loaded state: the
        # stream may have rotated (compression), come back alive, terminated and
        # cleared its own pending state, or had its turn finalized while we
        # waited. Any of these means there is nothing stale to repair.
        locked_stream_id = getattr(locked, 'active_stream_id', None)
        if locked_stream_id != seen_stream_id:
            return False
        if not (locked_stream_id or getattr(locked, 'pending_user_message', None)):
            return False
        if locked_stream_id and locked_stream_id in _active_stream_ids():
            return False
        if locked_stream_id:
            _lstarted = getattr(locked, 'pending_started_at', None)
            if _lstarted:
                try:
                    _lage = time.time() - float(_lstarted)
                except (TypeError, ValueError):
                    _lage = float('inf')
                if _lage < _REPAIR_STALE_PENDING_GRACE_SECONDS:
                    return False

        locked_messages = list(getattr(locked, 'messages', None) or [])
        locked_count = len(locked_messages)

        state_messages = get_state_db_session_messages(
            sid,
            profile=getattr(locked, 'profile', None),
        )
        if not state_messages:
            return False
        merged_messages = reconciled_state_db_messages_for_session(
            locked,
            state_messages=state_messages,
        )
        # The reconciler is append-only: a genuine state.db advance (output the
        # lost stream never wrote back) shows up as MORE rows than the sidecar.
        # A merged length not greater than the sidecar means nothing new to
        # recover — leave the sidecar untouched rather than rewriting in place.
        if len(merged_messages) <= locked_count:
            return False
        merged_context = reconciled_state_db_messages_for_session(
            locked,
            prefer_context=True,
            state_messages=state_messages,
        )

        # Mutate + persist the freshly-loaded, locked object. Because we hold the
        # lock and reloaded under it, this save cannot clobber a concurrent
        # writer's newer record.
        locked.messages = merged_messages
        locked.context_messages = merged_context
        locked.active_stream_id = None
        locked.pending_user_message = None
        locked.pending_attachments = []
        locked.pending_started_at = None
        locked.pending_user_source = None
        try:
            locked.save(touch_updated_at=True)
        except Exception:
            logger.debug(
                "state.db newer-sidecar sync save failed for session %s",
                sid, exc_info=True,
            )
            return False

        # Durable write succeeded — reflect the reconciled state on the caller's
        # shared/cached object so the in-flight read returns the recovered data.
        session.messages = merged_messages
        session.context_messages = merged_context
        session.active_stream_id = None
        session.pending_user_message = None
        session.pending_attachments = []
        session.pending_started_at = None
        session.pending_user_source = None
        logger.info(
            "Session %s: synced sidecar from newer state.db transcript (%d -> %d messages)",
            sid,
            locked_count,
            len(merged_messages),
        )
        return True
    finally:
        lock.release()

__all__ = ['_has_compression_continuation', '_repair_stale_pending', '_sync_sidecar_from_state_db_if_newer']
