"""Crash-safe session sidecar recovery Interface and CLI."""

# Recovery tooling historically imported helper seams from this module.  Keep
# those explicit compatibility exports while the implementation lives with its
# durable-backup, deletion, materialization, and audit owners.
# ruff: noqa: F401

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)
from .recovery_audit import audit_session_recovery
from .recovery_backups import (
    _orphaned_backup_live_paths,
    _rebuild_recovery_session_index,
    inspect_session_recovery_status,  # noqa: F401 - compatibility re-export
    recover_session,
)
from .recovery_materialization import recover_missing_sidecars_from_state_db

# Compatibility exports for existing recovery tooling and tests.
from .recovery_backups import (
    _backup_predates_intentional_shrink,
    _live_supersedes_backup_by_clear_generation,
    _msg_count,
    _session_records_clear_sentinel,
    _session_records_intentional_compress_shrink,
    _state_db_has_session,
)  # noqa: F401
from .recovery_deletion import (
    _durable_tombstone_marks_deleted_webui_session,
    _index_marks_deleted_webui_session,
    _marks_deleted_webui_session,
    _read_index_session_ids,
)  # noqa: F401
from .recovery_materialization import (
    _read_state_db_missing_sidecar_rows,
    _sql_optional_col,
    _state_db_row_to_sidecar,
)  # noqa: F401


def repair_safe_session_recovery(session_dir: Path, state_db_path: Path | None = None) -> dict:
    """Run safe, deterministic session recovery repairs.

    This mutates only repairable classes already handled by startup recovery:
    shrunken live sidecars and orphan backups that are not tombstoned by a
    readable state.db. Unsafe audit findings remain for manual review.
    """
    before = audit_session_recovery(session_dir, state_db_path=state_db_path)
    backup_repair = recover_all_sessions_on_startup(
        session_dir,
        rebuild_index=True,
        state_db_path=state_db_path,
    )
    sidecar_repair = recover_missing_sidecars_from_state_db(session_dir, state_db_path)
    if sidecar_repair.get('materialized'):
        try:
            _rebuild_recovery_session_index(session_dir)
        except Exception as exc:
            logger.warning("repair_safe_session_recovery: index rebuild after state.db reconciliation failed: %s", exc)
    after = audit_session_recovery(session_dir, state_db_path=state_db_path)
    unsafe_remaining = int((after.get("summary") or {}).get("unsafe_to_repair") or 0)
    repairable_remaining = int((after.get("summary") or {}).get("repairable") or 0)
    clean = unsafe_remaining == 0 and repairable_remaining == 0
    return {
        "clean": clean,
        "ok": clean,
        "repaired": int(backup_repair.get("restored") or 0) + int(sidecar_repair.get("materialized") or 0),
        "before": before,
        "backup_repair": backup_repair,
        "sidecar_repair": sidecar_repair,
        "after": after,
    }


def recover_all_sessions_on_startup(
    session_dir: Path,
    rebuild_index: bool = False,
    state_db_path: Path | None = None,
) -> dict:
    """Scan session_dir for shrunken/orphaned sessions and restore from .bak.

    Returns {"scanned": N, "restored": M, "orphaned_backups": K, "details": [...]}.
    """
    if not session_dir.exists():
        return {"scanned": 0, "restored": 0, "orphaned_backups": 0, "details": []}
    restored = 0
    details: list[dict] = []
    live_paths = [path for path in sorted(session_dir.glob('*.json')) if not path.name.startswith('_')]
    orphan_paths = _orphaned_backup_live_paths(session_dir, state_db_path=state_db_path)
    # Only sessions with a backup can be restored through this startup path.
    # Older code called recover_session() for every live sidecar, and
    # inspect_session_recovery_status() read the complete JSON file before even
    # checking whether <sid>.json.bak existed. Large WebUI installs therefore
    # parsed the entire session corpus on every boot even when there was
    # nothing to recover. Keep the public scanned count compatible, but limit
    # expensive reads to actual recovery candidates.
    recovery_paths = [path for path in live_paths if path.with_suffix('.json.bak').exists()]
    scanned = len(live_paths) + len(orphan_paths)
    for path in [*recovery_paths, *orphan_paths]:
        try:
            result = recover_session(path)
        except Exception as exc:
            # Defensive: a malformed session file shouldn't break recovery
            # for the rest. Log and continue.
            logger.warning(
                "recover_all_sessions_on_startup: skipped %s due to %s: %s",
                path.name, type(exc).__name__, exc,
            )
            continue
        if result.get("restored"):
            restored += 1
            details.append(result)
    if restored:
        logger.warning(
            "recover_all_sessions_on_startup: restored %d/%d sessions from .bak. "
            "If you weren't expecting this, check the session list for missing "
            "messages — see #1558.", restored, scanned,
        )
    if rebuild_index:
        try:
            if restored or not (session_dir / '_index.json').exists():
                _rebuild_recovery_session_index(session_dir)
        except Exception as exc:
            logger.warning("recover_all_sessions_on_startup: index rebuild failed: %s", exc)
    return {
        "scanned": scanned,
        "restored": restored,
        "orphaned_backups": len(orphan_paths),
        "details": details,
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description="Audit Hermes WebUI session recovery state")
    parser.add_argument("--audit", action="store_true", help="run a read-only recovery audit")
    parser.add_argument("--session-dir", type=Path, required=True, help="path to WebUI sessions directory")
    parser.add_argument("--state-db", type=Path, default=None, help="optional Hermes state.db path")
    parser.add_argument("--repair-safe", action="store_true", help="run safe deterministic repairs after auditing")
    args = parser.parse_args()
    if args.repair_safe:
        report = repair_safe_session_recovery(args.session_dir, state_db_path=args.state_db)
    elif args.audit:
        report = audit_session_recovery(args.session_dir, state_db_path=args.state_db)
    else:
        parser.error("choose --audit or --repair-safe")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
