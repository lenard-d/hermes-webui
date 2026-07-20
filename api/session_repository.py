"""Authoritative loading and persistence for mutable WebUI sessions.

Route handlers should describe a mutation, not reproduce the persistence
protocol.  ``SessionRepository.edit`` owns that protocol: acquire the
per-session lock, upgrade metadata-only projections, validate cache ownership,
and save only after a successful mutation.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator


logger = logging.getLogger(__name__)


class SessionBusyError(RuntimeError):
    """Raised when a bounded session edit cannot acquire its owner lock."""


class SessionActiveError(RuntimeError):
    """Raised when deletion would race an admitted or running turn."""

    def __init__(self, sid: str, stream_id: str) -> None:
        self.session_id = sid
        self.stream_id = stream_id
        super().__init__(f"Session {sid} has active stream {stream_id}")


class SessionWriteRejected(KeyError):
    """Raised when a durable delete forbids materializing a queued stale seed."""


@dataclass(frozen=True)
class SessionDeletionResult:
    """Observable result of deleting all WebUI-owned session state."""

    sidecar_deleted: bool
    state_db_cleanup_failed: bool


@dataclass(frozen=True)
class SessionCleanupResult:
    """Counts produced by a bounded sidecar/index reconciliation pass."""

    removed_sidecars: int
    pruned_index_rows: int
    skipped_active: int

    @property
    def cleaned(self) -> int:
        return self.removed_sidecars + self.pruned_index_rows


@dataclass(frozen=True)
class StreamWritebackResult:
    """One generation-checked stream settlement committed by the repository."""

    session: object
    value: object
    reconciled_after_save: bool
    reconciliation_error: Exception | None


class SessionRepository:
    """Coordinate safe edits of one persisted session at a time."""

    def __init__(
        self,
        *,
        load: Callable[[str], object],
        load_full: Callable[[str], object],
        lock_for: Callable[[str], object],
        cache_full: Callable[[str, object], None],
        write_index: Callable[[list[object]], None] | None = None,
        sidecar_exists: Callable[[str], bool] | None = None,
        discard_sidecar: Callable[[str], None] | None = None,
        prune_index: Callable[[str], None] | None = None,
    ) -> None:
        self._load = load
        self._load_full = load_full
        self._lock_for = lock_for
        self._cache_full = cache_full
        self._write_index = write_index
        self._sidecar_exists = sidecar_exists
        self._discard_sidecar = discard_sidecar
        self._prune_index = prune_index

    @staticmethod
    def _validate(sid: str, session: object, *, require_full: bool = False) -> object:
        if session is None:
            raise KeyError(sid)
        actual_sid = str(getattr(session, "session_id", "") or "")
        if actual_sid != sid:
            raise KeyError(sid)
        if require_full and getattr(session, "_loaded_metadata_only", False):
            raise RuntimeError(f"Full session load returned metadata-only data for {sid}")
        return session

    def _load_for_edit(self, sid: str, session: object | None) -> object:
        # Resolve again only after acquiring the per-session lock. A caller may
        # have loaded ``session`` before waiting for a streaming/checkpoint
        # writer; saving that stale object would overwrite the newer transcript.
        # The explicit object is therefore only a seed for a genuinely missing
        # record, such as a newly materialized foreign session.
        try:
            current = self._load(sid)
        except KeyError:
            current = None
        if current is None:
            current = session
        current = self._validate(sid, current)
        if not getattr(current, "_loaded_metadata_only", False):
            self._cache_full(sid, current)
            return current

        full_session = self._validate(
            sid,
            self._load_full(sid),
            require_full=True,
        )
        self._cache_full(sid, full_session)
        return full_session

    @staticmethod
    def _snapshot_session_state(session: object) -> dict[str, object]:
        """Copy mutable session fields while preserving collaborator identity."""
        snapshot = dict(getattr(session, "__dict__", {}))
        for key, value in tuple(snapshot.items()):
            if isinstance(value, (dict, list, set)):
                snapshot[key] = copy.deepcopy(value)
        return snapshot

    @staticmethod
    def _restore_session_state(session: object, snapshot: dict[str, object]) -> None:
        state = getattr(session, "__dict__", None)
        if state is None:
            raise TypeError("session writeback requires mutable object state")
        state.clear()
        state.update(snapshot)

    def get_full(self, sid: str, *, session: object | None = None) -> object:
        """Return an identity-checked full session under its owner lock."""
        sid = str(sid or "")
        if not sid:
            raise KeyError(sid)
        with self._hold_lock(sid, None):
            return self._load_for_edit(sid, session)

    @contextmanager
    def write_owner(
        self,
        sid: str,
        *,
        session: object | None = None,
        reject_when: Callable[[], bool] | None = None,
    ) -> Iterator[object]:
        """Yield the authoritative full session while holding its owner lock."""
        sid = str(sid or "")
        if not sid:
            raise KeyError(sid)
        with self._hold_lock(sid, None):
            if reject_when is not None and reject_when():
                raise SessionWriteRejected(sid)
            yield self._load_for_edit(sid, session)

    def compensate_failed_admission(
        self,
        sid: str,
        *,
        expected_stream_id: str,
        restore: Callable[[object], None],
        on_committed: Callable[[], None] | None = None,
        baseline_persisted: bool = True,
    ) -> bool:
        """Durably compensate one still-owned, unaccepted admission generation."""
        sid = str(sid or "")
        if not sid or not expected_stream_id:
            return False
        with self._hold_lock(sid, None):
            try:
                current = self._load_for_edit(sid, None)
            except KeyError:
                return False
            if getattr(current, "active_stream_id", None) != expected_stream_id:
                return False
            sidecar_exists = (
                self._sidecar_exists(sid)
                if self._sidecar_exists is not None
                else True
            )
            if baseline_persisted and not sidecar_exists:
                return False
            if not baseline_persisted and sidecar_exists:
                try:
                    persisted = self._validate(
                        sid,
                        self._load_full(sid),
                        require_full=True,
                    )
                except (KeyError, OSError):
                    return False
                if getattr(persisted, "active_stream_id", None) != expected_stream_id:
                    return False
            # The admission restore callback replaces owned fields rather than
            # mutating nested values in place. Preserve collaborator identity
            # (including test locks) if restore or persistence fails.
            before = dict(getattr(current, "__dict__", {}))
            try:
                restore(current)
                if baseline_persisted:
                    # The sidecar is the durable truth. Do not manufacture a
                    # recovery backup containing the rejected eager checkpoint.
                    current.save(
                        touch_updated_at=False,
                        skip_index=True,
                        _admission_compensation=True,
                    )
                elif sidecar_exists:
                    if self._discard_sidecar is None:
                        raise RuntimeError("admission sidecar discard is unavailable")
                    self._discard_sidecar(sid)
            except Exception:
                current.__dict__.clear()
                current.__dict__.update(before)
                raise
            if baseline_persisted and self._write_index is not None:
                try:
                    self._write_index([current])
                except Exception:
                    # The canonical sidecar is already restored.  The index is
                    # a repairable projection and must not make compensation
                    # appear to have failed after durable ownership was cleared.
                    logger.exception("Failed to refresh index after admission compensation")
            elif not baseline_persisted and self._prune_index is not None:
                try:
                    self._prune_index(sid)
                except Exception:
                    logger.exception("Failed to prune index after admission compensation")
            if on_committed is not None:
                on_committed()
            return True

    def commit_stream_writeback(
        self,
        sid: str,
        *,
        expected_stream_id: str,
        mutate: Callable[[object], object],
        reconcile_after_save: Callable[[object], bool] | None = None,
        session: object | None = None,
        touch_updated_at: bool = True,
        skip_index: bool = False,
    ) -> StreamWritebackResult | None:
        """Commit one stream generation and optionally reconcile a late signal.

        The generation check, mutation, first durable save, and one optional
        reconciliation save all run under the same per-session owner lock.
        This lets streaming backends observe cancellation immediately after the
        first save without reopening a check-then-use race.
        """
        sid = str(sid or "")
        expected_stream_id = str(expected_stream_id or "")
        if not sid:
            raise KeyError(sid)
        if not expected_stream_id:
            return None

        with self._hold_lock(sid, None):
            # An admitted stream must already have an authoritative session.
            # Never use the caller's pre-lock object as a seed here: deletion
            # may have won while the worker was queued for this owner lock.
            try:
                current = self._load_for_edit(sid, None)
            except KeyError:
                return None
            if getattr(current, "active_stream_id", None) != expected_stream_id:
                return None
            before = self._snapshot_session_state(current)
            save_kwargs = {"skip_index": True}
            if not touch_updated_at:
                save_kwargs["touch_updated_at"] = False
            try:
                value = mutate(current)
                current.save(**save_kwargs)
            except Exception:
                self._restore_session_state(current, before)
                raise

            first_commit = self._snapshot_session_state(current)
            reconciled = False
            reconciliation_error = None
            if reconcile_after_save is not None:
                try:
                    should_resave = bool(reconcile_after_save(current))
                    if should_resave:
                        current.save(**save_kwargs)
                        reconciled = True
                    else:
                        # ``False`` means the callback observed no durable
                        # reconciliation. Discard any incidental mutation so
                        # cache and index still describe the first checkpoint.
                        self._restore_session_state(current, first_commit)
                except Exception as exc:
                    # The first sidecar checkpoint is already durable. Keep the
                    # cache aligned with that known checkpoint rather than
                    # rolling all the way back to the pre-writeback generation.
                    self._restore_session_state(current, first_commit)
                    reconciliation_error = exc
                    logger.exception(
                        "Failed to reconcile stream writeback after its first save"
                    )

            if not skip_index and self._write_index is not None:
                try:
                    self._write_index([current])
                except Exception:
                    # The sidecar is canonical and already durable. The compact
                    # index is a repairable projection.
                    logger.exception("Failed to refresh index after stream writeback")
            return StreamWritebackResult(
                session=current,
                value=value,
                reconciled_after_save=reconciled,
                reconciliation_error=reconciliation_error,
            )

    @contextmanager
    def _hold_lock(self, sid: str, timeout: float | None) -> Iterator[None]:
        lock = self._lock_for(sid)
        if timeout is None:
            with lock:
                yield
            return
        if timeout < 0:
            raise ValueError("lock_timeout must be non-negative")
        if not lock.acquire(timeout=timeout):
            raise SessionBusyError(f"Session {sid} is busy")
        try:
            yield
        finally:
            lock.release()

    @contextmanager
    def edit(
        self,
        sid: str,
        *,
        session: object | None = None,
        touch_updated_at: bool = True,
        skip_index: bool = False,
        save_when: Callable[[object], bool] | None = None,
        lock_timeout: float | None = None,
    ) -> Iterator[object]:
        """Yield a full session under its lock and persist successful edits."""
        sid = str(sid or "")
        if not sid:
            raise KeyError(sid)

        with self._hold_lock(sid, lock_timeout):
            current = self._load_for_edit(sid, session)
            before = self._snapshot_session_state(current)
            try:
                yield current
            except BaseException:
                self._restore_session_state(current, before)
                raise

            if save_when is not None and not save_when(current):
                return
            save_kwargs = {}
            if not touch_updated_at:
                save_kwargs["touch_updated_at"] = False
            if skip_index:
                save_kwargs["skip_index"] = True
            current.save(**save_kwargs)


def _default_repository() -> SessionRepository:
    # Resolve collaborators at call time. ``/api/admin/reload`` can replace the
    # models module while the process is running; retaining bound callables here
    # would keep edits attached to the stale module and stale cache.
    from api import config, models

    def sidecar_path(sid: str):
        root = models.SESSION_DIR.resolve()
        path = (root / f"{sid}.json").resolve()
        path.relative_to(root)
        return path

    return SessionRepository(
        load=models.get_session,
        load_full=models.Session.load,
        lock_for=config._get_session_agent_lock,
        cache_full=models.cache_full_session,
        write_index=lambda sessions: models._write_session_index(updates=sessions),
        sidecar_exists=lambda sid: sidecar_path(sid).is_file(),
        discard_sidecar=lambda sid: sidecar_path(sid).unlink(missing_ok=True),
        prune_index=models.prune_session_from_index,
    )


@contextmanager
def edit_session(
    sid: str,
    *,
    session: object | None = None,
    touch_updated_at: bool = True,
    skip_index: bool = False,
    save_when: Callable[[object], bool] | None = None,
    lock_timeout: float | None = None,
) -> Iterator[object]:
    """Edit a session through the process-wide repository."""
    with _default_repository().edit(
        sid,
        session=session,
        touch_updated_at=touch_updated_at,
        skip_index=skip_index,
        save_when=save_when,
        lock_timeout=lock_timeout,
    ) as current:
        yield current


def get_full_session(sid: str, *, session: object | None = None) -> object:
    """Load a complete session through the process-wide repository."""
    return _default_repository().get_full(sid, session=session)


def commit_stream_writeback(
    sid: str,
    *,
    expected_stream_id: str,
    mutate: Callable[[object], object],
    reconcile_after_save: Callable[[object], bool] | None = None,
    session: object | None = None,
    touch_updated_at: bool = True,
    skip_index: bool = False,
) -> StreamWritebackResult | None:
    """Commit one stream generation through the process-wide repository."""
    return _default_repository().commit_stream_writeback(
        sid,
        expected_stream_id=expected_stream_id,
        mutate=mutate,
        reconcile_after_save=reconcile_after_save,
        session=session,
        touch_updated_at=touch_updated_at,
        skip_index=skip_index,
    )


@contextmanager
def session_write_owner(sid: str, *, session: object | None = None) -> Iterator[object]:
    """Yield authoritative session-owned state unless a durable delete won.

    The owner covers side effects that belong to the session as well as sidecar
    mutation.  Callers therefore serialize with ``delete_session_state`` and
    re-resolve durable truth only after acquiring the same per-session lock.
    """
    with _default_repository().write_owner(
        sid,
        session=session,
        reject_when=lambda: session_deleted_for_write(sid),
    ) as current:
        yield current


@contextmanager
def admission_write_owner(sid: str, *, session: object | None = None) -> Iterator[object]:
    """Yield admission's authoritative session unless a durable delete won."""
    with session_write_owner(sid, session=session) as current:
        yield current


