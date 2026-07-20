"""Thin HTTP routing adapter for the Kanban domain."""

from __future__ import annotations

from urllib.parse import unquote

from api.helpers import bad, j

from .boards import (
    _create_board_payload,
    _delete_board_payload,
    _list_boards_payload,
    _switch_board_payload,
    _update_board_payload,
)
from .config import _config_payload, _update_config_payload
from .queries import (
    _assignees_payload,
    _board_payload,
    _events_payload,
    _stats_payload,
    _task_detail_payload,
    _task_log_payload,
)
from .streaming import _handle_events_sse_stream
from .tasks import (
    _bulk_tasks_payload,
    _comment_payload,
    _create_task_payload,
    _dispatch_payload,
    _link_tasks_payload,
    _patch_task_payload,
    _task_action_payload,
)
from .validation import TASK_PREFIX, _resolve_board, _resolve_board_from_body


def handle_kanban_get(handler, parsed) -> bool | None:
    """Dispatch a Kanban GET. Three-valued return:

    - ``False`` — no Kanban path matched; caller should emit a 404
      (``_kanban_unknown_endpoint``) for genuinely stale-bundle requests.
    - ``None`` — a path matched and the inner handler already sent a
      response via ``bad(...)`` / ``j(...)`` (which both return ``None``).
      The caller MUST NOT emit another response.
    - ``True`` — a path matched and the inner handler succeeded.

    Treat any falsy-but-not-False return (``0``, ``''``, etc.) as a bug and
    audit the new return path; the caller uses ``is False`` identity check
    to distinguish unmatched paths from already-responded paths (#1843).
    """
    path = parsed.path
    try:
        # Multi-board management endpoints — these do NOT take a board arg
        # because they operate on the on-disk board collection itself, not
        # on a single board's tasks.
        if path == "/api/kanban/boards":
            return j(handler, _list_boards_payload(parsed)) or True
        if path == "/api/kanban/board":
            return j(handler, _board_payload(parsed)) or True
        if path == "/api/kanban/config":
            return j(handler, _config_payload(board=_resolve_board(parsed))) or True
        if path == "/api/kanban/stats":
            return j(handler, _stats_payload(board=_resolve_board(parsed))) or True
        if path == "/api/kanban/assignees":
            return j(handler, _assignees_payload(board=_resolve_board(parsed))) or True
        if path == "/api/kanban/events":
            return j(handler, _events_payload(parsed)) or True
        if path == "/api/kanban/events/stream":
            return _handle_events_sse_stream(handler, parsed)
        if path.startswith(TASK_PREFIX) and path.endswith("/log"):
            task_id = unquote(path[len(TASK_PREFIX) : -len("/log")]).strip("/")
            if not task_id or "/" in task_id:
                return False
            payload = _task_log_payload(parsed, task_id)
            if payload is None:
                return bad(handler, "task not found", status=404)
            return j(handler, payload) or True
        if path.startswith(TASK_PREFIX):
            task_id = unquote(path[len(TASK_PREFIX) :]).strip("/")
            if not task_id or "/" in task_id:
                return False
            payload = _task_detail_payload(task_id, board=_resolve_board(parsed))
            if payload is None:
                return bad(handler, "task not found", status=404)
            return j(handler, payload) or True
        return False
    except ImportError as exc:
        # hermes_cli not installed (webui-only deploy). Return a clean 503
        # "kanban unavailable" rather than a 500 so the frontend's existing
        # try/catch surfaces a useful toast.
        return bad(handler, f"kanban unavailable: {exc}", status=503)
    except LookupError as exc:
        return bad(handler, str(exc), status=404)
    except ValueError as exc:
        return bad(handler, str(exc))
    except RuntimeError as exc:
        return bad(handler, str(exc), status=409)


