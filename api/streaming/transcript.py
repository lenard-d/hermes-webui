"""Transcript reconciliation and terminal partial preservation."""

from __future__ import annotations

import copy
import logging
import time

from .compression_anchors import _find_current_user_turn, _is_context_compression_marker
from .context_replay import (
    _message_identity,
    _messages_have_prefix,
    _strip_replayed_prefix,
)
from .post_compression_context import (
    _is_compressed_context_tool_result_summary_message,
    _restore_reasoning_metadata,
)
from .stale_user_context import (
    _last_user_row,
    _normalize_user_text,
    _stale_user_tail_candidate,
    _strip_stale_user_merge_from_messages,
)
from .thinking_content import _assistant_message_has_final_visible_text
from .tool_events import _partial_marker_already_present
from api.workspace_context import _looks_like_current_user_turn


logger = logging.getLogger(__name__)


def _drop_synthetic_control_messages(messages):
    from .terminal_outcomes import _drop_synthetic_control_messages as drop

    return drop(messages)


def _has_new_assistant_reply(all_messages: list, prev_count: int) -> bool:
    """Return whether the current result added visible assistant content."""
    if len(all_messages) <= prev_count:
        return False
    return any(
        message.get("role") == "assistant"
        and str(message.get("content") or "").strip()
        for message in all_messages[prev_count:]
        if isinstance(message, dict)
    )