def compensate_failed_admission(
    sid: str,
    *,
    expected_stream_id: str,
    restore: Callable[[object], None],
    on_committed: Callable[[], None] | None = None,
    baseline_persisted: bool = True,
) -> bool:
    """Restore a failed admission without retaining its provisional transcript."""
    return _default_repository().compensate_failed_admission(
        sid,
        expected_stream_id=expected_stream_id,
        restore=restore,
        on_committed=on_committed,
        baseline_persisted=baseline_persisted,
    )


def session_deleted_for_write(sid: str) -> bool:
    """Return whether a durable delete forbids a queued stale write.

    A live sidecar or current cache entry wins over an old tombstone. The
    negative case is intentionally cheap and is checked while admission holds
    the same per-session owner lock as deletion.
    """
    from api import config, models

    sid = str(sid or "")
    if not models.is_safe_session_id(sid):
        return True
    if sid not in models._load_webui_deleted_session_tombstone():
        return False
    try:
        sidecar = (models.SESSION_DIR.resolve() / f"{sid}.json").resolve()
        sidecar.relative_to(models.SESSION_DIR.resolve())
    except (OSError, ValueError):
        return True
    if sidecar.exists():
        return False
    with config.LOCK:
        return sid not in models.SESSIONS


def cleanup_session_store(*, zero_only: bool = False) -> SessionCleanupResult:
    """Remove empty sidecars and index-only ghosts as one reconciliation pass."""
    from api import config, models

    session_dir = models.SESSION_DIR
    index_file = models.SESSION_INDEX_FILE
    removed_sidecar_ids: set[str] = set()
    pruned_index_rows = 0
    skipped_active = 0

    # Phase 1: remove file-backed empty sessions matching the requested mode.
    for path in session_dir.glob("*.json"):
        if path.name.startswith("_"):
            continue
        owner_lock = config._get_session_agent_lock(path.stem)
        try:
            with owner_lock:
                # Reload only after acquiring the same lock as writers. An
                # initially empty session may have gained messages while this
                # cleanup pass waited.
                session = models.Session.load(path.stem)
                should_delete = bool(
                    session
                    and len(session.messages) == 0
                    and (zero_only or session.title == "Untitled")
                )
                if not should_delete:
                    continue
                blocking_stream = config.blocking_runtime_stream(
                    path.stem,
                    active_stream_id=getattr(session, "active_stream_id", None),
                    pending_user_message=getattr(session, "pending_user_message", None),
                    pending_started_at=getattr(session, "pending_started_at", None),
                )
                if blocking_stream:
                    skipped_active += 1
                    continue
                with config.LOCK:
                    models.SESSIONS.pop(path.stem, None)
                path.unlink(missing_ok=True)
                removed_sidecar_ids.add(path.stem)
                # Startup recovery treats an orphan .bak as recoverable state;
                # leaving it behind would undo the cleanup on the next boot.
                path.with_suffix(".json.bak").unlink(missing_ok=True)
        except Exception:
            logger.debug("Failed to clean up session file %s", path, exc_info=True)
        finally:
            # The weak registry retains this lock while any holder or waiter
            # still references it, then removes it automatically.
            del owner_lock

    phase2_rewrote_index = False
    if index_file.exists():
        try:
            with models._INDEX_WRITE_LOCK:
                index_data = json.loads(index_file.read_bytes())
                if isinstance(index_data, list):
                    live_ids = {
                        path.stem
                        for path in session_dir.glob("*.json")
                        if not path.name.startswith("_")
                    }
                    with config.LOCK:
                        in_memory_ids = set(models.SESSIONS)

                    survivors = []
                    for entry in index_data:
                        sid = entry.get("session_id")
                        if not sid or sid in live_ids or sid in in_memory_ids:
                            survivors.append(entry)
                            continue
                        if sid in removed_sidecar_ids:
                            continue
                        pruned_index_rows += 1

                    if (
                        removed_sidecar_ids or pruned_index_rows
                    ) and len(survivors) < len(index_data):
                        tmp = index_file.with_suffix(
                            f".tmp.{os.getpid()}.{threading.current_thread().ident}"
                        )
                        payload = json.dumps(survivors, ensure_ascii=False, indent=2)
                        try:
                            with open(tmp, "w", encoding="utf-8") as handle:
                                handle.write(payload)
                                handle.flush()
                                os.fsync(handle.fileno())
                            models._safe_replace(tmp, index_file)
                            phase2_rewrote_index = True
                        except Exception:
                            try:
                                tmp.unlink(missing_ok=True)
                            except Exception:
                                pass
                            raise
        except Exception:
            logger.debug(
                "Failed to clean up index-only session entries",
                exc_info=True,
            )

    # A corrupt index could not be rewritten. Drop it only when Phase 1 made
    # it stale so the next sidebar read rebuilds it from surviving sidecars.
    if removed_sidecar_ids and not phase2_rewrote_index and index_file.exists():
        index_file.unlink(missing_ok=True)

    return SessionCleanupResult(
        removed_sidecars=len(removed_sidecar_ids),
        pruned_index_rows=pruned_index_rows,
        skipped_active=skipped_active,
    )


