"""Stable append-only session reconciliation Interface."""

# This module deliberately preserves the former reconciliation import surface.
# ruff: noqa: F401

import logging

from .claude_code import CLAUDE_CODE_SOURCE, get_claude_code_session_messages
from .reconciliation_context import (
    _compression_anchor_timestamp_as_float,
    _context_messages_include_compression_marker,
    _normalized_compression_anchor_text,
    _sidecar_has_terminal_partial_error,
    _state_db_anchor_index,
    state_db_delta_after_context,
)
from .reconciliation_merge import (
    _insert_state_message_chronologically,
    _tool_call_assistant_should_precede_content_assistant,
    merge_session_messages_append_only,
)
from .reconciliation_projection import (
    count_conversation_rounds,
    get_cli_session_messages,
    reconciled_state_db_messages_for_session,
)
from .state_db_messages import get_state_db_session_messages

logger = logging.getLogger(__name__)
