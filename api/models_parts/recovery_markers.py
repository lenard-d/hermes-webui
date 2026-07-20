"""Core recovery-marker application.

Loaded behind :mod:`api.models`; use that compatibility facade in callers.
"""
# The facade seeds the original monolith namespace dynamically.  Pyflakes cannot
# see those names, while the copied implementation intentionally keeps its old
# global lookups so api.models monkeypatch seams remain effective.
# ruff: noqa: F401, F811, F821, F841, B007, B023, B904, B905
from api.models_parts._compat import seed_module_globals

seed_module_globals(globals())

def _apply_core_sync_or_error_marker(
    session,
    core_path,
    stream_id_for_recheck=None,
    *,
    require_stream_dead=True,
    touch_updated_at=True,
) -> bool:
    """Inner repair logic. Must be called with the per-session lock already held.

    Re-checks session state under the lock, then either syncs messages from the
    core transcript (if present and non-empty) or restores the pending user
    message as a recovered user turn and appends an error marker.

    stream_id_for_recheck: when provided, repair bails if session.active_stream_id
    changed (e.g. context compression rotated it).  The cache-miss repair path
    also requires the stream to be absent from active streams; the streaming
    thread's final fallback passes require_stream_dead=False because it runs
    before its own stream is removed from STREAMS.

    Returns True if repair was applied, False if the re-check bailed out.
    Must never raise — caller is responsible for exception handling.
    """
    sid = session.session_id
    # Bail if pending is unset — nothing to repair.
    if not session.pending_user_message:
        return False
    if stream_id_for_recheck is not None:
        # Bail if active_stream_id rotated between the pre-lock check and now.
        # Cache-miss repair must also skip if the stream is alive again, but the
        # streaming thread's final fallback runs before removing its own stream
        # from STREAMS and must be allowed to repair that same active stream.
        if session.active_stream_id != stream_id_for_recheck:
            return False
        if require_stream_dead and session.active_stream_id in _active_stream_ids():
            return False

    # When messages is already non-empty, do not overwrite history from any core
    # transcript. The pending user turn may still be the only durable copy of a
    # prompt submitted just before a server restart, so materialize it before
    # clearing runtime stream state.
    if len(session.messages) != 0:
        _recovered_ts = int(time.time())
        if isinstance(session.pending_started_at, (int, float)) and session.pending_started_at > 0:
            _recovered_ts = int(session.pending_started_at)
        _already_checkpointed = _message_matches_pending_checkpoint(
            session.messages[-1],
            session.pending_user_message,
            _recovered_ts,
            session.pending_user_source,
            session.pending_attachments,
        )
        _tail_user_already_checkpointed = _already_checkpointed or _message_matches_pending_text(
            session.messages[-1],
            session.pending_user_message,
        )
        _stream_id = stream_id_for_recheck or session.active_stream_id
        _pending_started_at = session.pending_started_at
        if _run_journal_terminal_state(session, _stream_id) == 'completed':
            if not (_already_checkpointed or _latest_user_matches_pending_text(session.messages, session.pending_user_message)):
                _append_recovered_pending_turn(session, timestamp=_recovered_ts)
            _append_journaled_partial_output(
                session,
                _stream_id,
                dedupe_existing=True,
            )
            session.active_stream_id = None
            session.pending_user_message = None
            session.pending_attachments = []
            session.pending_started_at = None
            session.pending_user_source = None
            session.save(touch_updated_at=touch_updated_at)
            logger.info(
                "Session %s: cleared stale pending state for completed stream %s without error marker",
                sid,
                _stream_id,
            )
            return True
        if not _tail_user_already_checkpointed:
            _append_recovered_pending_turn(session, timestamp=_recovered_ts)
        else:
            recovered = {
                'role': 'user',
                'content': session.pending_user_message,
                'timestamp': _recovered_ts,
                '_recovered': True,
            }
            pending_source = getattr(session, 'pending_user_source', None)
            if pending_source and pending_source != 'webui':
                recovered['_source'] = pending_source
            if session.pending_attachments:
                recovered['attachments'] = list(session.pending_attachments)
            _append_recovered_turn_to_context(session, recovered)
        recovered_output = _append_journaled_partial_output(
            session,
            _stream_id,
        )
        session.active_stream_id = None
        session.pending_user_message = None
        session.pending_attachments = []
        session.pending_started_at = None
        session.pending_user_source = None
        session.messages.append(
            _build_recovery_marker_with_retry_hook(
                recovered_output=recovered_output,
                stream_id=_stream_id,
                pending_started_at=_pending_started_at,
            )
        )
        session.save(touch_updated_at=touch_updated_at)
        logger.info(
            "Session %s: recovered pending user turn (messages non-empty), added error marker",
            sid,
        )
        return True

    # ── messages *is* empty ─ full repair ─────────────────────────────────

    if core_path.exists():
        with open(core_path, encoding='utf-8') as f:
            core = json.load(f)
        core_messages = core.get('messages', [])
        if core_messages:
            _stream_id = stream_id_for_recheck or session.active_stream_id
            session.messages = core_messages
            session.tool_calls = core.get('tool_calls', [])
            for field in ('input_tokens', 'output_tokens', 'estimated_cost'):
                if core.get(field) is not None:
                    setattr(session, field, core[field])
            _pending_text = _normalize_journal_recovery_text(session.pending_user_message)
            _recovered_ts = int(time.time())
            if isinstance(session.pending_started_at, (int, float)) and session.pending_started_at > 0:
                _recovered_ts = int(session.pending_started_at)
            _already_checkpointed = _message_matches_pending_checkpoint(
                session.messages[-1] if session.messages else None,
                session.pending_user_message,
                _recovered_ts,
                session.pending_user_source,
                session.pending_attachments,
            )
            _tail_user_already_checkpointed = _already_checkpointed or _message_matches_pending_text(
                session.messages[-1] if session.messages else None,
                session.pending_user_message,
            )
            if (
                _pending_text
                and not _tail_user_already_checkpointed
                and _run_journal_has_visible_output(session, _stream_id)
            ):
                _append_recovered_pending_turn(session, timestamp=_recovered_ts)
            recovered_output = _append_journaled_partial_output(
                session,
                _stream_id,
                dedupe_existing=True,
            )
            _pending_started_at = session.pending_started_at
            session.active_stream_id = None
            session.pending_user_message = None
            session.pending_attachments = []
            session.pending_started_at = None
            session.pending_user_source = None
            if recovered_output:
                session.messages.append(
                    _interrupted_recovery_marker(
                        recovered_output=True,
                        stream_id=_stream_id,
                        pending_started_at=_pending_started_at,
                    )
                )
            # NOTE: when the core transcript was synced in but the run journal
            # is not yet visible, intentionally do NOT append a lazy-retry
            # marker here. In this branch the canonical history is the core
            # transcript itself (which has already been written to s.messages
            # above) and the marker is purely advisory — the existing contract
            # is "marker only when there is a recovered partial turn to
            # annotate". Adding a pending-retry marker on every empty-journal
            # core-sync would surface a spurious "reload to retry" banner on
            # sessions whose journal is legitimately absent (e.g. archived
            # streams). The first and third branches handle the lost-response
            # case where the marker is the only signal the user gets.
            session.save(touch_updated_at=touch_updated_at)
            logger.info(
                "Session %s: synced %d messages from core transcript%s",
                sid,
                len(core_messages),
                " and recovered journaled output" if recovered_output else "",
            )
            return True

    # Core missing or empty — restore the pending user message as a recovered
    # user turn (preserving the draft), then append an error marker.
    if session.pending_user_message:
        # Use the original send time if available so the recovered turn
        # appears in the correct chronological position.
        _recovered_ts = int(time.time())
        if isinstance(session.pending_started_at, (int, float)) and session.pending_started_at > 0:
            _recovered_ts = int(session.pending_started_at)
        _append_recovered_pending_turn(session, timestamp=_recovered_ts)
    recovered_output = _append_journaled_partial_output(
        session,
        stream_id_for_recheck or session.active_stream_id,
    )
    _stream_id = stream_id_for_recheck or session.active_stream_id
    _pending_started_at = session.pending_started_at
    session.active_stream_id = None
    session.pending_user_message = None
    session.pending_attachments = []
    session.pending_started_at = None
    session.pending_user_source = None
    session.messages.append(
        _build_recovery_marker_with_retry_hook(
            recovered_output=recovered_output,
            stream_id=_stream_id,
            pending_started_at=_pending_started_at,
        )
    )
    session.save(touch_updated_at=touch_updated_at)
    logger.info("Session %s: no core transcript found, added error marker", sid)
    return True


# ── _repair_stale_pending grace period (#1624) ─────────────────────────────
#
# Defense-in-depth against a narrow race between the streaming thread clearing
# pending_user_message and STREAMS.pop(stream_id). Without this guard, any
# fast turn (e.g. command approval) that exits the thread before the on-disk
# pending clear has flushed gets misdiagnosed as a crashed turn, producing a
# spurious "Response interrupted." marker.
#
# 30s covers the worst-case post-loop persistence window: LLM finishing a tool
# batch + lock contention with the checkpoint thread + a multi-MB session.save.
# A legitimately crashed turn whose pending_started_at is < 30s old will not
# repair on the first get_session() call, but WILL repair on the next call
# after the grace period elapses (typically the user's next interaction).
#
# Missing/falsy pending_started_at (legacy sidecars from before that field
# existed, or any path that forgot to set it) is treated as "old enough" so
# repair still recovers them — preserves current behavior for legacy data.
_REPAIR_STALE_PENDING_GRACE_SECONDS = 30

__all__ = ['_apply_core_sync_or_error_marker', '_REPAIR_STALE_PENDING_GRACE_SECONDS']
