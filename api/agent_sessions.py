"""Compatibility imports for the :mod:`api.agent_ops` session interface."""

import sqlite3  # noqa: F401 - historical test/integration patch surface

from api.agent_ops import (
    MESSAGING_SOURCES,
    is_cli_session_row,
    is_cli_session_row_visible,
    normalize_agent_session_source,
    open_state_db_readonly,
    read_importable_agent_session_rows,
    read_session_lineage_metadata,
    read_session_lineage_report,
)
from api.agent_ops.session_discovery import _project_agent_session_rows
from api.agent_ops.session_sources import (
    _is_continuation_session,
    _looks_like_default_cli_title,
)

__all__ = [
    "MESSAGING_SOURCES",
    "is_cli_session_row",
    "is_cli_session_row_visible",
    "normalize_agent_session_source",
    "open_state_db_readonly",
    "read_importable_agent_session_rows",
    "read_session_lineage_metadata",
    "read_session_lineage_report",
    "_is_continuation_session",
    "_looks_like_default_cli_title",
    "_project_agent_session_rows",
]
