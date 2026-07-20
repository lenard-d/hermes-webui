"""Durable deletion signals used to prevent recovery resurrection."""

from __future__ import annotations

import json
import logging
from pathlib import Path


logger = logging.getLogger(__name__)


def _read_index_session_ids(index_path: Path) -> set[str]:
    try:
        data = json.loads(index_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, ValueError):
        return set()
    if not isinstance(data, list):
        return set()
    ids: set[str] = set()
    for entry in data:
        if isinstance(entry, dict) and isinstance(entry.get('session_id'), str):
            ids.add(entry['session_id'])
    return ids


def _index_marks_deleted_webui_session(session_dir: Path, sid: str) -> bool:
    """Return True when _index.json has a WebUI-like entry whose sidecar is missing.

    This is a delete-route heuristic for cases where index pruning and durable
    tombstone recording both failed; it can also match other sidecar-loss modes.
    """
    if not sid or (session_dir / f"{sid}.json").exists():
        return False
    index_path = session_dir / '_index.json'
    if not index_path.exists():
        return False
    try:
        data = json.loads(index_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(data, list):
        return False
    for entry in data:
        if not isinstance(entry, dict) or entry.get('session_id') != sid:
            continue
        srcs = [
            str(entry.get('source_tag') or '').strip().lower(),
            str(entry.get('raw_source') or '').strip().lower(),
            str(entry.get('session_source') or '').strip().lower(),
        ]
        explicit = [src for src in srcs if src]
        if any(src in ('webui', 'fork') for src in explicit):
            return True
        if explicit:
            return False
        is_cli = entry.get('is_cli_session') is True
        is_read_only = bool(entry.get('read_only') or entry.get('is_read_only'))
        return not (is_cli or is_read_only)
    return False


def _durable_tombstone_marks_deleted_webui_session(session_dir: Path, sid: str) -> bool:
    """Return True when the durable WebUI delete tombstone contains sid."""
    if not sid or (session_dir / f"{sid}.json").exists():
        return False
    try:
        import api.sessions.records as _models

        if Path(_models.SESSION_DIR).resolve() == session_dir.resolve():
            return sid in _models._load_webui_deleted_session_tombstone()
    except Exception:
        pass
    tombstone_path = session_dir / '_deleted_webui_sessions.json'
    try:
        raw = json.loads(tombstone_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(raw, dict):
        return False
    try:
        version = int(raw.get('version', 0))
    except (TypeError, ValueError):
        return False
    if version != 1:
        return False
    ids = raw.get('ids')
    if not isinstance(ids, list):
        return False
    return sid in {str(value).strip() for value in ids if str(value or '').strip()}


def _marks_deleted_webui_session(session_dir: Path, sid: str) -> bool:
    return (
        _index_marks_deleted_webui_session(session_dir, sid)
        or _durable_tombstone_marks_deleted_webui_session(session_dir, sid)
    )
