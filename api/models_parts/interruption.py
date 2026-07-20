"""Interrupted-turn classification and journal matching.

Loaded behind :mod:`api.models`; use that compatibility facade in callers.
"""
# The facade seeds the original monolith namespace dynamically.  Pyflakes cannot
# see those names, while the copied implementation intentionally keeps its old
# global lookups so api.models monkeypatch seams remain effective.
# ruff: noqa: F401, F811, F821, F841, B007, B023, B904, B905
from api.models_parts._compat import seed_module_globals

seed_module_globals(globals())

_INTERRUPTED_RECOVERED_WORDING = (
    '**Response interrupted.**\n\n'
    'The live response stream stopped before this turn finished. '
    'The partial output above was recovered from the run journal, '
    'but the interrupted agent process could not continue.'
)
_INTERRUPTED_NO_OUTPUT_WORDING = (
    '**Response interrupted.**\n\n'
    'The live response stream stopped before this turn finished. '
    'The user message above was preserved, but no agent output was recovered.'
)
_INTERRUPTED_PENDING_RETRY_WORDING = (
    '**Response interrupted.**\n\n'
    'The live response stream stopped before this turn finished. '
    'Recovering the partial output from the run journal — '
    'reload this session to retry.'
)
# Neutral wording used when the lazy retry path gives up (max attempts reached
# or the marker has been pending longer than _JOURNAL_RETRY_GIVEUP_SECONDS).
_INTERRUPTED_NEUTRAL_WORDING = (
    '**Response interrupted.**\n\n'
    'The live response stream stopped before this turn finished. '
    'Partial output may have been lost.'
)

_INTERRUPTION_CAUSE_DETAILS = {
    'process_restart': (
        'Evidence: the WebUI process started after this turn began, so this '
        'looks like a real process crash or restart.'
    ),
    'stream_run_split_brain': (
        'Evidence: the browser response stream was gone but the worker registry '
        'still listed the run. This is a stream/run bookkeeping split-brain.'
    ),
    'lost_worker_bookkeeping': (
        'Evidence: the stream was gone and worker bookkeeping no longer had an '
        'active run for it. This usually means the worker state was lost or '
        'cleaned up without a terminal event.'
    ),
    'unknown': (
        'Evidence: the stream stopped, but the WebUI could not classify the '
        'interruption more precisely.'
    ),
}


def _classify_interruption_cause(
    *, stream_id: str | None = None, pending_started_at=None,
) -> str:
    """Classify the stale live-response state without overstating certainty."""
    try:
        started = float(pending_started_at) if pending_started_at else None
    except (TypeError, ValueError):
        started = None

    if started is not None:
        try:
            if float(getattr(_cfg, 'SERVER_START_TIME', 0.0) or 0.0) > started:
                return 'process_restart'
        except (TypeError, ValueError):
            pass

    if stream_id:
        try:
            if _cfg.runtime_worker_alive(str(stream_id)):
                return 'stream_run_split_brain'
        except Exception:
            pass
        return 'lost_worker_bookkeeping'

    return 'unknown'


def _interrupted_content_for(
    *, recovered_output: bool, pending_retry: bool, interruption_cause: str,
) -> str:
    if recovered_output:
        outcome = (
            'The partial output above was recovered from the run journal, '
            'but the interrupted agent process could not continue.'
        )
    elif pending_retry:
        outcome = (
            'Recovering the partial output from the run journal — '
            'reload this session to retry.'
        )
    else:
        outcome = 'The user message above was preserved, but no agent output was recovered.'
    cause_detail = _INTERRUPTION_CAUSE_DETAILS.get(
        interruption_cause,
        _INTERRUPTION_CAUSE_DETAILS['unknown'],
    )
    return (
        '**Response interrupted.**\n\n'
        'The live response stream stopped before this turn finished. '
        f'{cause_detail} {outcome}'
    )


def _interrupted_recovery_marker(
    *,
    recovered_output: bool = False,
    pending_retry: bool = False,
    stream_id: str | None = None,
    pending_started_at=None,
) -> dict:
    """Build the standard interrupted-turn marker.

    ``recovered_output=True`` means the run journal already yielded visible
    text on this repair pass — the marker advertises that the partial output
    has been recovered.

    ``pending_retry=True`` is the lazy-retry hook: the journal was unreadable
    on this pass (page-cache loss, un-fsynced writes on slow FS, etc.). The
    marker carries a ``_pending_journal_recovery`` flag so a later
    ``get_session()`` can re-attempt recovery without baking a permanent
    "no output" claim into the transcript.

    The two are mutually exclusive; ``recovered_output`` wins if both are
    set so the caller cannot accidentally re-arm retry on a successful
    repair.
    """
    interruption_cause = _classify_interruption_cause(
        stream_id=stream_id,
        pending_started_at=pending_started_at,
    )
    content = _interrupted_content_for(
        recovered_output=recovered_output,
        pending_retry=pending_retry,
        interruption_cause=interruption_cause,
    )
    marker = {
        'role': 'assistant',
        'content': content,
        'timestamp': int(time.time()),
        '_error': True,
        'type': 'interrupted',
        'interruption_cause': interruption_cause,
    }
    if pending_retry and not recovered_output:
        marker['_pending_journal_recovery'] = True
    return marker


