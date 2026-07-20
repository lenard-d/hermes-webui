"""Interrupted-turn classification and user-visible terminal markers."""

from __future__ import annotations

import time

import api.config as _cfg

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
