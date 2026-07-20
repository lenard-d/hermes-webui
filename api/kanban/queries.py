"""Read-only Kanban board and task projections."""

from __future__ import annotations

import json

from .integration import _conn, _kb, _latest_event_id, _obj_dict, _task_dict
from .validation import (
    BOARD_COLUMNS,
    _bool_query,
    _int_query,
    _resolve_board,
    _str_query,
)


def _task_link_counts(conn, tasks):
    """Return a dict mapping each task id to its {parents, children} dependency link counts."""
    counts = {task.id: {"parents": 0, "children": 0} for task in tasks}
    try:
        rows = conn.execute("SELECT parent_id, child_id FROM task_links").fetchall()
    except Exception:
        return counts
    for row in rows:
        counts.setdefault(row["parent_id"], {"parents": 0, "children": 0})[
            "children"
        ] += 1
        counts.setdefault(row["child_id"], {"parents": 0, "children": 0})[
            "parents"
        ] += 1
    return counts


def _comment_counts(conn):
    """Return a dict mapping each task id to its total comment count across the board."""
    try:
        rows = conn.execute(
            "SELECT task_id, COUNT(*) AS n FROM task_comments GROUP BY task_id"
        ).fetchall()
    except Exception:
        return {}
    return {row["task_id"]: int(row["n"] or 0) for row in rows}


def _board_payload(parsed):
    """Build the full board JSON payload: kanban columns with tasks, filter state, and latest_event_id."""
    board = _resolve_board(parsed)
    kb = _kb()
    tenant = _str_query(parsed, "tenant")
    assignee = _str_query(parsed, "assignee")
    include_archived = _bool_query(parsed, "include_archived", False)
    only_mine = _bool_query(parsed, "only_mine", False)
    since = _int_query(parsed, "since", None, minimum=0)
    profile = None
    if only_mine and not assignee:
        try:
            from api.profiles import get_active_profile_name

            profile = get_active_profile_name() or "default"
        except Exception:
            profile = "default"
        assignee = profile

    with _conn(board=board) as conn:
        latest_event_id = _latest_event_id(conn)
        if since is not None and since >= latest_event_id:
            return {
                "changed": False,
                "latest_event_id": latest_event_id,
                "read_only": False,
            }

        tasks = kb.list_tasks(
            conn,
            tenant=tenant,
            assignee=assignee,
            include_archived=include_archived,
        )
        link_counts = _task_link_counts(conn, tasks)
        comment_counts = _comment_counts(conn)

        def row(task):
            data = _task_dict(task)
            data["link_counts"] = link_counts.get(
                task.id, {"parents": 0, "children": 0}
            )
            data["comment_count"] = comment_counts.get(task.id, 0)
            return data

        columns = [
            {
                "name": name,
                "tasks": [row(task) for task in tasks if task.status == name],
            }
            for name in BOARD_COLUMNS
        ]
        if include_archived:
            columns.append(
                {
                    "name": "archived",
                    "tasks": [row(task) for task in tasks if task.status == "archived"],
                }
            )
        return {
            "columns": columns,
            "tenants": sorted(
                {task.tenant for task in tasks if getattr(task, "tenant", None)}
            ),
            "assignees": sorted(
                {task.assignee for task in tasks if getattr(task, "assignee", None)}
            ),
            "latest_event_id": latest_event_id,
            "changed": True,
            "read_only": False,
            "filters": {
                "tenant": tenant,
                "assignee": assignee,
                "include_archived": include_archived,
                "only_mine": only_mine,
                "profile": profile,
            },
        }


def _links_for(conn, task_id: str) -> dict:
    """Return {parents: [...], children: [...]} dependency id lists for a task."""
    kb = _kb()
    return {
        "parents": kb.parent_ids(conn, task_id),
        "children": kb.child_ids(conn, task_id),
    }


