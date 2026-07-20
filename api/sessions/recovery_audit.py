"""Read-only audit of sidecar, backup, journal, and state database recovery."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from api.turn_journal import (
    derive_turn_journal_states,
    is_terminal_turn_event,
    iter_turn_journal_session_ids,
    read_turn_journal,
)
from .recovery_backups import _msg_count, _state_db_has_session, inspect_session_recovery_status
from .recovery_deletion import (
    _durable_tombstone_marks_deleted_webui_session,
    _read_index_session_ids,
)
from .recovery_materialization import _read_state_db_missing_sidecar_rows

logger = logging.getLogger(__name__)


def _new_audit_item(
    session_id: str,
    kind: str,
    category: str,
    recommendation: str,
    live_messages: int = -1,
    bak_messages: int = -1,
    **extra,
) -> dict:
    item = {
        "session_id": session_id,
        "kind": kind,
        "category": category,
        "recommendation": recommendation,
        "live_messages": live_messages,
        "bak_messages": bak_messages,
    }
    item.update(extra)
    return item


def audit_session_recovery(session_dir: Path, state_db_path: Path | None = None) -> dict:
    """Read-only audit of session recovery state.

    The audit intentionally does not mutate files. It classifies only the safe
    recovery primitives this module knows how to perform: backup restores and
    derived index rebuilds. Call ``recover_all_sessions_on_startup`` separately
    for safe repairs.
    """
    if not session_dir.exists():
        return {
            "status": "ok",
            "summary": {"ok": 0, "repairable": 0, "unsafe_to_repair": 0},
            "items": [],
        }

    items: list[dict] = []
    live_paths = sorted(p for p in session_dir.glob('*.json') if not p.name.startswith('_'))
    live_ids = {p.stem for p in live_paths}
    state_db_missing_rows = _read_state_db_missing_sidecar_rows(
        session_dir,
        state_db_path,
        include_empty=True,
    )
    state_db_deleted_webui_ids = {
        str(row.get('id') or '')
        for row in state_db_missing_rows
        if row.get('_state_db_deleted_webui_tombstone')
    }

    for live_path in live_paths:
        status = inspect_session_recovery_status(live_path)
        if status.get('recommend') == 'restore':
            items.append(_new_audit_item(
                status['session_id'],
                "shrunken_live",
                "repairable",
                "restore_from_bak",
                status.get('live_messages', -1),
                status.get('bak_messages', -1),
            ))

    # Track sids already classified as deleted-webui-tombstone from the orphan
    # .bak branch below, so the later state_db_missing_rows loop doesn't emit a
    # duplicate audit item for the same sid (both a surviving .bak and a state.db
    # row can exist for one deleted session). (#5504)
    _bak_tombstoned_ids: set[str] = set()

    for bak_path in sorted(session_dir.glob('*.json.bak')):
        live_path = bak_path.with_suffix('')
        if live_path.exists() or live_path.name.startswith('_'):
            continue
        bak_messages = _msg_count(bak_path)
        session_id = live_path.stem
        if bak_messages < 0:
            items.append(_new_audit_item(
                session_id, "malformed_orphan_backup", "unsafe_to_repair", "manual_review", -1, bak_messages
            ))
        elif _durable_tombstone_marks_deleted_webui_session(session_dir, session_id):
            # The user deleted this WebUI session; its surviving .bak must NOT be
            # reported as repairable. DURABLE tombstone only — the _index.json
            # heuristic must not suppress a genuine crash whose index survived
            # (that case is legitimately repairable). Matches the startup-recovery
            # skip + the state.db recovery path (#5504 Codex/Opus finding).
            _bak_tombstoned_ids.add(session_id)
            items.append(_new_audit_item(
                session_id,
                "state_db_deleted_webui_tombstone",
                "unsafe_to_repair",
                "deleted_session_skipped",
                -1,
                bak_messages,
            ))
        elif _state_db_has_session(session_id, state_db_path):
            items.append(_new_audit_item(
                session_id, "orphan_backup", "repairable", "restore_from_bak", -1, bak_messages
            ))
        else:
            items.append(_new_audit_item(
                session_id,
                "orphan_backup_without_state_row",
                "unsafe_to_repair",
                "manual_review",
                -1,
                bak_messages,
            ))

    index_path = session_dir / '_index.json'
    if index_path.exists():
        index_ids = _read_index_session_ids(index_path)
        for session_id in sorted(index_ids - live_ids):
            if (
                session_id in state_db_deleted_webui_ids
                or _durable_tombstone_marks_deleted_webui_session(session_dir, session_id)
            ):
                continue
            items.append(_new_audit_item(
                session_id, "index_missing_file", "repairable", "rebuild_index"
            ))
        for session_id in sorted(live_ids - index_ids):
            items.append(_new_audit_item(
                session_id, "index_missing_entry", "repairable", "rebuild_index",
                _msg_count(session_dir / f"{session_id}.json"), -1,
            ))

    for row in state_db_missing_rows:
        sid = str(row.get('id') or '')
        if sid in _bak_tombstoned_ids:
            # Already emitted a deleted-webui-tombstone audit item from the
            # orphan .bak branch above (surviving .bak + state.db row both exist
            # for this one deleted session) — don't double-count. (#5504)
            continue
        if row.get('_state_db_deleted_webui_tombstone'):
            items.append(_new_audit_item(
                sid,
                "state_db_deleted_webui_tombstone",
                "unsafe_to_repair",
                "deleted_session_skipped",
                -1,
                -1,
            ))
            continue
        if row.get('_state_db_empty_messages'):
            items.append(_new_audit_item(
                sid,
                "state_db_orphan_webui_row",
                "unsafe_to_repair",
                "manual_review",
                -1,
                -1,
            ))
            continue
        items.append(_new_audit_item(
            sid,
            "state_db_missing_sidecar",
            "repairable",
            "materialize_from_state_db",
            -1,
            -1,
        ))

    for session_id in iter_turn_journal_session_ids(session_dir):
        journal = read_turn_journal(session_id, session_dir=session_dir)
        states, _ = derive_turn_journal_states(journal.get('events') or [])
        live_path = session_dir / f"{session_id}.json"
        live_messages = _msg_count(live_path)
        existing_user_messages: set[str] = set()
        try:
            payload = json.loads(live_path.read_text(encoding='utf-8'))
            if isinstance(payload, dict):
                for message in payload.get('messages') or []:
                    if isinstance(message, dict) and message.get('role') == 'user':
                        existing_user_messages.add(str(message.get('content') or '').strip())
        except (OSError, json.JSONDecodeError, ValueError):
            pass
        for turn_id, event in sorted(states.items()):
            if is_terminal_turn_event(event):
                continue
            content = str(event.get('content') or '').strip()
            if not content or content in existing_user_messages:
                continue
            items.append(_new_audit_item(
                session_id,
                "turn_journal_pending_turn",
                "repairable",
                "audit_only_pending_turn_journal",
                live_messages,
                -1,
                turn_id=turn_id,
                event=str(event.get('event') or ''),
            ))

    summary = {"ok": len(live_paths), "repairable": 0, "unsafe_to_repair": 0}
    for item in items:
        category = item.get('category')
        if category in summary:
            summary[category] += 1
    if summary["unsafe_to_repair"]:
        overall = "needs_manual_review"
    elif summary["repairable"]:
        overall = "warn"
    else:
        overall = "ok"
    return {"status": overall, "summary": summary, "items": items}
