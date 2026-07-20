"""Bounded cache for fully projected session-detail tail responses."""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from pathlib import Path

from api.config import SETTINGS_FILE, get_config_path
from api.profiles import get_active_hermes_home

from .records import SESSION_DIR, is_safe_session_id
from .sources import (
    is_messaging_session_record,
    requires_external_metadata_lookup,
)
from .state_db import _active_state_db_path


_CACHE_VERSION = 1
_MAX_ENTRIES = 24
_MAX_BYTES = 24 * 1024 * 1024
_MAX_ENTRY_BYTES = 2 * 1024 * 1024
_CACHE: "OrderedDict[tuple, tuple[dict, int]]" = OrderedDict()
_CACHE_BYTES = 0
_CACHE_LOCK = threading.Lock()


def _active_profile_config_path() -> Path:
    try:
        return Path(get_active_hermes_home()) / "config.yaml"
    except Exception:
        return get_config_path()


def _path_stamp(path) -> tuple | None:
    try:
        path = Path(path)
        st = path.stat()
    except (OSError, TypeError, ValueError):
        return None
    return (
        str(path),
        int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))),
        int(st.st_size),
        int(getattr(st, "st_ctime_ns", int(st.st_ctime * 1_000_000_000))),
    )


def _source_stamp(session_id: str) -> tuple | None:
    if not is_safe_session_id(session_id):
        return None
    sidecar_stamp = _path_stamp(SESSION_DIR / f"{session_id}.json")
    if sidecar_stamp is None:
        return None
    try:
        state_db_path = Path(_active_state_db_path())
    except Exception:
        state_db_path = None
    state_stamps = ()
    if state_db_path is not None:
        state_stamps = tuple(
            _path_stamp(path)
            for path in (
                state_db_path,
                Path(f"{state_db_path}-wal"),
                Path(f"{state_db_path}-shm"),
            )
        )
    try:
        profile_config_path = _active_profile_config_path()
    except Exception:
        profile_config_path = None
    return (
        sidecar_stamp,
        state_stamps,
        _path_stamp(SETTINGS_FILE),
        _path_stamp(profile_config_path),
    )


def eligible(session) -> bool:
    """Return whether an immutable idle WebUI tail can be cached safely."""
    if session is None:
        return False
    if getattr(session, "active_stream_id", None):
        return False
    if getattr(session, "pending_user_message", None) or getattr(
        session, "pending_started_at", None
    ):
        return False
    if getattr(session, "parent_session_id", None) or getattr(
        session, "pre_compression_snapshot", False
    ):
        return False
    if getattr(session, "truncation_watermark", None) not in (None, ""):
        return False
    if getattr(session, "truncation_boundary", None) not in (None, ""):
        return False
    if getattr(session, "read_only", False) or getattr(
        session, "is_cli_session", False
    ):
        return False
    if is_messaging_session_record(session) or requires_external_metadata_lookup(
        session
    ):
        return False
    source = str(getattr(session, "session_source", "") or "").strip().lower()
    return source in ("", "webui")


def key(session, *, msg_limit: int, expand_renderable: bool) -> tuple | None:
    """Build a cache key that includes every mutable projection input."""
    if not eligible(session):
        return None
    session_id = str(getattr(session, "session_id", "") or "")
    source_stamp = _source_stamp(session_id)
    if source_stamp is None:
        return None
    return (
        _CACHE_VERSION,
        session_id,
        str(getattr(session, "profile", "") or ""),
        max(1, int(msg_limit)),
        bool(expand_renderable),
        source_stamp,
    )


def get(cache_key: tuple | None) -> dict | None:
    """Return the cached payload and refresh its LRU position."""
    if cache_key is None:
        return None
    with _CACHE_LOCK:
        entry = _CACHE.get(cache_key)
        if entry is None:
            return None
        _CACHE.move_to_end(cache_key)
        return entry[0]


def get_for(session, *, msg_limit: int, expand_renderable: bool):
    """Return ``(key, payload)`` for one session-detail request."""
    cache_key = key(
        session,
        msg_limit=msg_limit,
        expand_renderable=expand_renderable,
    )
    return cache_key, get(cache_key)


def store(cache_key: tuple | None, payload: dict) -> None:
    """Store a bounded projected payload under ``cache_key``."""
    global _CACHE_BYTES
    if cache_key is None or not isinstance(payload, dict):
        return
    try:
        entry_bytes = len(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
    except (TypeError, ValueError):
        return
    if entry_bytes > _MAX_ENTRY_BYTES:
        return
    with _CACHE_LOCK:
        previous = _CACHE.pop(cache_key, None)
        if previous is not None:
            _CACHE_BYTES -= previous[1]
        _CACHE[cache_key] = (payload, entry_bytes)
        _CACHE_BYTES += entry_bytes
        while len(_CACHE) > _MAX_ENTRIES or _CACHE_BYTES > _MAX_BYTES:
            _old_key, (_old_payload, old_bytes) = _CACHE.popitem(last=False)
            _CACHE_BYTES -= old_bytes


def clear() -> None:
    """Invalidate every cached detail-tail projection."""
    global _CACHE_BYTES
    with _CACHE_LOCK:
        _CACHE.clear()
        _CACHE_BYTES = 0
