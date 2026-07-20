"""Project persisted cron runs into unread completion payloads."""

from __future__ import annotations

import datetime
import sqlite3
from contextlib import closing
from pathlib import Path

from api.sessions.store import _active_state_db_path


def _normalize_job_ids(job_ids) -> list[str]:
    seen = set()
    normalized = []
    for job_id in job_ids or []:
        value = str(job_id or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def latest_session_info(
    job_ids, completed_job_ids=None
) -> dict[str, dict[str, int | str | None]]:
    """Return newest persisted cron session info keyed by completed job ID."""
    normalized = _normalize_job_ids(job_ids)
    requested = _normalize_job_ids(
        completed_job_ids if completed_job_ids is not None else job_ids
    )
    if not requested:
        return {}
    empty = {job_id: {"session_id": "", "message_count": None} for job_id in requested}
    if not normalized:
        return empty
    db_path = _active_state_db_path()
    if not db_path or not Path(db_path).exists():
        return empty
    try:
        with closing(sqlite3.connect(str(db_path))) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(sessions)")
            columns = {row[1] for row in cur.fetchall()}
            if "id" not in columns or "source" not in columns:
                return empty
            message_count = (
                "s.message_count AS message_count"
                if "message_count" in columns
                else "NULL AS message_count"
            )
            order = (
                "COALESCE(s.started_at, 0) DESC, s.id DESC"
                if "started_at" in columns
                else "s.id DESC"
            )
            cur.execute(
                f"""
                SELECT s.id, {message_count}
                FROM sessions s
                WHERE LOWER(COALESCE(s.source, '')) = 'cron'
                ORDER BY {order}
                """
            )
            results = dict(empty)
            requested_ids = set(requested)
            prefixes = {job_id: f"cron_{job_id}_" for job_id in normalized}
            for row in cur.fetchall():
                session_id = str(row["id"] or "")
                matches = [
                    job_id
                    for job_id in normalized
                    if session_id.startswith(prefixes[job_id])
                ]
                if not matches:
                    continue
                # IDs may contain one another; the longest matching ID owns it.
                job_id = max(matches, key=len)
                if job_id not in requested_ids or results[job_id]["session_id"]:
                    continue
                results[job_id] = {
                    "session_id": session_id,
                    "message_count": (
                        int(row["message_count"])
                        if row["message_count"] is not None
                        else None
                    ),
                }
                if all(info["session_id"] for info in results.values()):
                    break
            return results
    except sqlite3.Error:
        return empty


def recent_completions(jobs, since: float) -> list[dict]:
    """Project jobs completed after ``since`` and attach their newest session."""
    completions = []
    for job in jobs:
        job_id = str(job.get("id", "") or "")
        last_run = job.get("last_run_at")
        if not last_run:
            continue
        if isinstance(last_run, str):
            try:
                completed_at = datetime.datetime.fromisoformat(
                    last_run.replace("Z", "+00:00")
                ).timestamp()
            except (ValueError, TypeError):
                continue
        else:
            completed_at = float(last_run)
        if completed_at <= since:
            continue
        completions.append(
            {
                "job_id": job_id,
                "name": job.get("name", "Unknown"),
                "status": job.get("last_status", "unknown"),
                "completed_at": completed_at,
                "toast_notifications": job.get("toast_notifications") is not False,
            }
        )

    session_info = latest_session_info(
        [job.get("id", "") for job in jobs],
        [completion["job_id"] for completion in completions],
    )
    for completion in completions:
        info = session_info.get(completion["job_id"], {})
        completion["session_id"] = str(info.get("session_id", "") or "")
        if info.get("message_count") is not None:
            completion["message_count"] = int(info["message_count"])
    return completions
