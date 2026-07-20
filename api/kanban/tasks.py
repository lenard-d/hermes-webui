"""Kanban task commands and dispatcher operations."""

from __future__ import annotations

import json
import time
from dataclasses import asdict

from .integration import _conn, _kb, _task_dict
from .validation import _bool_query, _int_query, _resolve_board, _validate_status


def _set_status_direct(conn, task_id: str, new_status: str) -> bool:
    """Direct status write for drag-drop moves not covered by structured verbs.

    Used for ``todo <-> ready`` and ``running -> ready`` transitions. The
    structured verbs (``complete_task``, ``block_task``, ``unblock_task``,
    ``archive_task``, ``claim_task``) own their own state changes; this helper
    handles the remainder while preserving the dispatcher's contract:

    - When transitioning OFF ``running`` to anything other than the terminal
      verbs, claim_lock / claim_expires / worker_pid are nulled so the
      dispatcher doesn't see a phantom-running task. The active run (if any)
      is closed with ``outcome='reclaimed'`` so attempt history isn't
      orphaned.
    - When transitioning INTO ``running``, claim fields are preserved (this
      function is NOT used for entering 'running' — that goes through
      ``kb.claim_task()`` and the bridge rejects raw 'running' status writes
      with HTTP 400).

    Mirrors the agent dashboard plugin's ``_set_status_direct``
    (plugins/kanban/dashboard/plugin_api.py) so first-party clients see
    identical behaviour from either surface.
    """
    kb = _kb()
    with kb.write_txn(conn):
        prev = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if prev is None:
            return False
        was_running = prev["status"] == "running"
        cur = conn.execute(
            "UPDATE tasks SET status = ?, "
            "  claim_lock = CASE WHEN ? = 'running' THEN claim_lock ELSE NULL END, "
            "  claim_expires = CASE WHEN ? = 'running' THEN claim_expires ELSE NULL END, "
            "  worker_pid = CASE WHEN ? = 'running' THEN worker_pid ELSE NULL END "
            "WHERE id = ?",
            (new_status, new_status, new_status, new_status, task_id),
        )
        if cur.rowcount != 1:
            return False
        run_id = None
        if was_running and new_status != "running" and prev["current_run_id"]:
            try:
                run_id = kb._end_run(
                    conn,
                    task_id,
                    outcome="reclaimed",
                    status="reclaimed",
                    summary=f"status changed to {new_status} (webui/direct)",
                )
            except Exception:
                # _end_run is best-effort here; the status flip itself is
                # what matters for sidebar rendering.
                run_id = None
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, 'status', ?, ?)",
            (
                task_id,
                run_id,
                json.dumps({"status": new_status, "source": "webui"}),
                int(time.time()),
            ),
        )
    if new_status in ("done", "ready") and hasattr(kb, "recompute_ready"):
        try:
            kb.recompute_ready(conn)
        except Exception:
            pass
    return True


def _create_task_payload(body: dict, *, board=None):
    """Create a new task from a parsed request body and return the task dict in a read_only envelope."""
    title = str(body.get("title") or "").strip()
    if not title:
        raise ValueError("title is required")
    try:
        priority = int(body.get("priority") or 0)
    except (TypeError, ValueError):
        raise ValueError("priority must be an integer") from None
    kb = _kb()
    requested_status = body.get("status")
    with _conn(board=board) as conn:
        task_id = kb.create_task(
            conn,
            title=title,
            body=body.get("body") or None,
            assignee=body.get("assignee") or None,
            created_by=body.get("created_by") or "webui",
            tenant=body.get("tenant") or None,
            priority=priority,
            parents=body.get("parents") or (),
            triage=bool(body.get("triage") or False),
            workspace_kind=body.get("workspace_kind") or "scratch",
            workspace_path=body.get("workspace_path") or None,
            idempotency_key=body.get("idempotency_key") or None,
            max_runtime_seconds=body.get("max_runtime_seconds") or None,
            skills=body.get("skills") or None,
        )
        if requested_status:
            _patch_task(conn, task_id, {"status": requested_status})
        return {"task": _task_dict(kb.get_task(conn, task_id)), "read_only": False}