def _merge_display_messages_after_agent_result(previous_display, previous_context, result_messages, msg_text, source: str = "webui"):
    """Keep UI transcript durable while allowing model context to compact.

    If Hermes Agent returns a normal append-only history, append that delta to
    the UI transcript. If the model/context history was compacted and no longer
    has the prior context as a prefix, keep the previous UI transcript and append
    the current user turn onward. Synthetic compaction/reference markers remain
    internal recovery material and must not become visible user/assistant turns.
    """
    previous_display = [
        m for m in list(previous_display or [])
        if not _is_context_compression_marker(m)
        and not _is_compressed_context_tool_result_summary_message(m)
    ]
    # Drop Hermes Agent internal verify-loop scaffolding (synthetic "premature
    # done" answer + the "[System: ...verification evidence...]" nudge) before
    # it can become a visible user/assistant turn. The agent flags these with
    # structured markers (_verification_stop_synthetic / _pre_verify_synthetic)
    # and already keeps them out of its own durable store; honor the same
    # markers here so they never leak into the WebUI transcript. Filter all
    # three inputs consistently so prefix/delta detection below stays aligned.
    # (#5334; same internal-control-message class as #3320/#3821/#4373/#4875)
    previous_display = _drop_synthetic_control_messages(previous_display)
    # Deduplicate stale _partial messages that accumulated in previous_display.
    # A bug in cancel_stream() could insert multiple identical _partial messages
    # when _stripped was empty but _has_reasoning/_has_tools was True. The
    # merge's _message_identity previously returned None for empty _partial
    # messages, so the seen-set couldn't catch them — they doubled each turn.
    # Scan backwards and keep only the LAST occurrence of each unique _partial
    # identity, then reverse back to original order.
    _partial_seen = set()
    _deduped_rev = []
    for m in reversed(previous_display):
        if isinstance(m, dict) and m.get('_partial'):
            key = _message_identity(m)
            if key is not None:
                if key in _partial_seen:
                    continue
                _partial_seen.add(key)
        _deduped_rev.append(m)
    _deduped = list(reversed(_deduped_rev))
    if len(_deduped) < len(previous_display):
        logger.debug(
            "Deduplicated %d stale _partial messages from previous_display (was %d, now %d)",
            len(previous_display) - len(_deduped), len(previous_display), len(_deduped),
        )
    previous_display = _deduped
    previous_context = list(previous_context or [])
    result_messages = list(result_messages or [])
    # Same marker filter for the model-history inputs: the synthetic verify-loop
    # answer/nudge live in the agent's returned messages and prior context, and
    # would otherwise slip into the merged transcript as a real delta. (#5334)
    previous_context = _drop_synthetic_control_messages(previous_context)
    result_messages = _drop_synthetic_control_messages(result_messages)
    if not result_messages:
        return previous_display
    previous_user_tail = _stale_user_tail_candidate(_last_user_row(previous_context))

    # ── Backfill normal turns from previous_context that are missing from
    # previous_display.  After context compression recovery, previous_context
    # can contain user/assistant turns that were never rendered in the visible
    # transcript (they were behind a compression marker). On the next
    # append-only merge those turns sit inside the shared prefix and get
    # stripped, leaving them permanently invisible.  Reinsert them now.
    #
    # Use display as the backbone to preserve visible order. Walk display in
    # order and for each display message search for its identity in context
    # at/after a cursor. Any context messages between the cursor and that
    # match are context-only gaps that get spliced in before the display msg.
    if previous_display and previous_context:
        _display_id_set = {_message_identity(m) for m in previous_display}
        _context_id_set = {
            _message_identity(m)
            for m in previous_context
            if not _is_context_compression_marker(m)
            and not _is_compressed_context_tool_result_summary_message(m)
        }
        _has_context_only_turns = bool(_context_id_set - _display_id_set)
        if _has_context_only_turns:
            context_keys = [_message_identity(m) for m in previous_context]
            # Precompute display keys once; avoids repeated json.dumps calls inside
            # the inner any() loop (was O(D²·C) — see perf fix below).
            _display_keys = [_message_identity(m) for m in previous_display]
            # Multiset mirror of context_keys[_cursor:] kept in sync as _cursor
            # advances. Enables O(1) membership tests in the any() check instead
            # of an O(N) list scan, while preserving EXACT list-slice semantics:
            # _message_identity intentionally returns duplicate keys for
            # identical-content turns (and None for empty rows), so a plain set
            # would drop a key still present later in the slice. A count-keyed
            # dict (including None) matches `in context_keys[_cursor:]` exactly.
            _remaining_ck_counts = {}
            for _ck in context_keys:
                _remaining_ck_counts[_ck] = _remaining_ck_counts.get(_ck, 0) + 1
            _backfilled = []
            # #3300 fix: track ONLY context rows we splice in, so the
            # visible-display backbone is never suppressed. Sharing one set
            # between context inserts and display rows (and _message_identity
            # ignoring timestamps) dropped a legitimate second identical visible
            # user turn. Display rows are always appended in order; a context
            # row is backfilled only if it isn't already a display row and
            # hasn't already been inserted.
            _context_inserted = set()
            _cursor = 0
            for _display_idx, _dmsg in enumerate(previous_display):
                _dkey = _display_keys[_display_idx]
                if _dkey is not None:
                    _j = _cursor
                    while _j < len(context_keys) and context_keys[_j] != _dkey:
                        _j += 1
                    if _j < len(context_keys):
                        for _k in range(_cursor, _j):
                            _ckey = context_keys[_k]
                            _cmsg = previous_context[_k]
                            if (
                                _ckey is not None
                                and _ckey not in _context_inserted
                                and _ckey not in _display_id_set
                                and not _is_context_compression_marker(_cmsg)
                                and not _is_compressed_context_tool_result_summary_message(_cmsg)
                            ):
                                _backfilled.append(copy.deepcopy(_cmsg))
                                _context_inserted.add(_ckey)
                        # Sync multiset: decrement keys consumed by advancing
                        # the cursor to _j+1 (delete at zero so membership matches
                        # the list slice exactly).
                        for _k in range(_cursor, _j + 1):
                            _consumed_ck = context_keys[_k]
                            _ck_n = _remaining_ck_counts.get(_consumed_ck, 0) - 1
                            if _ck_n <= 0:
                                _remaining_ck_counts.pop(_consumed_ck, None)
                            else:
                                _remaining_ck_counts[_consumed_ck] = _ck_n
                        _cursor = _j + 1
                    elif not any(
                        _display_keys[_fi] in _remaining_ck_counts
                        for _fi in range(_display_idx + 1, len(_display_keys))
                    ):
                        for _k in range(_cursor, len(context_keys)):
                            _ckey = context_keys[_k]
                            _cmsg = previous_context[_k]
                            if (
                                _ckey is not None
                                and _ckey not in _context_inserted
                                and _ckey not in _display_id_set
                                and not _is_context_compression_marker(_cmsg)
                                and not _is_compressed_context_tool_result_summary_message(_cmsg)
                            ):
                                _backfilled.append(copy.deepcopy(_cmsg))
                                _context_inserted.add(_ckey)
                        _cursor = len(context_keys)
                        _remaining_ck_counts.clear()
                # The display row is the visible backbone — always preserve it,
                # in order, even when an earlier (identical-content) turn or a
                # backfilled context row shares its timestamp-less identity.
                _backfilled.append(_dmsg)
            while _cursor < len(context_keys):
                _ckey = context_keys[_cursor]
                _cmsg = previous_context[_cursor]
                _cursor += 1
                if (
                    _ckey is not None
                    and _ckey not in _context_inserted
                    and _ckey not in _display_id_set
                    and not _is_context_compression_marker(_cmsg)
                    and not _is_compressed_context_tool_result_summary_message(_cmsg)
                ):
                    _backfilled.append(copy.deepcopy(_cmsg))
                    _context_inserted.add(_ckey)
            if len(_backfilled) > len(previous_display):
                logger.debug(
                    "Backfilled %d context-only turns into previous_display (was %d, now %d)",
                    len(_backfilled) - len(previous_display),
                    len(previous_display),
                    len(_backfilled),
                )
                previous_display = _backfilled

    if _messages_have_prefix(result_messages, previous_context):
        candidates = result_messages[len(previous_context):]
        # Normalize stale merges only in the new-turn slice; never rewrite
        # historical rows in the already-committed previous_context prefix.
        if msg_text and previous_user_tail:
            candidates = _strip_stale_user_merge_from_messages(
                candidates,
                msg_text,
                previous_user_tail,
                previous_context=previous_context,
            )
        current_user_key = _message_identity({'role': 'user', 'content': msg_text})
        current_user_in_candidates = any(
            _message_identity(m) == current_user_key or _looks_like_current_user_turn(m, msg_text)
            for m in candidates
        )
        assistant_or_tool_only_candidates = bool(candidates) and all(
            _is_context_compression_marker(m)
            or (
                isinstance(m, dict)
                and m.get('role') in ('assistant', 'tool')
            )
            for m in candidates
        )
        if not (assistant_or_tool_only_candidates and not current_user_in_candidates):
            candidates = _strip_replayed_prefix(previous_display, candidates)
            candidates = _strip_replayed_prefix(previous_context, candidates)
    else:
        current_user_idx = _find_current_user_turn(result_messages, msg_text)
        turn_candidates = result_messages[current_user_idx:] if current_user_idx is not None else []
        # Normalize stale merges only in the current-turn slice.
        if msg_text and previous_user_tail:
            turn_candidates = _strip_stale_user_merge_from_messages(
                turn_candidates,
                msg_text,
                previous_user_tail,
                previous_context=previous_context,
            )
        candidates = turn_candidates

    merged = previous_display[:]
    seen = {_message_identity(m) for m in merged}
    current_user_key = _message_identity({'role': 'user', 'content': msg_text})
    current_user_in_candidates = any(
        _message_identity(m) == current_user_key or _looks_like_current_user_turn(m, msg_text)
        for m in candidates
    )
    current_user_already_checkpointed = bool(
        merged
        and (
            _message_identity(merged[-1]) == current_user_key
            or _looks_like_current_user_turn(merged[-1], msg_text)
        )
    )
    if (
        current_user_key is not None
        and not current_user_in_candidates
        and not current_user_already_checkpointed
        and any(
            isinstance(m, dict) and m.get('role') in ('assistant', 'tool')
            for m in candidates
        )
    ):
        # Some provider retry/fallback paths can return an assistant/tool delta
        # without echoing the current user turn. In deferred session-save mode
        # the prompt exists only in pending_user_message, so appending that delta
        # directly would make the assistant bubble appear attached to the prior
        # exchange and then clear the pending prompt. Materialize the current
        # turn at the transcript boundary before the assistant/tool response.
        current_user_msg = {'role': 'user', 'content': msg_text}
        if source and source != 'webui':
            current_user_msg['_source'] = source
        insert_at = 0
        while insert_at < len(candidates) and _is_context_compression_marker(candidates[insert_at]):
            insert_at += 1
        candidates = candidates[:insert_at] + [current_user_msg] + candidates[insert_at:]

    for msg in candidates:
        if (
            _is_context_compression_marker(msg)
            or _is_compressed_context_tool_result_summary_message(msg)
        ):
            continue
        key = _message_identity(msg)
        is_current_user_turn = _looks_like_current_user_turn(msg, msg_text)
        if (
            ((key is not None and key == current_user_key) or is_current_user_turn)
            and merged
            and (
                _message_identity(merged[-1]) == current_user_key
                or _looks_like_current_user_turn(merged[-1], msg_text)
            )
        ):
            # Eager session-save mode can checkpoint the current user turn
            # before the agent runs. When the agent returns that same user turn
            # in result_messages, keep the durable checkpoint and append only
            # the assistant/tool delta.
            # The eager checkpoint was written before _assign_stable_message_ids
            # stamped the result rows, so it has no `id` while the context copy
            # does — which would silently defeat id-based fork/truncate alignment
            # for eager-mode users. Carry the minted id onto the kept checkpoint
            # so display and context share it (#5564).
            if (
                isinstance(msg, dict)
                and msg.get('id') is not None
                and isinstance(merged[-1], dict)
                and merged[-1].get('id') is None
            ):
                merged[-1]['id'] = msg['id']
            continue
        if (
            key is not None
            and isinstance(msg, dict)
            and msg.get('role') == 'assistant'
            and merged
            and _message_identity(merged[-1]) == key
        ):
            # Some provider/result replay paths can include the same assistant
            # message twice in the current delta. Treat only adjacent identity
            # matches as replay duplicates so identical answers in separate
            # user turns remain visible.
            continue
        if _is_context_compression_marker(msg) and key is not None and key in seen:
            continue
        display_msg = msg
        if (
            ((key is not None and key == current_user_key) or is_current_user_turn)
            and isinstance(msg, dict)
            and msg.get('role') == 'user'
        ):
            display_msg = copy.deepcopy(msg)
            display_msg['content'] = msg_text
            if source and source != 'webui':
                display_msg['_source'] = source
        merged.append(copy.deepcopy(display_msg))
        if key is not None:
            seen.add(key)
    return merged

