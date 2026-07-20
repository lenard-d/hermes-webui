"""Resolve visible continuations for hidden pre-compression snapshots."""

from __future__ import annotations

import json

from api.profiles import _profiles_match

from .records import (
    LOCK,
    SESSIONS,
    SESSION_DIR,
    SESSION_INDEX_FILE,
    Session,
    is_safe_session_id,
)
from .sources import safe_first


def continuation_session_id(session) -> str | None:
    """Return the newest visible descendant for a hidden compression snapshot."""
    if not getattr(session, "pre_compression_snapshot", False):
        return None
    session_id = safe_first(getattr(session, "session_id", None))
    if not session_id:
        return None
    snapshot_profile = getattr(session, "profile", None)

    def child_rows_from_memory(seen_ids: set[str]) -> list:
        rows = []
        try:
            with LOCK:
                memory_sessions = list(SESSIONS.values())
            for child in memory_sessions:
                child_id = safe_first(getattr(child, "session_id", None))
                if not child_id or child_id in seen_ids:
                    continue
                seen_ids.add(child_id)
                rows.append(child)
        except Exception:
            pass
        return rows

    def child_rows_from_index(seen_ids: set[str]) -> list | None:
        if not SESSION_INDEX_FILE.exists():
            return None
        try:
            entries = json.loads(SESSION_INDEX_FILE.read_bytes())
        except Exception:
            return None
        if not isinstance(entries, list):
            return None
        try:
            persisted_sidecar_ids = {
                path.stem
                for path in SESSION_DIR.glob("*.json")
                if not path.name.startswith("_") and is_safe_session_id(path.stem)
            }
        except Exception:
            return None
        indexed_ids: set[str] = set()
        row_seen_ids = set(seen_ids)
        rows = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            child_id = safe_first(entry.get("session_id"))
            if not child_id or not is_safe_session_id(child_id):
                continue
            indexed_ids.add(child_id)
            if child_id in row_seen_ids or not safe_first(
                entry.get("parent_session_id")
            ):
                continue
            row_seen_ids.add(child_id)
            rows.append(entry)
        if persisted_sidecar_ids - indexed_ids - seen_ids:
            return None
        return rows

    def child_rows_from_sidecars(seen_ids: set[str]) -> list:
        rows = []
        try:
            for path in SESSION_DIR.glob("*.json"):
                if path.name.startswith("_"):
                    continue
                child_id = path.stem
                if not child_id or child_id in seen_ids:
                    continue
                child = Session.load_metadata_only(child_id)
                if child:
                    seen_ids.add(child_id)
                    rows.append(child)
        except Exception:
            pass
        return rows

    def row_value(row, key, default=None):
        if isinstance(row, dict):
            return row.get(key, default)
        return getattr(row, key, default)

    def row_has_backing_state(row) -> bool:
        child_id = safe_first(row_value(row, "session_id"))
        if not child_id or not is_safe_session_id(child_id):
            return False
        if not isinstance(row, dict):
            return True
        return (SESSION_DIR / f"{child_id}.json").exists()

    def resolve_from_rows(rows: list) -> str | None:
        children_by_parent: dict[str, list] = {}
        for child in rows:
            parent_id = safe_first(row_value(child, "parent_session_id"))
            child_id = safe_first(row_value(child, "session_id"))
            if not parent_id or not child_id or child_id == session_id:
                continue
            if not _profiles_match(row_value(child, "profile"), snapshot_profile):
                continue
            children_by_parent.setdefault(parent_id, []).append(child)

        candidates = []
        frontier = [session_id]
        seen = {session_id}
        for _ in range(20):
            if not frontier:
                break
            parent_id = frontier.pop(0)
            for child in children_by_parent.get(parent_id, []):
                child_id = safe_first(row_value(child, "session_id"))
                if not child_id or child_id in seen or not row_has_backing_state(child):
                    continue
                seen.add(child_id)
                if row_value(child, "pre_compression_snapshot", False):
                    frontier.append(child_id)
                else:
                    candidates.append(child)

        if not candidates:
            return None
        latest = max(
            candidates,
            key=lambda child: (
                float(
                    safe_first(
                        row_value(child, "updated_at"),
                        row_value(child, "created_at"),
                        0,
                    )
                    or 0
                ),
                str(safe_first(row_value(child, "session_id"), "") or ""),
            ),
        )
        latest_id = safe_first(row_value(latest, "session_id", None)) or None
        if latest_id and not is_safe_session_id(latest_id):
            return None
        return latest_id

    memory_seen_ids: set[str] = set()
    rows = child_rows_from_memory(memory_seen_ids)
    index_rows = child_rows_from_index(memory_seen_ids)
    if index_rows is not None:
        return resolve_from_rows(rows + index_rows)
    rows.extend(child_rows_from_sidecars(memory_seen_ids))
    return resolve_from_rows(rows)