def _truncate_journal_tool_args(args, limit: int = 4) -> dict:
    if not isinstance(args, dict):
        return {}
    out = {}
    for key, value in list(args.items())[:limit]:
        text = str(value)
        out[str(key)] = text[:120] + ('...' if len(text) > 120 else '')
    return out


def _normalize_journal_recovery_text(value) -> str:
    return " ".join(str(value or "").split())


def _message_matches_pending_checkpoint(message, pending_text, timestamp, source, attachments):
    if not isinstance(message, dict) or message.get('role') != 'user':
        return False
    try:
        message_timestamp = int(message.get('timestamp'))
        expected_timestamp = int(timestamp)
    except (TypeError, ValueError):
        return False
    return (
        _normalize_journal_recovery_text(message.get('content'))
        == _normalize_journal_recovery_text(pending_text)
        and message_timestamp == expected_timestamp
        and (message.get('_source') or 'webui') == (source or 'webui')
        and list(message.get('attachments') or []) == list(attachments or [])
    )


def _message_matches_pending_text(message, pending_text):
    if not isinstance(message, dict) or message.get('role') != 'user':
        return False
    return (
        _normalize_journal_recovery_text(message.get('content'))
        == _normalize_journal_recovery_text(pending_text)
    )


def _latest_user_matches_pending_text(messages, pending_text):
    if not isinstance(messages, list) or not pending_text:
        return False
    for message in reversed(messages):
        if isinstance(message, dict) and message.get('role') == 'user':
            return _message_matches_pending_text(message, pending_text)
    return False