def _patch_task(conn, task_id: str, body: dict):
    """Apply a partial update to a task, routing status transitions through structured verbs (complete, block, archive)."""
    kb = _kb()
    task = kb.get_task(conn, task_id)
    if not task:
        raise LookupError("task not found")

    updates = {}
    if "title" in body:
        title = str(body.get("title") or "").strip()
        if not title:
            raise ValueError("title is required")
        updates["title"] = title
    if "body" in body:
        updates["body"] = body.get("body") or None
    if "tenant" in body:
        updates["tenant"] = body.get("tenant") or None
    if "priority" in body:
        try:
            updates["priority"] = int(body.get("priority") or 0)
        except (TypeError, ValueError):
            raise ValueError("priority must be an integer") from None

    for field, value in updates.items():
        if hasattr(task, field):
            try:
                setattr(task, field, value)
            except Exception:
                pass
    if updates:
        assignments = ", ".join(f"{field} = ?" for field in updates)
        conn.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ?", [*updates.values(), task_id]
        )
        if hasattr(kb, "_append_event"):
            kb._append_event(
                conn, task_id, "updated", {"fields": list(updates), "source": "webui"}
            )

    if "assignee" in body:
        if not kb.assign_task(conn, task_id, body.get("assignee") or None):
            raise LookupError("task not found")

    if "status" not in body or body.get("status") in (None, ""):
        return
    status = _validate_status(body.get("status"))
    if status == "done":
        if not kb.complete_task(
            conn, task_id, result=body.get("result"), summary=body.get("summary")
        ):
            raise LookupError("task not found")
    elif status == "blocked":
        if not kb.block_task(
            conn, task_id, reason=body.get("block_reason") or body.get("reason")
        ):
            raise LookupError("task not found")
    elif status == "archived":
        if not kb.archive_task(conn, task_id):
            raise LookupError("task not found")
    elif status == "running":
        # The 'running' state is owned by the kanban dispatcher / claim
        # protocol — entering it via raw UPDATE bypasses claim_lock,
        # claim_expires, started_at, and worker_pid, which leaves the task
        # in a state the dispatcher treats as "phantom claimed" and may
        # reclaim or hide. Match the agent dashboard plugin's contract
        # (plugins/kanban/dashboard/plugin_api.py update_task) by rejecting
        # this transition with HTTP 400. Workers enter 'running' via
        # kb.claim_task(); UI users should use the dispatcher nudge.
        raise ValueError(
            "Cannot set status to 'running' directly; use the dispatcher/claim path"
        )
    elif status == "ready":
        # If the task is currently 'blocked', use the structured unblock
        # verb so the unblocked event fires. Otherwise it's a legitimate
        # drag-drop or click move (e.g. todo → ready, running → ready when
        # the user yanks a stuck worker back to the queue) and we use the
        # claim-aware direct status write.
        current = kb.get_task(conn, task_id)
        if not current:
            raise LookupError("task not found")
        if current.status == "blocked":
            if not kb.unblock_task(conn, task_id):
                raise LookupError("task not found")
        else:
            if not _set_status_direct(conn, task_id, "ready"):
                raise LookupError("task not found")
    elif status in ("triage", "todo"):
        # Direct status write for drag-drop moves between non-running,
        # non-terminal columns. Uses the claim-aware helper that nulls out
        # claim_lock / claim_expires / worker_pid when leaving 'running'
        # and ends any active run with outcome='reclaimed'.
        if not _set_status_direct(conn, task_id, status):
            raise LookupError("task not found")
    else:
        # _validate_status guarantees we never reach here, but be defensive.
        raise ValueError(f"unknown status: {status}")


def _patch_task_payload(task_id: str, body: dict, *, board=None):
    """Validate task_id, open a connection, and delegate field-level updates to _patch_task."""
    task_id = str(task_id or "").strip()
    if not task_id:
        raise ValueError("task_id is required")
    kb = _kb()
    with _conn(board=board) as conn:
        _patch_task(conn, task_id, body)
        return {"task": _task_dict(kb.get_task(conn, task_id)), "read_only": False}


def _comment_payload(task_id: str, body: dict, *, board=None):
    """Add a comment to a task and return the new comment_id in a read_only envelope."""
    task_id = str(task_id or "").strip()
    comment_body = str(body.get("body") or "").strip()
    if not task_id:
        raise ValueError("task_id is required")
    if not comment_body:
        raise ValueError("body is required")
    kb = _kb()
    with _conn(board=board) as conn:
        if not kb.get_task(conn, task_id):
            raise LookupError("task not found")
        comment_id = kb.add_comment(
            conn, task_id, body.get("author") or "webui", comment_body
        )
        return {"ok": True, "comment_id": comment_id, "read_only": False}


