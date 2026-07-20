"""Journal partial-output reconstruction.

Loaded behind :mod:`api.models`; use that compatibility facade in callers.
"""
# The facade seeds the original monolith namespace dynamically.  Pyflakes cannot
# see those names, while the copied implementation intentionally keeps its old
# global lookups so api.models monkeypatch seams remain effective.
# ruff: noqa: F401, F811, F821, F841, B007, B023, B904, B905
from api.models_parts._compat import seed_module_globals

seed_module_globals(globals())

def _append_journaled_partial_output(
    session,
    stream_id: str | None,
    *,
    dedupe_existing: bool = False,
) -> bool:
    """Recover already-emitted visible output from a dead stream journal.

    This repair path is intentionally conservative: it restores user-visible
    assistant text, display-only reasoning, and tool-card metadata that had
    already been emitted over SSE before the WebUI process died. Restored
    reasoning stays out of ``context_messages`` so it cannot become provider-
    facing history. The repair does not try to continue execution.
    """
    if not stream_id:
        return False

    try:
        from api.runs.journal import read_run_events
        journal = read_run_events(session.session_id, stream_id)
    except Exception:
        logger.debug(
            "Session %s: failed to read run journal for stream %s",
            getattr(session, 'session_id', '?'),
            stream_id,
            exc_info=True,
        )
        return False

    events = [event for event in journal.get('events') or [] if isinstance(event, dict)]
    if not events:
        return False

    appended_any = False
    assistant_parts: list[str] = []
    reasoning_parts: list[str] = []
    assistant_started_at: float | None = None
    current_assistant_idx: int | None = None
    recovered_tool_calls: list[dict] = []
    initial_message_count = len(session.messages or [])
    claimed_existing_assistant_indexes: set[int] = set()

    def content_match_can_receive_reasoning(existing_idx: int) -> bool:
        messages = session.messages or []
        owner_idx = None
        for candidate_idx in range(existing_idx - 1, -1, -1):
            candidate = messages[candidate_idx]
            if isinstance(candidate, dict) and candidate.get('role') == 'user':
                owner_idx = candidate_idx
                break
        if owner_idx is None:
            return False

        pending_text = _normalize_journal_recovery_text(session.pending_user_message)
        if pending_text and not _message_matches_pending_checkpoint(
            messages[owner_idx],
            session.pending_user_message,
            session.pending_started_at,
            session.pending_user_source,
            session.pending_attachments,
        ):
            return False

        for candidate_idx in range(existing_idx + 1, initial_message_count):
            candidate = messages[candidate_idx]
            if not isinstance(candidate, dict) or candidate.get('role') != 'user':
                continue
            candidate_text = _normalize_journal_recovery_text(candidate.get('content'))
            candidate_matches_checkpoint = pending_text and _message_matches_pending_checkpoint(
                candidate,
                session.pending_user_message,
                session.pending_started_at,
                session.pending_user_source,
                session.pending_attachments,
            )
            if candidate_matches_checkpoint and candidate.get('_recovered'):
                continue
            if pending_text and candidate_text == pending_text:
                return False
            return False
        return True

    def append_context_projection(message: dict) -> None:
        context_projection = dict(message)
        context_projection.pop('reasoning', None)
        _append_recovered_turn_to_context(session, context_projection)

    def attach_display_reasoning(message: dict, reasoning: str) -> bool:
        if not reasoning:
            return False
        existing = str(message.get('reasoning') or '').strip()
        if existing:
            return False
        message['reasoning'] = reasoning
        return True

    def flush_assistant() -> int | None:
        nonlocal appended_any, assistant_parts, reasoning_parts
        nonlocal assistant_started_at, current_assistant_idx
        content = ''.join(assistant_parts).strip()
        reasoning = ''.join(reasoning_parts).strip()
        assistant_parts = []
        reasoning_parts = []
        if not content and not reasoning:
            return current_assistant_idx
        if dedupe_existing and content:
            search_excluded = set(claimed_existing_assistant_indexes)
            existing_idx = None
            while True:
                candidate_idx = _find_existing_assistant_for_journal_content(
                    session,
                    content,
                    max_index=initial_message_count,
                    excluded_indexes=search_excluded,
                )
                if candidate_idx is None:
                    break
                if not reasoning or content_match_can_receive_reasoning(candidate_idx):
                    existing_idx = candidate_idx
                    break
                search_excluded.add(candidate_idx)
            if existing_idx is not None:
                claimed_existing_assistant_indexes.add(existing_idx)
                current_assistant_idx = existing_idx
                assistant_started_at = None
                if 0 <= existing_idx < len(session.messages):
                    existing_message = session.messages[existing_idx]
                    append_context_projection(existing_message)
                    if attach_display_reasoning(existing_message, reasoning):
                        appended_any = True
                return existing_idx
        if dedupe_existing and reasoning and not content:
            for existing_idx in range(initial_message_count):
                if existing_idx in claimed_existing_assistant_indexes:
                    continue
                existing_message = session.messages[existing_idx]
                if not isinstance(existing_message, dict):
                    continue
                if (
                    existing_message.get('_recovered_from_run_journal')
                    and existing_message.get('_recovered_stream_id') == stream_id
                    and existing_message.get('role') == 'assistant'
                    and not str(existing_message.get('content') or '').strip()
                    and str(existing_message.get('reasoning') or '').strip() == reasoning
                ):
                    claimed_existing_assistant_indexes.add(existing_idx)
                    current_assistant_idx = existing_idx
                    assistant_started_at = None
                    return existing_idx
        timestamp = int(assistant_started_at or time.time())
        recovered_assistant = {
            'role': 'assistant',
            'content': content,
            'timestamp': timestamp,
            '_recovered_from_run_journal': True,
            '_recovered_stream_id': stream_id,
        }
        attach_display_reasoning(recovered_assistant, reasoning)
        session.messages.append(recovered_assistant)
        append_context_projection(recovered_assistant)
        current_assistant_idx = len(session.messages) - 1
        assistant_started_at = None
        appended_any = True
        return current_assistant_idx

    def ensure_assistant_anchor(created_at: float | None = None) -> int:
        nonlocal appended_any, current_assistant_idx
        idx = flush_assistant()
        if idx is not None:
            return idx
        # A stream can start with tools before any text. Keep those tools
        # visible after restart with an empty recovered assistant anchor instead
        # of inventing synthetic progress prose.
        #
        # Dedup guard (#3875): reuse an existing empty recovered anchor for THIS
        # stream instead of appending a fresh one. The lazy read-side retry path
        # (_retry_journal_recovery_in_place) re-runs this recovery on repeated
        # get_session() calls, and a tool-first stream that never emitted text
        # has no content to dedup on (flush_assistant() returns early on empty),
        # so without this guard each retry — and each distinct interrupted stream
        # over the session's life — appends another empty anchor. A session that
        # was interrupted-and-recovered many times then accumulates thousands of
        # empty content-less assistant rows, bloating the file and (combined with
        # the render path) painting the transcript blank. One anchor per stream
        # is all that's needed to host its recovered tool cards.
        for _existing_idx in range(len(session.messages) - 1, -1, -1):
            _m = session.messages[_existing_idx]
            if not isinstance(_m, dict):
                continue
            if (
                _m.get('_recovered_from_run_journal')
                and _m.get('_recovered_stream_id') == stream_id
                and _m.get('role') == 'assistant'
                and not str(_m.get('content') or '').strip()
                and not str(_m.get('reasoning') or '').strip()
            ):
                current_assistant_idx = _existing_idx
                return _existing_idx
        session.messages.append({
            'role': 'assistant',
            'content': '',
            'timestamp': int(created_at or time.time()),
            '_recovered_from_run_journal': True,
            '_recovered_stream_id': stream_id,
        })
        current_assistant_idx = len(session.messages) - 1
        appended_any = True
        return current_assistant_idx

    for event in events:
        event_name = str(event.get('event') or event.get('type') or '')
        payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
        created_at = event.get('created_at') if isinstance(event.get('created_at'), (int, float)) else None
        if event_name == 'reasoning':
            text = str(
                payload.get('text') or payload.get('reasoning') or payload.get('thinking') or ''
            )
            if not text:
                continue
            if not assistant_parts and not reasoning_parts and assistant_started_at is None:
                assistant_started_at = created_at or time.time()
            reasoning_parts.append(text)
            continue
        if event_name == 'token':
            text = str(payload.get('text') or '')
            if not text:
                continue
            if not assistant_parts and assistant_started_at is None:
                assistant_started_at = created_at or time.time()
            assistant_parts.append(text)
            continue
        if event_name == 'interim_assistant':
            if payload.get('already_streamed'):
                flush_assistant()
                continue
            text = str(payload.get('text') or '').strip()
            if not text:
                continue
            if not assistant_parts and assistant_started_at is None:
                assistant_started_at = created_at or time.time()
            if assistant_parts and not ''.join(assistant_parts).endswith(('\n', ' ')):
                assistant_parts.append('\n\n')
            assistant_parts.append(text)
            flush_assistant()
            continue
        if event_name == 'tool':
            anchor_idx = flush_assistant()
            if anchor_idx is None:
                anchor_idx = ensure_assistant_anchor(created_at)
            name = str(payload.get('name') or 'tool')
            preview = str(payload.get('preview') or '')
            if dedupe_existing and _journal_tool_already_present(
                session, name, preview, stream_id=stream_id,
            ):
                current_assistant_idx = anchor_idx
                continue
            recovered_tool_calls.append({
                'name': name,
                'preview': preview,
                'snippet': preview,
                'tid': f"journal-{event.get('seq') or len(recovered_tool_calls) + 1}",
                'assistant_msg_idx': anchor_idx,
                'args': _truncate_journal_tool_args(payload.get('args') or {}),
                'done': False,
                '_recovered_from_run_journal': True,
                '_recovered_stream_id': stream_id,
            })
            appended_any = True
            current_assistant_idx = anchor_idx
            continue
        if event_name == 'tool_complete':
            name = str(payload.get('name') or '')
            for tool_call in reversed(recovered_tool_calls):
                if tool_call.get('done'):
                    continue
                if not name or tool_call.get('name') == name:
                    tool_call['done'] = True
                    if payload.get('preview'):
                        tool_call['preview'] = str(payload.get('preview') or '')
                        tool_call['snippet'] = str(payload.get('preview') or '')
                    if payload.get('duration') is not None:
                        tool_call['duration'] = payload.get('duration')
                    tool_call['is_error'] = bool(payload.get('is_error', False))
                    break
            continue
        if event_name in {'done', 'stream_end', 'cancel', 'apperror', 'error'}:
            flush_assistant()

    flush_assistant()
    if recovered_tool_calls:
        session.tool_calls = list(session.tool_calls or []) + recovered_tool_calls
        appended_any = True
    return appended_any