def _partial_message_signature(message: dict) -> tuple:
    """Return a stable identity for partial assistant markers recovered on load."""
    if not isinstance(message, dict):
        return ('', '', ())
    tool_sig = []
    for tool_call in message.get('_partial_tool_calls') or []:
        if not isinstance(tool_call, dict):
            continue
        try:
            args_sig = json.dumps(
                tool_call.get('args') or {},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        except Exception:
            args_sig = str(tool_call.get('args') or '')
        tool_sig.append((
            str(tool_call.get('name') or ''),
            args_sig,
            bool(tool_call.get('done', False)),
            bool(tool_call.get('is_error', False)),
            str(tool_call.get('preview') or tool_call.get('snippet') or ''),
        ))
    return (
        str(message.get('content') or '').strip(),
        str(message.get('reasoning') or '').strip(),
        tuple(tool_sig),
    )


def _collapse_adjacent_duplicate_partials(messages) -> tuple[list, bool]:
    """Collapse repeated identical partial markers from the same failed turn."""
    if not isinstance(messages, list):
        return messages, False
    collapsed = []
    changed = False
    previous_partial_sig = None
    for message in messages:
        if isinstance(message, dict) and message.get('_partial'):
            sig = _partial_message_signature(message)
            if previous_partial_sig == sig:
                changed = True
                continue
            previous_partial_sig = sig
        else:
            previous_partial_sig = None
        collapsed.append(message)
    return collapsed, changed


def _find_existing_assistant_for_journal_content(
    session,
    content: str,
    *,
    max_index: int | None = None,
    excluded_indexes: set[int] | None = None,
) -> int | None:
    candidate = _normalize_journal_recovery_text(content)
    if not candidate:
        return None
    messages = session.messages or []
    stop = len(messages) if max_index is None else min(len(messages), max_index)
    substring_match = None
    for idx in range(stop):
        if excluded_indexes and idx in excluded_indexes:
            continue
        message = messages[idx]
        if not isinstance(message, dict) or message.get('role') != 'assistant':
            continue
        if message.get('_error'):
            continue
        existing = _normalize_journal_recovery_text(message.get('content'))
        if not existing:
            continue
        if existing == candidate:
            return idx
        if substring_match is None and len(candidate) >= 24 and candidate in existing:
            substring_match = idx
    return substring_match


def _journal_tool_already_present(
    session,
    name: str,
    preview: str,
    *,
    stream_id: str | None = None,
) -> bool:
    """Return True when an equivalent tool card already exists.

    Matching rule:

    * If the existing tool card carries ``_recovered_stream_id``, that means a
      previous journal-recovery run materialized it.  The retry can safely
      collapse against it only when both stream ids match — otherwise a
      legitimately-repeated tool (e.g. a second ``terminal: ls`` in a
      different turn) would be dropped.
    * If the existing tool card has no ``_recovered_stream_id`` (a live tool
      card, or a tool card carried over from a core transcript that pre-dates
      stream-id tagging), the legacy name+preview match still wins.  This
      preserves the "core transcript already has this tool, don't duplicate
      it" invariant the original repair path established.
    * When ``stream_id`` is omitted, the helper degrades cleanly to its
      pre-fix session-wide behaviour.
    """
    candidate_name = str(name or '')
    candidate_preview = _normalize_journal_recovery_text(preview)
    candidate_stream = str(stream_id) if stream_id else None
    for tool_call in session.tool_calls or []:
        if not isinstance(tool_call, dict):
            continue
        if str(tool_call.get('name') or '') != candidate_name:
            continue
        existing_preview = _normalize_journal_recovery_text(
            tool_call.get('preview') or tool_call.get('snippet') or ''
        )
        if existing_preview != candidate_preview:
            continue
        if candidate_stream is not None:
            existing_stream = tool_call.get('_recovered_stream_id')
            # A tool card explicitly tagged with a recovered_stream_id that
            # differs from ours belongs to another retry's turn — don't let
            # it pre-empt this retry.  Untagged tool cards (live or carried
            # over from the core transcript) still match.
            if existing_stream and str(existing_stream) != candidate_stream:
                continue
        return True
    return False


def _run_journal_has_visible_output(session, stream_id: str | None) -> bool:
    if not stream_id:
        return False
    try:
        from api.run_journal import read_run_events
        journal = read_run_events(session.session_id, stream_id)
    except Exception:
        return False
    for event in journal.get('events') or []:
        if not isinstance(event, dict):
            continue
        event_name = str(event.get('event') or event.get('type') or '')
        payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
        if event_name == 'token' and str(payload.get('text') or ''):
            return True
        if event_name == 'interim_assistant':
            if payload.get('already_streamed'):
                continue
            if str(payload.get('text') or '').strip():
                return True
        if event_name == 'reasoning':
            reasoning_text = str(
                payload.get('text') or payload.get('reasoning') or payload.get('thinking') or ''
            )
            if reasoning_text.strip():
                return True
        if event_name == 'tool':
            return True
    return False


def _run_journal_terminal_state(session, stream_id: str | None) -> str | None:
    if not stream_id:
        return None
    try:
        from api.run_journal import latest_run_summary
        summary = latest_run_summary(session.session_id, stream_id)
    except Exception:
        return None
    if not summary.get('terminal'):
        return None
    return str(summary.get('terminal_state') or '') or None


def _journal_is_still_arriving(session, stream_id: str | None) -> bool:
    """Return True for journals that may become visible on a later read.

    `read_run_events()` deliberately collapses missing files and empty files
    into an empty event list, so the lazy retry path needs a small filesystem
    visibility check to avoid burning all retry attempts while WSL2 / network
    filesystems are still surfacing the journal.  Non-empty journals are treated
    as sealed enough for retry-budget accounting; if they contain no visible
    output, the normal capped give-up path handles them.
    """
    if not stream_id:
        return False
    try:
        from api.run_journal import _run_path, latest_run_summary

        path = _run_path(session.session_id, stream_id)
        summary = latest_run_summary(session.session_id, stream_id)
        if summary.get('terminal'):
            return False
        try:
            return (not path.exists()) or path.stat().st_size == 0
        except OSError:
            return True
    except Exception:
        logger.debug(
            "Session %s: failed to classify journal visibility for stream %s",
            getattr(session, 'session_id', '?'),
            stream_id,
            exc_info=True,
        )
        return False

__all__ = ['_INTERRUPTED_RECOVERED_WORDING', '_INTERRUPTED_NO_OUTPUT_WORDING', '_INTERRUPTED_PENDING_RETRY_WORDING', '_INTERRUPTED_NEUTRAL_WORDING', '_INTERRUPTION_CAUSE_DETAILS', '_classify_interruption_cause', '_interrupted_content_for', '_interrupted_recovery_marker', '_truncate_journal_tool_args', '_normalize_journal_recovery_text', '_message_matches_pending_checkpoint', '_message_matches_pending_text', '_latest_user_matches_pending_text', '_partial_message_signature', '_collapse_adjacent_duplicate_partials', '_find_existing_assistant_for_journal_content', '_journal_tool_already_present', '_run_journal_has_visible_output', '_run_journal_terminal_state', '_journal_is_still_arriving']