def _stamp_missing_message_timestamps(messages, *, now: float | None = None) -> int:
    """Stamp missing message timestamps without collapsing transcript order.

    Compacted/reconciled rows can arrive without timestamps. Assigning one
    integer seconds value to the whole batch makes later timestamp-based display
    merges unstable; use a subsecond sequence instead.
    """
    base = time.time() if now is None else float(now)
    stamped = 0
    for msg in messages or []:
        if isinstance(msg, dict) and not msg.get('timestamp') and not msg.get('_ts'):
            msg['timestamp'] = base + (stamped * 0.000001)
            stamped += 1
    return stamped

def _assistant_reply_added_after_current_turn(result_messages, previous_context, msg_text) -> bool:
    """Return True only when the just-finished turn produced assistant text."""
    result_messages = list(result_messages or [])
    previous_context = list(previous_context or [])
    if _messages_have_prefix(result_messages, previous_context):
        candidates = result_messages[len(previous_context):]
    else:
        current_user_idx = _find_current_user_turn(result_messages, msg_text)
        candidates = result_messages[current_user_idx + 1:] if current_user_idx is not None else result_messages
    return any(
        isinstance(m, dict)
        and m.get('role') == 'assistant'
        and not m.get('_error')
        and _assistant_message_has_final_visible_text(m)
        for m in candidates
    )

