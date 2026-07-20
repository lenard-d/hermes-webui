"""Compatibility imports for the Kanban package.

Production code imports :mod:`api.kanban`. This module preserves the legacy
import path while downstream callers migrate; it owns no state or dispatch.
"""

# ruff: noqa: F401 -- imported names intentionally preserve the legacy module API

from api.kanban.boards import (
    _board_counts_for_slug,
    _board_meta_dict,
    _create_board_payload,
    _delete_board_payload,
    _list_boards_payload,
    _switch_board_payload,
    _update_board_payload,
)
from api.kanban.config import _config_payload, _update_config_payload
from api.kanban.http import (
    handle_kanban_delete,
    handle_kanban_get,
    handle_kanban_patch,
    handle_kanban_post,
)
from api.kanban.integration import _conn, _kb, _latest_event_id, _obj_dict, _task_dict
from api.kanban.queries import (
    _assignees_payload,
    _board_payload,
    _comment_counts,
    _events_payload,
    _links_for,
    _stats_payload,
    _task_detail_payload,
    _task_link_counts,
    _task_log_payload,
)
from api.kanban.streaming import (
    _KANBAN_SSE_BATCH_LIMIT,
    _KANBAN_SSE_HEARTBEAT_SECONDS,
    _KANBAN_SSE_POLL_SECONDS,
    _handle_events_sse_stream,
    _kanban_sse_fetch_new,
)
from api.kanban.tasks import (
    _bulk_tasks_payload,
    _comment_payload,
    _create_task_payload,
    _dispatch_payload,
    _link_tasks_payload,
    _patch_task,
    _patch_task_payload,
    _set_status_direct,
    _task_action_payload,
)
from api.kanban.validation import (
    BOARD_COLUMNS,
    TASK_PREFIX as _TASK_PREFIX,
    _bool_query,
    _int_query,
    _normalise_board_or_raise,
    _resolve_board,
    _resolve_board_from_body,
    _str_query,
    _validate_status,
)

__all__ = [
    "handle_kanban_delete",
    "handle_kanban_get",
    "handle_kanban_patch",
    "handle_kanban_post",
]
