"""Locked CLI-session deletion.

Loaded behind :mod:`api.models`; use that compatibility facade in callers.
"""
# The facade seeds the original monolith namespace dynamically.  Pyflakes cannot
# see those names, while the copied implementation intentionally keeps its old
# global lookups so api.models monkeypatch seams remain effective.
# ruff: noqa: F401, F811, F821, F841, B007, B023, B904, B905
from api.models_parts._compat import seed_module_globals

seed_module_globals(globals())

@contextmanager
def _cleanup_manifest_process_lock(hermes_home):
    """Serialize cleanup across WebUI worker processes for one profile.

    Keep the lock file in place permanently: unlinking it while another process
    is waiting can split later callers across different inodes and defeat the
    lock. POSIX uses ``flock``; native Windows locks the first byte with
    ``msvcrt.locking``. If neither primitive exists, fail closed rather than
    running destructive cleanup without cross-process serialization.
    """
    lock_path = Path(hermes_home) / ".session_cleanup.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+b", buffering=0) as lock_file:
        if _fcntl is not None:
            _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_EX)
            try:
                yield
            finally:
                _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_UN)
            return

        if _msvcrt is not None:
            if os.fstat(lock_file.fileno()).st_size == 0:
                lock_file.write(b"\0")
            lock_file.seek(0)
            _msvcrt.locking(  # type: ignore[attr-defined]
                lock_file.fileno(), _msvcrt.LK_LOCK, 1  # type: ignore[attr-defined]
            )
            try:
                yield
            finally:
                lock_file.seek(0)
                _msvcrt.locking(  # type: ignore[attr-defined]
                    lock_file.fileno(), _msvcrt.LK_UNLCK, 1  # type: ignore[attr-defined]
                )
            return

        raise RuntimeError("cross-process session cleanup locking is unavailable")


_cleanup_manifest_locks_guard = threading.Lock()
_cleanup_manifest_locks = {}


def _cleanup_manifest_thread_lock(hermes_home):
    """Return the in-process cleanup lock for one resolved profile home."""
    key = os.fspath(Path(hermes_home))
    with _cleanup_manifest_locks_guard:
        lock = _cleanup_manifest_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _cleanup_manifest_locks[key] = lock
        return lock


def delete_cli_session(sid) -> bool:
    """Delete a CLI session while serializing manifest and DB cleanup."""
    try:
        from api.profiles import get_active_hermes_home
        hermes_home = Path(get_active_hermes_home()).expanduser().resolve()
    except Exception:
        logger.warning("Failed to resolve active profile for session delete", exc_info=True)
        return False
    try:
        with _cleanup_manifest_thread_lock(hermes_home):
            with _cleanup_manifest_process_lock(hermes_home):
                return _delete_cli_session_locked(sid, hermes_home)
    except Exception:
        logger.warning("Failed to delete CLI session %s from state.db", sid, exc_info=True)
        return False