def _session_lacks_final_assistant_answer(messages) -> bool:
    """Return True when the persisted transcript ends before a final answer."""
    for msg in reversed(list(messages or [])):
        if not isinstance(msg, dict):
            continue
        if msg.get('_error'):
            return False
        if _is_context_compression_marker(msg):
            continue
        role = msg.get('role')
        if role == 'tool':
            return True
        if role == 'assistant':
            if _assistant_message_has_final_visible_text(msg):
                return False
            continue
        if role == 'user':
            return True
    return True

def _turn_transcript_lacks_final_assistant_answer(
    merged_messages,
    previous_display,
    msg_text,
    source: str = "webui",
    drop_replayed_assistant: bool = False,
) -> bool:
    """Return True when an already-merged transcript still lacks a final assistant answer."""
    merged_messages = list(merged_messages or [])
    previous_display = list(previous_display or [])
    current_user_idx = _find_current_user_turn(merged_messages, msg_text)
    if current_user_idx is None or current_user_idx < len(previous_display):
        # The active turn lives after the durable transcript boundary. If the
        # merged display only exposes an older user row, materialize the pending
        # prompt so a replayed assistant row cannot satisfy the wrong turn.
        pending_user = {
            'role': 'user',
            'content': msg_text,
        }
        if source and source != 'webui':
            pending_user['_source'] = source
        merged_messages.append(pending_user)
        current_user_idx = len(merged_messages) - 1

    current_user_key = _message_identity(merged_messages[current_user_idx])
    filtered_messages = merged_messages[:current_user_idx + 1]
    if drop_replayed_assistant:
        prior_id_set = {
            _message_identity(msg)
            for msg in merged_messages[:current_user_idx]
            if isinstance(msg, dict)
        }
        for msg in merged_messages[current_user_idx + 1:]:
            if not isinstance(msg, dict):
                filtered_messages.append(msg)
                continue
            if msg.get('role') == 'assistant':
                key = _message_identity(msg)
                if key is not None and key in prior_id_set:
                    continue
            filtered_messages.append(msg)
    else:
        filtered_messages.extend(merged_messages[current_user_idx + 1:])
    if current_user_key is not None:
        filtered_messages = [
            msg for msg in filtered_messages
            if _message_identity(msg) != current_user_key or msg is merged_messages[current_user_idx]
        ]
    return _session_lacks_final_assistant_answer(filtered_messages)

