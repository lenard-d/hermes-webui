"""Hermes Agent Kanban persistence boundary and serialization helpers."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass


def _kb():
    """Lazily import hermes_cli.kanban_db to avoid circular imports at module load."""
    from hermes_cli import kanban_db as kb

    return kb


def _conn(board=None):
    """Initialize the kanban DB for the given board slug and return a context manager
    that yields a sqlite connection and CLOSES it on exit.

    Must be ``kb.connect_closing`` — a raw ``kb.connect()`` connection used as
    ``with _conn(...) as conn:`` only gets sqlite3's transaction-scope context
    manager, which never closes the file descriptor. In this long-lived server
    that leaks one FD per request and pins stale WAL snapshots (FDs to deleted
    ``-wal``/``-shm`` files), which starves SQLite checkpoints on the shared
    kanban DB and aggravates probe⇄checkpoint contention for every process.
    """
    kb = _kb()
    kb.init_db(board=board)
    closing = getattr(kb, "connect_closing", None)
    if closing is not None:
        return closing(board=board)
    # Older kanban_db builds (and lightweight test doubles) without
    # connect_closing: fall back to the raw connection; sqlite3's own
    # context manager at least scopes the transaction.
    return kb.connect(board=board)


def _obj_dict(value):
    """Coerce a dataclass or arbitrary object to a plain dict; returns None unchanged."""
    if value is None:
        return None
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return dict(value)
    return dict(getattr(value, "__dict__", {}))


def _task_dict(task):
    """Convert a task to a JSON-serialisable dict, annotating it with computed age_seconds and progress fields."""
    data = _obj_dict(task)
    if not data:
        return data
    try:
        age = _kb().task_age(task)
    except Exception:
        age = None
    data["age_seconds"] = age
    data["age"] = age
    data.setdefault("progress", None)
    return data


def _latest_event_id(conn) -> int:
    """Return the highest event id in task_events, falling back to 0 when the table is empty."""
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS latest FROM task_events"
        ).fetchone()
        return int(row["latest"] or 0)
    except Exception:
        return 0
