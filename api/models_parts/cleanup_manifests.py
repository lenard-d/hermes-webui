"""Stale deletion-manifest recovery.

Loaded behind :mod:`api.models`; use that compatibility facade in callers.
"""
# The facade seeds the original monolith namespace dynamically.  Pyflakes cannot
# see those names, while the copied implementation intentionally keeps its old
# global lookups so api.models monkeypatch seams remain effective.
# ruff: noqa: F401, F811, F821, F841, B007, B023, B904, B905
from api.models_parts._compat import seed_module_globals

seed_module_globals(globals())

def _process_stale_cleanup_manifests(hermes_home) -> bool:
    """Process any leftover cleanup manifests outside a DB transaction.

    Called at the start of each delete_cli_session run, before the
    regular transaction, so a previous run whose DB commit succeeded
    but artifact cleanup failed gets another chance — even when the
    session ID being deleted this time is unrelated.

    Serialized by the caller's per-profile thread and process locks so that the
    manifest read → DB transaction → post-commit cleanup triad is
    never interleaved across concurrent delete calls. Returns ``True`` only
    when every discovered retry record was processed completely.
    """
    try:
        import sqlite3
    except ImportError:
        return False
    db_path = hermes_home / "state.db"
    sessions_dir = hermes_home / "sessions"
    if not sessions_dir.exists():
        return True
    manifests = sorted(sessions_dir.glob(".cleanup_manifest_*.json"))
    if not manifests:
        return True
    # Missing state.db is not proof that a manifested session is dead. It may
    # have been temporarily renamed, unmounted, or made inaccessible. Preserve
    # every manifest and artifact until a successful query proves absence.
    if not db_path.exists():
        return False
    cleanup_complete = True
    for mp in manifests:
        try:
            raw = mp.read_text(encoding="utf-8")
            pending_ids = json.loads(raw)
        except (OSError, ValueError, TypeError):
            # Preserve malformed/unreadable retry records. Their pending IDs
            # are unknowable, so silently deleting them would turn an
            # incomplete cleanup into a false success.
            cleanup_complete = False
            continue
        if not isinstance(pending_ids, list) or not all(
            isinstance(item, str) for item in pending_ids
        ):
            cleanup_complete = False
            continue
        pending_ids = list(pending_ids)
        if not pending_ids:
            mp.unlink(missing_ok=True)
            continue
        # Require a successful read-only query to prove absence. Opening via a
        # read-only URI also prevents SQLite from creating a fresh empty DB if
        # state.db disappears between the existence check and connect().
        try:
            db_uri = f"{db_path.resolve().as_uri()}?mode=ro"
            with closing(sqlite3.connect(db_uri, uri=True)) as conn:
                cursor = conn.execute(
                    "SELECT id FROM sessions WHERE id IN ({})".format(
                        ",".join("?" * len(pending_ids))
                    ),
                    pending_ids,
                )
                alive = {row[0] for row in cursor.fetchall()}
        except Exception:
            # Liveness query failed (missing DB, lock, timeout, I/O error).
            # Fail closed: preserve the manifest file on disk and skip it this
            # round. A later call can retry when DB state is queryable.
            cleanup_complete = False
            continue

        still_pending = []
        for removed_id in pending_ids:
            if removed_id in alive:
                # Session still exists — the previous commit never
                # reached the DB, so this manifest entry is stale.
                # Drop it silently: never propagate a stale manifest
                # into the post-commit cleanup loop where it would
                # cause the current call to report a false failure.
                continue
            if not _clean_pending_artifact(sessions_dir, removed_id):
                still_pending.append(removed_id)
        if still_pending:
            tmp = mp.with_suffix(".tmp")
            try:
                tmp.write_text(json.dumps(still_pending), encoding="utf-8")
                tmp.rename(mp)
            except OSError:
                cleanup_complete = False
        else:
            try:
                mp.unlink(missing_ok=True)
            except OSError:
                cleanup_complete = False
    return cleanup_complete


def _clean_pending_artifact(sessions_dir, removed_id):
    """Remove on-disk transcript files for one session ID, outside a
    DB transaction.  Returns True when every artifact is gone (or absent).
    """
    if not is_safe_session_id(removed_id):
        return False
    ok = True
    for suffix in (".json", ".jsonl"):
        artifact = sessions_dir / f"{removed_id}{suffix}"
        if not artifact.exists():
            continue
        try:
            artifact.unlink(missing_ok=True)
        except OSError:
            ok = False
    try:
        for path in list(sessions_dir.glob(f"request_dump_{removed_id}_*.json")):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                ok = False
    except OSError:
        ok = False
    return ok

__all__ = ['_process_stale_cleanup_manifests', '_clean_pending_artifact']
