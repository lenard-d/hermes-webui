"""Stable session state-database Interface.

The implementation is grouped by access authorization, sidebar projection,
cache identity, and message reads.  Callers should import the narrow owner
module when they need implementation-specific seams.
"""

# This module deliberately re-exports the former state_db surface while
# callers migrate to the narrower owner modules named below.
# ruff: noqa: F401

import logging

from .state_db_access import (
    _ExternalSessionView,
    _active_state_db_path,
    _agent_state_db_path,
    agent_session_row_exists,
    agent_session_rows_existing,
    agent_session_zero_message_sids,
    get_session_for_file_ops,
    state_db_has_session,
)
from .state_db_identity import state_db_cache_key, state_db_content_fingerprint
from .state_db_messages import (
    _json_loads_if_string,
    get_state_db_session_message_keys_before_timestamp,
    get_state_db_session_message_prefix_summary,
    get_state_db_session_messages,
    get_state_db_session_summary,
)
from .state_db_sidebar import (
    _apply_sidebar_state_db_override_metadata,
    _apply_sidebar_state_db_overrides,
    _enrich_sidebar_lineage_metadata,
    _read_state_db_sidebar_overrides,
    _sidebar_title_is_generic_webui,
)

logger = logging.getLogger(__name__)