def _delete_cli_session_locked(sid, hermes_home) -> bool:
    """Delete a CLI session from state.db using Hermes' session semantics.

    A scoped transaction implements the Agent invariant while giving branch and
    compression evidence precedence over inherited delegate metadata. Current
    Hermes Agent's canonical helper does not yet make that precedence guarantee,
    so this destructive path fails closed instead of delegating to it.

    Returns True when the requested state is absent after cleanup, False on an
    operational error.
    """
    try:
        import sqlite3
    except ImportError:
        return False

    # Process any leftover cleanup manifests from a previous failed run.
    # This runs before the DB-existence check so pending artifact
    # removals get another chance even when the current session ID
    # is unrelated.
    stale_cleanup_complete = _process_stale_cleanup_manifests(hermes_home)

    db_path = hermes_home / 'state.db'
    if not db_path.exists():
        return False

    try:
        with closing(sqlite3.connect(str(db_path))) as conn:
            conn.row_factory = sqlite3.Row
            # SQLite does not enforce foreign keys by default; enabling
            # PRAGMA foreign_keys makes the ON DELETE CASCADE clauses on
            # session_model_usage and telegram_dm_topic_bindings fire
            # automatically.  Compression locks have no FK, so they are
            # cleaned explicitly below.
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN IMMEDIATE")
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
            }
            if not {"id", "parent_session_id"}.issubset(columns):
                return False

            selected = ["id", "parent_session_id"]
            for column in (
                "model_config", "source", "end_reason", "started_at", "ended_at"
            ):
                selected.append(column if column in columns else f"NULL AS {column}")
            rows = conn.execute(f"SELECT {', '.join(selected)} FROM sessions").fetchall()

            # _delegate_from is authoritative. source=subagent is the legacy
            # compatibility signal for rows created before that marker existed,
            # but legacy branch/compression continuations must remain intact.
            records = []
            for row in rows:
                model_config = {}
                raw_model_config = row["model_config"]
                model_config_known = raw_model_config in (None, "")
                if not model_config_known:
                    try:
                        parsed_model_config = json.loads(raw_model_config)
                    except (TypeError, ValueError):
                        parsed_model_config = None
                    if isinstance(parsed_model_config, dict):
                        model_config = parsed_model_config
                        model_config_known = True
                records.append(
                    {
                        "id": row["id"],
                        "parent_id": row["parent_session_id"],
                        "delegate_from": model_config.get("_delegate_from"),
                        "branched_from": model_config.get("_branched_from"),
                        "model_config_known": model_config_known,
                        "source": row["source"],
                        "end_reason": row["end_reason"],
                        "started_at": row["started_at"],
                        "ended_at": row["ended_at"],
                    }
                )
            records_by_id = {record["id"]: record for record in records}

            def _timestamp_value(value):
                """Return a comparable UTC timestamp, or None when ambiguous."""
                if isinstance(value, bool) or value in (None, ""):
                    return None
                if isinstance(value, (int, float)):
                    numeric = float(value)
                    return numeric if math.isfinite(numeric) else None
                if isinstance(value, datetime.datetime):
                    parsed = value
                elif isinstance(value, str):
                    raw = value.strip()
                    if not raw:
                        return None
                    try:
                        numeric = float(raw)
                        return numeric if math.isfinite(numeric) else None
                    except ValueError:
                        try:
                            parsed = datetime.datetime.fromisoformat(
                                raw[:-1] + "+00:00" if raw.endswith("Z") else raw
                            )
                        except ValueError:
                            return None
                else:
                    return None
                if parsed.tzinfo is None:
                    return None
                try:
                    numeric = parsed.timestamp()
                except (OverflowError, OSError, ValueError):
                    return None
                return numeric if math.isfinite(numeric) else None

            def _must_preserve(record, parent):
                """Fail closed when branch/compression evidence is ambiguous."""
                if record["branched_from"] is not None:
                    return True
                end_reason = parent.get("end_reason")
                if end_reason == "compression":
                    return True
                if end_reason != "branched":
                    return False
                started_at = _timestamp_value(record["started_at"])
                parent_ended_at = _timestamp_value(parent.get("ended_at"))
                if started_at is None or parent_ended_at is None:
                    return True
                return started_at >= parent_ended_at

            def _lineage_parent(record):
                """Return the parent that supplies this row's lineage edge.

                ``_delegate_from`` is authoritative when present. Falling back
                to the physical parent is only valid for legacy rows without
                that marker; otherwise a compression continuation whose physical
                parent differs from its lineage parent could be deleted.
                """
                delegate_from = record["delegate_from"]
                if delegate_from is not None:
                    return records_by_id.get(delegate_from) or {}
                return records_by_id.get(record["parent_id"]) or {}

            # Once a branch/compression continuation is preserved, its physical
            # child tree is outside the delete lineage. Stale inherited delegate
            # markers must not let traversal re-enter that retained subtree.
            preserved_ids = {
                record["id"]
                for record in records
                if record["id"] != sid
                and _must_preserve(record, _lineage_parent(record))
            }
            while True:
                descendants = {
                    record["id"]
                    for record in records
                    if record["id"] != sid
                    and record["parent_id"] in preserved_ids
                }
                new_ids = descendants - preserved_ids
                if not new_ids:
                    break
                preserved_ids.update(new_ids)

            found = {sid}
            frontier = {sid}
            while frontier:
                next_frontier = set()
                for record in records:
                    row_id = record["id"]
                    if row_id in found or row_id in preserved_ids:
                        continue
                    parent_id = record["parent_id"]
                    delegate_from = record["delegate_from"]
                    linked_to_frontier = (
                        delegate_from in frontier or parent_id in frontier
                    )
                    if not linked_to_frontier:
                        continue
                    parent = _lineage_parent(record)
                    if _must_preserve(record, parent):
                        continue
                    # Explicit _delegate_from is authoritative even when a
                    # migrated legacy row lacks source='subagent'. Branch and
                    # compression evidence above still wins and preserves it.
                    if delegate_from is not None:
                        if delegate_from in frontier:
                            next_frontier.add(row_id)
                        continue
                    # Only marker-less legacy inference requires the historical
                    # source tag plus a compatible physical-parent edge.
                    if record["source"] != "subagent":
                        continue
                    if (
                        parent_id in frontier
                        and record["model_config_known"]
                    ):
                        next_frontier.add(row_id)

                found.update(next_frontier)
                frontier = next_frontier

            delegate_ids = sorted(found - {sid})
            all_removed_ids = [sid, *delegate_ids]
            placeholders = ",".join("?" * len(all_removed_ids))

            # Delete delegate children first (messages, then orphan their
            # children, then the rows themselves).
            for child_id in delegate_ids:
                conn.execute("DELETE FROM messages WHERE session_id = ?", (child_id,))
            for child_id in delegate_ids:
                conn.execute(
                    "UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id = ?",
                    (child_id,),
                )
            for child_id in delegate_ids:
                conn.execute("DELETE FROM sessions WHERE id = ?", (child_id,))

            # Preserve every remaining child as an independent row, matching
            # current SessionDB.delete_session().
            conn.execute(
                "UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id = ?",
                (sid,),
            )
            conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))

            # Referential-integrity cleanup for session-owned rows that are
            # not covered by ON DELETE CASCADE.  session_model_usage and
            # telegram_dm_topic_bindings have FK CASCADE (enforced by
            # PRAGMA foreign_keys = ON above), but compression_locks has no
            # foreign key, so stale rows would survive.  Gate each table
            # on existence so this works on older schemas too.
            table_names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "compression_locks" in table_names:
                conn.execute(
                    f"DELETE FROM compression_locks WHERE session_id IN ({placeholders})",
                    all_removed_ids,
                )
            # Belt-and-suspenders: also explicitly clear FK tables in case
            # PRAGMA foreign_keys is OFF on an older SQLite build or a
            # future schema drops the CASCADE clause.
            if "session_model_usage" in table_names:
                conn.execute(
                    f"DELETE FROM session_model_usage WHERE session_id IN ({placeholders})",
                    all_removed_ids,
                )
            if "telegram_dm_topic_bindings" in table_names:
                conn.execute(
                    f"DELETE FROM telegram_dm_topic_bindings WHERE session_id IN ({placeholders})",
                    all_removed_ids,
                )

            # Persist a cleanup manifest BEFORE the commit so artifact
            # removal is idempotent and retryable.  Each call uses a unique
            # manifest filename (atomic temp-file + rename) so concurrent
            # deletes never clobber each other's retry records.
            sessions_dir = hermes_home / "sessions"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            manifest_basename = f".cleanup_manifest_{uuid.uuid4().hex}"
            manifest_path = sessions_dir / f"{manifest_basename}.json"
            manifest_tmp = sessions_dir / f"{manifest_basename}.tmp"
            try:
                manifest_tmp.write_text(
                    json.dumps(sorted(str(i) for i in all_removed_ids)),
                    encoding="utf-8",
                )
                manifest_tmp.rename(manifest_path)
            except OSError:
                logger.warning("Failed to write cleanup manifest", exc_info=True)
                try:
                    manifest_tmp.unlink(missing_ok=True)
                except OSError:
                    logger.warning(
                        "Failed to remove incomplete cleanup manifest %s",
                        manifest_tmp,
                        exc_info=True,
                    )
                # Publishing the retry record is a prerequisite for making the
                # DB deletion durable. Without it, committed rows could vanish
                # while transcript artifacts remain forever and the UI reports
                # a false success.
                conn.rollback()
                return False

            conn.commit()

            # Post-commit artifact cleanup.  Scan ALL outstanding manifests
            # (including the one just written) and retry every pending ID.
            # Each manifest entry is re-checked against the DB so a stale
            # manifest from a failed commit never unlinks a live session's
            # artifacts.
            artifact_cleanup_failed = False

            def _is_session_alive(conn, sid):
                """True when *sid* still has a row in the sessions table.
                On any query failure, assume alive (fail closed) — an
                uncertain liveness check must never trigger artifact
                deletion on this unrecoverable state.db path.
                """
                try:
                    cursor = conn.execute(
                        "SELECT 1 FROM sessions WHERE id = ?", (sid,)
                    )
                    return cursor.fetchone() is not None
                except Exception:
                    return True

            def _clean_artifacts_for_id(sessions_dir, removed_id):
                """Remove on-disk transcript files for one session ID.
                Returns True when every artifact is gone (or was absent).
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
                        logger.warning(
                            "Failed to remove session artifact %s%s",
                            removed_id,
                            suffix,
                            exc_info=True,
                        )
                try:
                    for path in list(
                        sessions_dir.glob(f"request_dump_{removed_id}_*.json")
                    ):
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            ok = False
                            logger.warning(
                                "Failed to remove request dump %s",
                                path,
                                exc_info=True,
                            )
                except OSError:
                    ok = False
                    logger.warning(
                        "Failed to enumerate request dumps for %s",
                        removed_id,
                        exc_info=True,
                    )
                return ok

            for mp in sorted(sessions_dir.glob(".cleanup_manifest_*.json")):
                try:
                    raw = mp.read_text(encoding="utf-8")
                    pending_ids = json.loads(raw)
                except (OSError, ValueError, TypeError):
                    # The IDs are unknown, so deleting the manifest would lose
                    # the only retry record for potentially orphaned artifacts.
                    artifact_cleanup_failed = True
                    continue
                if not isinstance(pending_ids, list) or not all(
                    isinstance(item, str) for item in pending_ids
                ):
                    artifact_cleanup_failed = True
                    continue
                still_pending = []
                for removed_id in pending_ids:
                    # Transaction-outcome guard: never unlink artifacts for
                    # a session that still exists in the DB.  A stale
                    # manifest from a failed commit must not delete data
                    # belonging to a live conversation.
                    if _is_session_alive(conn, removed_id):
                        still_pending.append(removed_id)
                        continue
                    if not _clean_artifacts_for_id(sessions_dir, removed_id):
                        still_pending.append(removed_id)
                if still_pending:
                    # Atomic rewrite using temp-file + rename.
                    tmp = mp.with_suffix(".tmp")
                    try:
                        tmp.write_text(json.dumps(still_pending), encoding="utf-8")
                        tmp.rename(mp)
                    except OSError:
                        logger.warning(
                            "Failed to rewrite manifest %s", mp, exc_info=True,
                        )
                    artifact_cleanup_failed = True
                else:
                    mp.unlink(missing_ok=True)

            return stale_cleanup_complete and not artifact_cleanup_failed
    except Exception:
        logger.warning("Failed to delete CLI session %s from state.db", sid, exc_info=True)
        return False

# ---------------------------------------------------------------------------
# ``delete_cli_session`` nests each profile's cross-process file lock inside
# that profile's in-process lock so stale-manifest read, DB transaction, and
# post-commit cleanup remain one critical section without blocking unrelated
# profiles.
# ---------------------------------------------------------------------------

__all__ = ['_cleanup_manifest_process_lock', '_cleanup_manifest_locks_guard', '_cleanup_manifest_locks', '_cleanup_manifest_thread_lock', 'delete_cli_session', '_delete_cli_session_locked']
