"""Gateway session-registry identity projection.

This module owns the read-only adapter for Gateway's durable session registry,
including its path resolution and stat-keyed cache.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_GATEWAY_SESSION_METADATA_CACHE: dict[str, object] = {
    "path": None,
    "mtime_ns": None,
    "identity": {},
}
_GATEWAY_SESSION_METADATA_LOCK = threading.Lock()

def _gateway_session_metadata_path() -> Path:
    """Return the active profile's Gateway session registry."""
    try:
        from api.profiles import get_active_hermes_home

        hermes_home = Path(get_active_hermes_home()).expanduser().resolve()
    except Exception:
        hermes_home = Path(
            os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))
        ).expanduser().resolve()
    return hermes_home / "sessions" / "sessions.json"


def load_gateway_session_identity_map() -> dict[str, dict]:
    """Return Gateway session identity keyed by durable Agent session id.

    This read-only projection belongs with the other external session stores;
    callers do not need to know the Gateway registry layout or cache rules.
    """
    from .sources import safe_first

    path = _gateway_session_metadata_path()
    if not path.exists():
        return {}
    try:
        stat = path.stat()
        mtime_ns = int(
            getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))
        )
        with _GATEWAY_SESSION_METADATA_LOCK:
            if (
                _GATEWAY_SESSION_METADATA_CACHE["path"] == str(path)
                and _GATEWAY_SESSION_METADATA_CACHE["mtime_ns"] == mtime_ns
            ):
                return dict(_GATEWAY_SESSION_METADATA_CACHE["identity"])
    except OSError:
        return {}

    try:
        raw_sessions = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        logger.debug("Failed to parse Gateway session metadata from %s", path, exc_info=True)
        return {}

    mapping: dict[str, dict] = {}
    if isinstance(raw_sessions, dict):
        for entry in raw_sessions.values():
            if not isinstance(entry, dict):
                continue
            session_id = safe_first(entry.get("session_id"))
            if not session_id:
                continue
            origin = entry.get("origin") if isinstance(entry.get("origin"), dict) else {}
            platform = safe_first(origin.get("platform"), entry.get("platform"))
            mapping[session_id] = {
                "session_key": safe_first(entry.get("session_key"), entry.get("key")),
                "chat_id": safe_first(origin.get("chat_id"), entry.get("chat_id")),
                "thread_id": safe_first(origin.get("thread_id"), entry.get("thread_id")),
                "chat_type": safe_first(origin.get("chat_type"), entry.get("chat_type")),
                "user_id": safe_first(origin.get("user_id"), entry.get("user_id")),
                "platform": platform,
                "raw_source": platform,
            }

    with _GATEWAY_SESSION_METADATA_LOCK:
        _GATEWAY_SESSION_METADATA_CACHE["path"] = str(path)
        _GATEWAY_SESSION_METADATA_CACHE["mtime_ns"] = mtime_ns
        _GATEWAY_SESSION_METADATA_CACHE["identity"] = mapping
    return dict(mapping)


def gateway_session_identity(session_id: str) -> dict:
    """Return one Gateway identity without exposing registry/cache details."""
    if not session_id:
        return {}
    metadata = load_gateway_session_identity_map().get(str(session_id))
    return metadata if isinstance(metadata, dict) else {}
