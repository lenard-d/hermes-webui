"""Interrupted-turn recovery owners.

The package preserves the legacy import surface while callers can depend on the
specific owner for interruption markers, journal replay/retry, sidecar repair,
or state.db reconciliation.
"""

from __future__ import annotations

import logging

from .interruption import (  # noqa: F401 - compatibility re-exports
    _INTERRUPTED_NEUTRAL_WORDING,
    _INTERRUPTED_NO_OUTPUT_WORDING,
    _INTERRUPTED_PENDING_RETRY_WORDING,
    _INTERRUPTED_RECOVERED_WORDING,
    _INTERRUPTION_CAUSE_DETAILS,
    _classify_interruption_cause,
    _interrupted_content_for,
    _interrupted_recovery_marker,
)
from .journal_replay import (  # noqa: F401 - compatibility re-exports
    _append_journaled_partial_output,
    _find_existing_assistant_for_journal_content,
    _journal_is_still_arriving,
    _journal_tool_already_present,
    _run_journal_has_visible_output,
    _run_journal_terminal_state,
    _truncate_journal_tool_args,
)
from .journal_retry import (  # noqa: F401 - compatibility re-exports
    _JOURNAL_RETRY_GIVEUP_SECONDS,
    _JOURNAL_RETRY_LOCKS,
    _JOURNAL_RETRY_LOCKS_GUARD,
    _JOURNAL_RETRY_MAX_ATTEMPTS,
    _build_recovery_marker_with_retry_hook,
    _journal_retry_lock_for_sid,
    _reorder_journal_tail_above_marker,
    _retry_journal_recovery_in_place,
    _session_has_pending_journal_retry,
    _strip_journal_retry_meta,
    _try_retry_journal_recovery_in_place,
)
from .sidecar_recovery import (  # noqa: F401 - compatibility re-exports
    _REPAIR_STALE_PENDING_GRACE_SECONDS,
    _apply_core_sync_or_error_marker,
    _has_compression_continuation,
    _repair_stale_pending,
)
from .state_db_recovery import _sync_sidecar_from_state_db_if_newer  # noqa: F401

logger = logging.getLogger(__name__)