def handle_kanban_post(handler, parsed, body) -> bool | None:
    """Dispatch a Kanban POST. See ``handle_kanban_get`` for the
    three-valued ``True | None | False`` contract (#1843)."""
    path = parsed.path
    try:
        # Multi-board management endpoints — `_create_board_payload` and
        # `_switch_board_payload` operate on the on-disk board collection,
        # not on a single board's tasks.
        if path == "/api/kanban/boards":
            return j(handler, _create_board_payload(body)) or True
        # POST /api/kanban/boards/<slug>/switch — set active board
        _BOARDS_PREFIX = "/api/kanban/boards/"
        if path.startswith(_BOARDS_PREFIX) and path.endswith("/switch"):
            slug = unquote(path[len(_BOARDS_PREFIX) : -len("/switch")]).strip("/")
            if not slug or "/" in slug:
                return False
            return j(handler, _switch_board_payload(slug)) or True
        # All board-scoped writes accept a ?board=<slug> query param OR a
        # `board` field in the JSON body. Query takes precedence.
        board_q = _resolve_board(parsed)
        board_b = _resolve_board_from_body(body)
        board = board_q if board_q is not None else board_b
        if path == "/api/kanban/dispatch":
            return j(handler, _dispatch_payload(parsed)) or True
        if path == "/api/kanban/tasks/bulk":
            return j(handler, _bulk_tasks_payload(body, board=board)) or True
        if path == "/api/kanban/tasks":
            return j(handler, _create_task_payload(body, board=board)) or True
        if path == "/api/kanban/links":
            return j(handler, _link_tasks_payload(body, board=board)) or True
        if path == "/api/kanban/links/delete":
            return (
                j(handler, _link_tasks_payload(body, unlink=True, board=board)) or True
            )
        if path.startswith(TASK_PREFIX) and path.endswith("/comments"):
            task_id = path[len(TASK_PREFIX) : -len("/comments")].strip("/")
            return j(handler, _comment_payload(task_id, body, board=board)) or True
        for suffix, action in (("/block", "block"), ("/unblock", "unblock")):
            if path.startswith(TASK_PREFIX) and path.endswith(suffix):
                task_id = path[len(TASK_PREFIX) : -len(suffix)].strip("/")
                return (
                    j(handler, _task_action_payload(task_id, body, action, board=board))
                    or True
                )
        if path.startswith(TASK_PREFIX) and path.endswith("/patch"):
            task_id = path[len(TASK_PREFIX) : -len("/patch")].strip("/")
            return j(handler, _patch_task_payload(task_id, body, board=board)) or True
    except ImportError as exc:
        return bad(handler, f"kanban unavailable: {exc}", status=503)
    except LookupError as exc:
        return bad(handler, str(exc), status=404)
    except ValueError as exc:
        return bad(handler, str(exc))
    except RuntimeError as exc:
        return bad(handler, str(exc), status=409)
    return False


def handle_kanban_patch(handler, parsed, body) -> bool | None:
    """Dispatch a Kanban PATCH. See ``handle_kanban_get`` for the
    three-valued ``True | None | False`` contract (#1843)."""
    path = parsed.path
    try:
        if path == "/api/kanban/config":
            return j(handler, _update_config_payload(body)) or True
        # /boards/<slug> routes operate on the on-disk board collection
        # itself — the slug travels in the URL path, not via ?board=. Match
        # them BEFORE resolving the board param so a stray ?board=ghost in
        # the query string doesn't 404 the legitimate `experiments` rename.
        # (Mirrors handle_kanban_post's structure — fixes asymmetry caught
        # by Opus advisor.)
        _BOARDS_PREFIX = "/api/kanban/boards/"
        if path.startswith(_BOARDS_PREFIX):
            slug = unquote(path[len(_BOARDS_PREFIX) :]).strip("/")
            if not slug or "/" in slug:
                return False
            return j(handler, _update_board_payload(slug, body)) or True
        # Task-scoped writes accept ?board=<slug> (or body.board) to pin the
        # write to a specific board. Query takes precedence over body.
        board_q = _resolve_board(parsed)
        board_b = _resolve_board_from_body(body)
        board = board_q if board_q is not None else board_b
        if path.startswith(TASK_PREFIX):
            task_id = unquote(path[len(TASK_PREFIX) :]).strip("/")
            if not task_id or "/" in task_id:
                return False
            return j(handler, _patch_task_payload(task_id, body, board=board)) or True
    except ImportError as exc:
        return bad(handler, f"kanban unavailable: {exc}", status=503)
    except LookupError as exc:
        return bad(handler, str(exc), status=404)
    except ValueError as exc:
        return bad(handler, str(exc))
    except RuntimeError as exc:
        return bad(handler, str(exc), status=409)
    return False


def handle_kanban_delete(handler, parsed, body) -> bool | None:
    """Dispatch a Kanban DELETE. See ``handle_kanban_get`` for the
    three-valued ``True | None | False`` contract (#1843)."""
    path = parsed.path
    try:
        # Same routing reorder as PATCH: /boards/<slug> path-routed first,
        # so a stray ?board=ghost can't 404 a legitimate board archive.
        _BOARDS_PREFIX = "/api/kanban/boards/"
        if path.startswith(_BOARDS_PREFIX):
            slug = unquote(path[len(_BOARDS_PREFIX) :]).strip("/")
            if not slug or "/" in slug:
                return False
            return j(handler, _delete_board_payload(slug, parsed)) or True
        board_q = _resolve_board(parsed)
        board_b = _resolve_board_from_body(body)
        board = board_q if board_q is not None else board_b
        if path == "/api/kanban/links":
            return (
                j(handler, _link_tasks_payload(body, unlink=True, board=board)) or True
            )
    except ImportError as exc:
        return bad(handler, f"kanban unavailable: {exc}", status=503)
    except LookupError as exc:
        return bad(handler, str(exc), status=404)
    except ValueError as exc:
        return bad(handler, str(exc))
    except RuntimeError as exc:
        return bad(handler, str(exc), status=409)
    return False
