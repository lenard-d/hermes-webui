"""Fail-closed state database to sidecar materialization."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from .recovery_deletion import _durable_tombstone_marks_deleted_webui_session

logger = logging.getLogger(__name__)


def _read_state_db_missing_sidecar_rows(
    session_dir: Path,
    state_db_path: Path | None,
    *,
    include_empty: bool = False,
) -> list[dict]:
    """Return WebUI-origin state.db rows whose JSON sidecar is missing."""
    if state_db_path is None or not state_db_path.exists():
        return []
    try:
        with sqlite3.connect(f"file:{state_db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            session_cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
            message_cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
            if not {'id', 'source'}.issubset(session_cols):
                return []
            title_expr = _sql_optional_col('title', session_cols)
            model_expr = _sql_optional_col('model', session_cols)
            started_expr = _sql_optional_col('started_at', session_cols, '0')
            parent_expr = _sql_optional_col('parent_session_id', session_cols)
            msg_count_expr = _sql_optional_col('message_count', session_cols, '0')
            workspace_expr = _sql_optional_col('workspace', session_cols)
            worktree_path_expr = _sql_optional_col('worktree_path', session_cols)
            worktree_branch_expr = _sql_optional_col('worktree_branch', session_cols)
            worktree_repo_root_expr = _sql_optional_col('worktree_repo_root', session_cols)
            worktree_created_at_expr = _sql_optional_col('worktree_created_at', session_cols)
            rows = []
            for row in conn.execute(
                f"""
                SELECT id, source, {title_expr}, {model_expr}, {started_expr},
                       {parent_expr}, {msg_count_expr}, {workspace_expr},
                       {worktree_path_expr}, {worktree_branch_expr},
                       {worktree_repo_root_expr}, {worktree_created_at_expr}
                FROM sessions
                WHERE source = 'webui'
                ORDER BY COALESCE(started_at, 0) DESC
                """
            ).fetchall():
                data = dict(row)
                sid = str(data.get('id') or '').strip()
                if not sid or (session_dir / f"{sid}.json").exists():
                    continue
                # Only the DURABLE delete tombstone suppresses state.db sidecar
                # repair. The _index.json heuristic must NOT gate this path: a
                # genuine crash that loses the sidecar while its index entry
                # survives (no durable tombstone) is exactly the case this repair
                # exists for, and on master it materialized the sidecar. Using
                # _marks_deleted_webui_session() here (which ORs in the index
                # heuristic) wrongly classified that crash as a delete and
                # stopped recovery (#5504 Codex/Opus finding).
                tombstoned = _durable_tombstone_marks_deleted_webui_session(session_dir, sid)
                if tombstoned and not include_empty:
                    continue
                message_rows: list[dict] = []
                if {'session_id', 'role', 'content'}.issubset(message_cols):
                    order = "timestamp, id" if 'timestamp' in message_cols and 'id' in message_cols else "rowid"
                    ts_expr = 'timestamp' if 'timestamp' in message_cols else 'NULL AS timestamp'
                    for msg in conn.execute(
                        f"SELECT role, content, {ts_expr} FROM messages WHERE session_id = ? ORDER BY {order}",
                        (sid,),
                    ).fetchall():
                        message = {
                            'role': msg['role'],
                            'content': msg['content'] or '',
                        }
                        if msg['timestamp'] is not None:
                            message['timestamp'] = msg['timestamp']
                        message_rows.append(message)
                if not message_rows and not include_empty:
                    continue
                data['messages'] = message_rows
                data['_state_db_empty_messages'] = not message_rows
                if tombstoned:
                    data['_state_db_deleted_webui_tombstone'] = True
                rows.append(data)
            return rows
    except Exception as exc:
        logger.debug("state_db sidecar reconciliation scan failed for %s: %s", state_db_path, exc)
        return []


def _sql_optional_col(name: str, columns: set[str], fallback: str = "NULL") -> str:
    return name if name in columns else f"{fallback} AS {name}"


def _state_db_row_to_sidecar(row: dict) -> dict:
    try:
        from api.agent_ops import normalize_agent_session_source
    except Exception:
        normalize_agent_session_source = None
    source = str(row.get('source') or '').strip().lower()
    source_meta = normalize_agent_session_source(source) if normalize_agent_session_source else {
        'raw_source': source or None,
        'session_source': source or None,
        'source_label': source.title() if source else None,
    }
    started_at = row.get('started_at') or 0
    messages = row.get('messages') if isinstance(row.get('messages'), list) else []
    last_ts = messages[-1].get('timestamp') if messages and isinstance(messages[-1], dict) else started_at
    workspace_value = row.get('workspace') or ''
    compression_recovery = row.get('compression_recovery')
    if not isinstance(compression_recovery, dict):
        compression_recovery = {}
    return {
        'session_id': row.get('id'),
        'title': row.get('title') or 'Recovered WebUI Session',
        'workspace': workspace_value if isinstance(workspace_value, str) else '',
        'message_count': row.get('message_count') if isinstance(row.get('message_count'), int) else len(messages),
        'worktree_path': row.get('worktree_path') or None,
        'worktree_branch': row.get('worktree_branch') or None,
        'worktree_repo_root': row.get('worktree_repo_root') or None,
        'worktree_created_at': row.get('worktree_created_at') or None,
        'model': row.get('model') or 'unknown',
        'model_provider': None,
        'created_at': started_at,
        'updated_at': last_ts or started_at,
        'pinned': False,
        'archived': False,
        'project_id': None,
        'profile': None,
        'input_tokens': 0,
        'output_tokens': 0,
        'estimated_cost': None,
        'personality': None,
        'active_stream_id': None,
        'pending_user_message': None,
        'pending_attachments': [],
        'pending_started_at': None,
        'compression_anchor_visible_idx': None,
        'compression_anchor_message_key': None,
        'compression_anchor_summary': None,
        'context_length': None,
        'threshold_tokens': None,
        'last_prompt_tokens': None,
        'compression_recovery': compression_recovery,
        'recommended_recovery_action': row.get('recommended_recovery_action') or None,
        'compression_recovery_source_session_id': row.get('compression_recovery_source_session_id') or None,
        'compression_recovery_action': row.get('compression_recovery_action') or None,
        'gateway_routing': None,
        'gateway_routing_history': [],
        'llm_title_generated': False,
        'parent_session_id': row.get('parent_session_id'),
        'is_cli_session': False,
        'source_tag': source or None,
        **source_meta,
        'enabled_toolsets': None,
        'composer_draft': {},
        'messages': messages,
        'tool_calls': [],
        '_recovered_from_state_db': True,
    }


def recover_missing_sidecars_from_state_db(session_dir: Path, state_db_path: Path | None) -> dict:
    """Materialize missing WebUI JSON sidecars from canonical state.db rows."""
    rows = _read_state_db_missing_sidecar_rows(session_dir, state_db_path)
    materialized = 0
    details: list[dict] = []
    session_dir.mkdir(parents=True, exist_ok=True)
    for row in rows:
        sid = str(row.get('id') or '').strip()
        if not sid:
            continue
        target = session_dir / f"{sid}.json"
        if target.exists():
            continue
        payload = _state_db_row_to_sidecar(row)
        # Per-process/per-thread tmp suffix to avoid corruption under
        # concurrent reconciliation calls (matches Session.save()
        # Session.save() convention).
        tmp_suffix = f".json.reconcile.tmp.{os.getpid()}.{threading.current_thread().ident}"
        tmp = target.with_suffix(tmp_suffix)
        detail_recorded = False
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            details.append({'session_id': sid, 'materialized': False, 'error': str(exc)})
            continue
        # Atomic create-or-fail: os.link() refuses to overwrite an existing
        # target. Closes the TOCTOU window between the target.exists() check
        # above and the rename — a concurrent Session.save() for the same SID
        # will win and we silently skip rather than overwrite a live sidecar.
        materialized_now = False
        try:
            os.link(str(tmp), str(target))
            materialized_now = True
        except FileExistsError:
            # Live sidecar appeared between the check and the link — keep it.
            pass
        except OSError as exc:
            details.append({'session_id': sid, 'materialized': False, 'error': str(exc)})
            detail_recorded = True
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        if materialized_now:
            materialized += 1
            details.append({'session_id': sid, 'materialized': True, 'messages': len(payload.get('messages') or [])})
        elif not detail_recorded:
            details.append({'session_id': sid, 'materialized': False, 'skipped': 'sidecar_appeared_during_reconcile'})
    return {'scanned': len(rows), 'materialized': materialized, 'details': details}