def _task_detail_payload(task_id: str, *, board=None):
    """Return the full task detail: task dict, comments, events, dependency links, and run history."""
    kb = _kb()
    with _conn(board=board) as conn:
        task = kb.get_task(conn, task_id)
        if not task:
            return None
        return {
            "task": _task_dict(task),
            "comments": [_obj_dict(c) for c in kb.list_comments(conn, task_id)],
            "events": [_obj_dict(e) for e in kb.list_events(conn, task_id)],
            "links": _links_for(conn, task_id),
            "runs": [_obj_dict(r) for r in kb.list_runs(conn, task_id)],
            "read_only": False,
        }


def _events_payload(parsed):
    """Return paginated task events from the board's event log, starting after the ?since= cursor."""
    board = _resolve_board(parsed)
    since = _int_query(parsed, "since", 0, minimum=0)
    limit = _int_query(parsed, "limit", 200, minimum=1, maximum=200)
    with _conn(board=board) as conn:
        rows = conn.execute(
            "SELECT id, task_id, run_id, kind, payload, created_at "
            "FROM task_events WHERE id > ? ORDER BY id ASC LIMIT ?",
            (since, limit),
        ).fetchall()
        events = []
        cursor = since
        for row in rows:
            try:
                payload = json.loads(row["payload"]) if row["payload"] else None
            except Exception:
                payload = None
            events.append(
                {
                    "id": row["id"],
                    "task_id": row["task_id"],
                    "run_id": row["run_id"],
                    "kind": row["kind"],
                    "payload": payload,
                    "created_at": row["created_at"],
                }
            )
            cursor = int(row["id"])
        latest = _latest_event_id(conn)
        if not events:
            cursor = latest if since >= latest else since
        return {
            "events": events,
            "cursor": cursor,
            "latest_event_id": cursor,
            "read_only": False,
        }


def _stats_payload(*, board=None):
    """Return per-status and per-assignee task counts for the board."""
    kb = _kb()
    with _conn(board=board) as conn:
        if hasattr(kb, "board_stats"):
            return kb.board_stats(conn)
        rows = conn.execute(
            "SELECT status, assignee, COUNT(*) AS n FROM tasks WHERE status != 'archived' GROUP BY status, assignee"
        ).fetchall()
        by_status = {}
        by_assignee = {}
        for row in rows:
            n = int(row["n"] or 0)
            by_status[row["status"]] = by_status.get(row["status"], 0) + n
            assignee = row["assignee"] or "unassigned"
            by_assignee[assignee] = by_assignee.get(assignee, 0) + n
        return {"by_status": by_status, "by_assignee": by_assignee}


def _assignees_payload(*, board=None):
    """Return the list of known assignees derived from task history."""
    kb = _kb()
    with _conn(board=board) as conn:
        try:
            assignees = list(kb.known_assignees(conn))
        except Exception:
            rows = conn.execute(
                "SELECT DISTINCT assignee FROM tasks WHERE assignee IS NOT NULL AND assignee != '' ORDER BY assignee"
            ).fetchall()
            assignees = [row["assignee"] for row in rows]
    return {"assignees": assignees}


def _task_log_payload(parsed, task_id: str):
    """Return the raw worker log content and on-disk metadata for a task's dispatcher run."""
    board = _resolve_board(parsed)
    kb = _kb()
    tail = _int_query(parsed, "tail", None, minimum=1, maximum=2_000_000)
    with _conn(board=board) as conn:
        if not kb.get_task(conn, task_id):
            return None
    if not hasattr(kb, "read_worker_log"):
        return {
            "task_id": task_id,
            "path": "",
            "exists": False,
            "size_bytes": 0,
            "content": "",
            "truncated": False,
        }
    content = kb.read_worker_log(task_id, tail_bytes=tail)
    log_path = kb.worker_log_path(task_id) if hasattr(kb, "worker_log_path") else None
    try:
        size = log_path.stat().st_size if log_path and log_path.exists() else 0
    except OSError:
        size = 0
    return {
        "task_id": task_id,
        "path": str(log_path or ""),
        "exists": content is not None,
        "size_bytes": size,
        "content": content or "",
        "truncated": bool(tail and size > tail),
    }