def delete_session_state(sid: str, *, messaging: bool) -> SessionDeletionResult:
    """Delete one session's owned state under its mutation lock.

    Authorization and source policy stay with the caller. This operation owns
    the deletion protocol itself: runtime projections, sidecars, recovery
    artifacts, journals, the sidebar index, and (for non-messaging sessions)
    the Hermes state database.
    """
    from api import config, models

    sid = str(sid or "")
    if not models.is_safe_session_id(sid):
        raise ValueError("Invalid session_id")

    owner_lock = config._get_session_agent_lock(sid)
    sidecar_deleted = False
    state_db_cleanup_failed = False
    try:
        with owner_lock:
            try:
                current = models.get_session(sid, metadata_only=True)
            except (KeyError, OSError):
                current = None
            blocking_stream = config.blocking_runtime_stream(
                sid,
                active_stream_id=getattr(current, "active_stream_id", None),
                pending_user_message=getattr(current, "pending_user_message", None),
                pending_started_at=getattr(current, "pending_started_at", None),
            )
            if blocking_stream:
                raise SessionActiveError(sid, blocking_stream)

            with config.LOCK:
                models.SESSIONS.pop(sid, None)

            # Eviction may flush lifecycle memory. Complete that work before
            # removing persisted artifacts while the same owner lock is held.
            config._evict_session_agent(sid)

            session_dir = models.SESSION_DIR.resolve()
            sidecar = (session_dir / f"{sid}.json").resolve()
            try:
                sidecar.relative_to(session_dir)
            except ValueError as exc:
                raise ValueError("Invalid session_id") from exc

            try:
                sidecar.unlink(missing_ok=True)
            except Exception:
                logger.debug("Failed to unlink session file %s", sidecar, exc_info=True)
            sidecar_deleted = not sidecar.exists()

            backup = sidecar.with_suffix(".json.bak")
            try:
                backup.unlink(missing_ok=True)
            except Exception:
                logger.debug(
                    "Failed to unlink session backup file %s",
                    backup,
                    exc_info=True,
                )

            try:
                models.prune_session_from_index(sid)
            except Exception:
                logger.debug(
                    "Failed to prune deleted session from index: %s",
                    sid,
                    exc_info=True,
                )

            if sidecar_deleted and not messaging:
                try:
                    models._record_webui_deleted_session_tombstone(sid)
                except Exception:
                    logger.debug(
                        "Failed to tombstone deleted WebUI session %s",
                        sid,
                        exc_info=True,
                    )

            try:
                from api.upload import _session_attachment_dir

                shutil.rmtree(_session_attachment_dir(sid), ignore_errors=True)
            except Exception:
                logger.debug(
                    "Failed to clean attachment dir for deleted session %s",
                    sid,
                    exc_info=True,
                )

            try:
                from api.turn_journal import delete_turn_journal

                delete_turn_journal(sid)
            except Exception:
                logger.debug(
                    "Failed to delete turn journal for deleted session %s",
                    sid,
                    exc_info=True,
                )

            try:
                from api.run_journal import delete_run_journal

                delete_run_journal(sid)
            except Exception:
                logger.debug(
                    "Failed to delete run journal for deleted session %s",
                    sid,
                    exc_info=True,
                )

            try:
                from api.background_process import forget_bg_task_completion_dedup

                forget_bg_task_completion_dedup(sid)
            except Exception:
                logger.debug(
                    "Failed to prune bg-task dedup entry for deleted session %s",
                    sid,
                    exc_info=True,
                )

            try:
                from api.terminal import close_terminal

                close_terminal(sid)
            except Exception:
                logger.debug(
                    "Failed to close workspace terminal for deleted session %s",
                    sid,
                    exc_info=True,
                )

            if not messaging:
                try:
                    state_db_cleanup_failed = not models.delete_cli_session(sid)
                except Exception:
                    state_db_cleanup_failed = True
                    logger.warning(
                        "Failed to delete CLI session %s",
                        sid,
                        exc_info=True,
                    )
    finally:
        # Do not pop the weak registry entry: another waiter may already hold
        # this same lock reference. It self-prunes after all references end.
        del owner_lock

    return SessionDeletionResult(
        sidecar_deleted=sidecar_deleted,
        state_db_cleanup_failed=state_db_cleanup_failed,
    )