# ── Lazy run-journal recovery (read-side self-heal) ─────────────────────────
#
# When sidecar repair runs before the run-journal for the dead stream is
# visible on disk (page-cache loss on WSL2 9p / DrvFs, an un-fsynced journal
# tail, a slow network FS, …), `_append_journaled_partial_output` returns
# False even though the journaled events will appear on disk shortly. Without
# the helpers below the repair path baked a permanent "no agent output was
# recovered" claim into the marker, and a later session read could never
# correct it.
#
# The contract is:
#
#   * Sidecar repair (`_apply_core_sync_or_error_marker`) writes a marker
#     with `_pending_journal_recovery=True` whenever it could not recover
#     visible output AND the stream id is known. Three retry-meta keys go
#     onto the marker: `_journal_retry_stream_id`, `_journal_retry_attempts`,
#     `_journal_retry_first_seen_ts`.
#   * Every `get_session()` call that returns the full session checks the
#     latest assistant marker; if the flag is set it re-runs
#     `_append_journaled_partial_output` with `dedupe_existing=True`. On
#     success the marker is promoted in place to the recovered-output
#     wording, the journaled rows are reordered to sit above the marker,
#     and all retry meta is stripped. If the journal is still missing or
#     zero-byte, the retry is a no-op and does not consume attempt budget.
#     Terminal/non-useful journals consume attempt budget and can demote
#     immediately at the max-attempt cap.
#   * After `_JOURNAL_RETRY_MAX_ATTEMPTS` failed retries or
#     `_JOURNAL_RETRY_GIVEUP_SECONDS` of wall-clock age, the marker is
#     demoted to the neutral wording ("Partial output may have been lost.")
#     so users do not see "reload to retry" prompts forever.

__all__ = ['_append_journaled_partial_output']