def _merged_transcript_lacks_final_assistant_answer(
    previous_display,
    previous_context,
    result_messages,
    msg_text,
    source: str = "webui",
    drop_replayed_assistant: bool = False,
) -> bool:
    """Return True when the current turn still lacks a final assistant answer."""
    previous_display = list(previous_display or [])
    merged_messages = _merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        _restore_reasoning_metadata(previous_display, result_messages),
        msg_text,
        source=source,
    )
    return _turn_transcript_lacks_final_assistant_answer(
        merged_messages,
        previous_display,
        msg_text,
        source=source,
        drop_replayed_assistant=drop_replayed_assistant,
    )

def _agent_result_terminal_failure(result) -> bool:
    """Return True for agent results that must not be finalized as done."""
    if not isinstance(result, dict):
        return False
    status = str(result.get('status') or result.get('state') or '').strip().lower()
    if status in {'failed', 'error', 'partial', 'compression_exhausted'}:
        return True
    if result.get('compression_exhausted'):
        return True
    if result.get('failed') or result.get('partial'):
        return True
    return False

def _materialize_pending_user_turn_before_error(session) -> bool:
    """Persist the pending user prompt before clearing runtime stream state.

    Error paths often clear ``pending_user_message`` before appending an assistant
    error marker. In deferred session-save mode that pending field can be the
    only durable copy of the user's current turn, so clearing it makes the user
    bubble disappear on reload/reconcile. Return True when a recovered user turn
    was appended.
    """
    pending_text = str(getattr(session, 'pending_user_message', None) or '')
    if not pending_text:
        return False
    recovered_ts = int(time.time())
    pending_started_at = getattr(session, 'pending_started_at', None)
    if isinstance(pending_started_at, (int, float)) and pending_started_at > 0:
        recovered_ts = int(pending_started_at)
    pending_source = getattr(session, 'pending_user_source', None) or 'webui'
    pending_attachments = list(getattr(session, 'pending_attachments', None) or [])

    def is_exact_checkpoint(messages):
        if not isinstance(messages, list) or not messages:
            return False
        existing = messages[-1]
        if not isinstance(existing, dict) or existing.get('role') != 'user':
            return False
        existing_source = existing.get('_source') or 'webui'
        try:
            existing_ts = int(existing.get('timestamp'))
        except (TypeError, ValueError):
            return False
        return (
            _normalize_user_text(existing.get('content')) == _normalize_user_text(pending_text)
            and existing_ts == recovered_ts
            and existing_source == pending_source
            and list(existing.get('attachments') or []) == pending_attachments
        )

    if is_exact_checkpoint(getattr(session, 'messages', None)):
        return False
    recovered = {
        'role': 'user',
        'content': pending_text,
        'timestamp': recovered_ts,
        '_recovered': True,
    }
    if pending_source != 'webui':
        recovered['_source'] = pending_source
    if pending_attachments:
        recovered['attachments'] = pending_attachments
    session.messages.append(recovered)
    # Mirror to context_messages so the _recovered flag survives the state.db
    # round-trip (#4283).  state.db has no _recovered column, so without this
    # mirror the next turn's reconciled_state_db_messages_for_session(
    # prefer_context=True) finds the recovered user as a flagless state.db
    # delta and _sanitize_messages_for_api cannot filter it — causing the
    # interrupted turn's prompt to be prepended to every subsequent turn.
    # Placing the mirror here (rather than in _persist_cancelled_turn) covers
    # all three callers: cancel, provider-error, and exception paths.
    ctx = getattr(session, 'context_messages', None)
    if isinstance(ctx, list) and ctx and not is_exact_checkpoint(ctx):
        ctx.append(dict(recovered))
    # The new user turn is now committed to messages (#3831): advance a positive
    # truncation watermark left over from a prior retry/undo/edit so that
    # merge_session_messages_append_only() still filters out replaced pre-edit
    # rows from state.db. The merge's sidecar_advanced_past_watermark guard
    # allows state.db rows newer than the watermark, so post-edit turns are not
    # dropped. Never 0.0 (the truncate-to-empty sentinel, #2914).
    if getattr(session, 'truncation_watermark', None):
        session.truncation_watermark = float(recovered_ts)
    return True