def _link_tasks_payload(body: dict, *, unlink: bool = False, board=None):
    """Create or delete a parent-child dependency link between two tasks."""
    parent_id = str(body.get("parent_id") or "").strip()
    child_id = str(body.get("child_id") or "").strip()
    if not parent_id or not child_id:
        raise ValueError("parent_id and child_id are required")
    kb = _kb()
    with _conn(board=board) as conn:
        if not kb.get_task(conn, parent_id):
            raise LookupError("parent task not found")
        if not kb.get_task(conn, child_id):
            raise LookupError("child task not found")
        if unlink:
            changed = kb.unlink_tasks(conn, parent_id, child_id)
            return {
                "ok": True,
                "changed": bool(changed),
                "parent_id": parent_id,
                "child_id": child_id,
                "read_only": False,
            }
        kb.link_tasks(conn, parent_id, child_id)
        return {
            "ok": True,
            "parent_id": parent_id,
            "child_id": child_id,
            "read_only": False,
        }


def _bulk_tasks_payload(body: dict, *, board=None):
    """Apply a common mutation (archive/status/assignee/priority) to multiple task ids in a single transaction."""
    ids = [str(i).strip() for i in (body.get("ids") or []) if str(i).strip()]
    if not ids:
        raise ValueError("ids is required")
    results = []
    kb = _kb()
    with _conn(board=board) as conn:
        for task_id in ids:
            entry = {"id": task_id, "ok": True}
            try:
                if not kb.get_task(conn, task_id):
                    entry.update(ok=False, error="not found")
                    results.append(entry)
                    continue
                if body.get("archive"):
                    if not kb.archive_task(conn, task_id):
                        entry.update(ok=False, error="archive refused")
                elif body.get("status") is not None:
                    _patch_task(conn, task_id, {"status": body.get("status")})
                if body.get("assignee") is not None:
                    if not kb.assign_task(conn, task_id, body.get("assignee") or None):
                        entry.update(ok=False, error="assign refused")
                if body.get("priority") is not None:
                    try:
                        priority = int(body.get("priority"))
                    except (TypeError, ValueError):
                        entry.update(ok=False, error="priority must be an integer")
                    else:
                        conn.execute(
                            "UPDATE tasks SET priority = ? WHERE id = ?",
                            (priority, task_id),
                        )
                        if hasattr(kb, "_append_event"):
                            kb._append_event(
                                conn,
                                task_id,
                                "reprioritized",
                                {"priority": priority, "source": "webui"},
                            )
            except Exception as exc:
                entry.update(ok=False, error=str(exc))
            results.append(entry)
    return {"results": results, "read_only": False}


def _dispatch_payload(parsed):
    """Trigger a single-pass kanban dispatcher run and return the dispatch result."""
    board = _resolve_board(parsed)
    kb = _kb()
    dry_run = _bool_query(parsed, "dry_run", False)
    max_spawn = _int_query(parsed, "max", 8, minimum=1, maximum=100)
    if not hasattr(kb, "dispatch_once"):
        raise ValueError("dispatcher is unavailable")
    with _conn(board=board) as conn:
        result = kb.dispatch_once(conn, dry_run=dry_run, max_spawn=max_spawn)
    if isinstance(result, dict):
        return result
    try:
        return asdict(result)
    except TypeError:
        return {"result": str(result)}


def _task_action_payload(task_id: str, body: dict, action: str, *, board=None):
    """Execute a named action (block or unblock) on a task and return the updated task dict."""
    kb = _kb()
    task_id = str(task_id or "").strip()
    if not task_id:
        raise ValueError("task_id is required")
    with _conn(board=board) as conn:
        if not kb.get_task(conn, task_id):
            raise LookupError("task not found")
        if action == "block":
            ok = kb.block_task(
                conn, task_id, reason=body.get("reason") or body.get("block_reason")
            )
        elif action == "unblock":
            if hasattr(kb, "unblock_task"):
                ok = kb.unblock_task(conn, task_id)
            else:
                _patch_task(conn, task_id, {"status": "ready"})
                ok = True
        else:
            raise ValueError(f"invalid action: {action}")
        if not ok:
            raise RuntimeError(f"{action} refused")
        return {"task": _task_dict(kb.get_task(conn, task_id)), "read_only": False}