def _build_partial_message(content_text, reasoning_text, tool_calls) -> dict | None:
    """Build a _partial assistant message from raw streaming buffers.

    Shared by cancel_stream() and _snapshot_and_append_partial_on_error().
    Strips thinking/reasoning markup, builds the dict, returns None when
    there is nothing meaningful to preserve.
    """
    import re as _re
    partial_text = (content_text or '').strip()
    _stripped = ''
    if partial_text:
        # First pass: remove complete <thinking>...</thinking> blocks.
        _stripped = _re.sub(r'<think(?:ing)?\b[^>]*>.*?</think(?:ing)?>',
                            '', partial_text,
                            flags=_re.DOTALL | _re.IGNORECASE).strip()
        # Second pass: strip trailing UNCLOSED think/thinking block (the common
        # cancel/error case — user stops mid-reasoning before the close tag appears).
        _stripped = _re.sub(r'<think(?:ing)?\b[^>]*>.*',
                            '', _stripped,
                            flags=_re.DOTALL | _re.IGNORECASE).strip()
    _has_reasoning = bool(reasoning_text and reasoning_text.strip())
    _has_tools = bool(tool_calls)
    if not (_stripped or _has_reasoning or _has_tools):
        return None
    _msg: dict = {
        'role': 'assistant',
        'content': _stripped,  # may be empty for reasoning/tool-only turns
        '_partial': True,
        'timestamp': int(time.time()),
    }
    if _has_reasoning:
        _msg['reasoning'] = reasoning_text.strip()
    if _has_tools:
        _msg['_partial_tool_calls'] = list(tool_calls)
    return _msg

def _snapshot_and_append_partial_on_error(session, stream_id) -> dict | None:
    """Snapshot runtime-owned progress and append a _partial message.

    Uses _build_partial_message() for the shared thinking-strip + dict-build logic.
    """
    from api import config as _live_config

    progress = _live_config.runtime_progress_snapshot(stream_id)

    _partial_msg = _build_partial_message(
        progress.partial_text,
        progress.reasoning_text,
        progress.live_tool_calls,
    )
    if _partial_msg is None:
        return None
    if not isinstance(session.messages, list):
        session.messages = []
    if not _partial_marker_already_present(session.messages, _partial_msg):
        session.messages.append(_partial_msg)
        return _partial_msg
    return None
