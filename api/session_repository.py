"""Authoritative loading and persistence for mutable WebUI sessions.

Route handlers should describe a mutation, not reproduce the persistence
protocol.  ``SessionRepository.edit`` owns that protocol: acquire the
per-session lock, upgrade metadata-only projections, validate cache ownership,
and save only after a successful mutation.
"""

from __future__ import annotations

import logging
import shutil
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


@dataclass(frozen=True)
class SessionDeletionResult:
    """Observable result of deleting all WebUI-owned session state."""

    sidecar_deleted: bool
    state_db_cleanup_failed: bool


class SessionRepository:
    """Coordinate safe edits of one persisted session at a time."""

    def __init__(
        self,
        *,
        load: Callable[[str], object],
        load_full: Callable[[str], object],
        lock_for: Callable[[str], object],
        cache_full: Callable[[str, object], None],
    ) -> None:
        self._load = load
        self._load_full = load_full
        self._lock_for = lock_for
        self._cache_full = cache_full

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

    def get_full(self, sid: str, *, session: object | None = None) -> object:
        """Return an identity-checked full session under its owner lock."""
        sid = str(sid or "")
        if not sid:
            raise KeyError(sid)
        with self._hold_lock(sid, None):
            return self._load_for_edit(sid, session)

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
            yield current

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

    return SessionRepository(
        load=models.get_session,
        load_full=models.Session.load,
        lock_for=config._get_session_agent_lock,
        cache_full=models.cache_full_session,
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
    deletion_started = False
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

            deletion_started = True
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
        # Only remove the lock we actually held. An identity check prevents a
        # concurrent replacement from losing its own owner entry.
        if deletion_started:
            with config.SESSION_AGENT_LOCKS_LOCK:
                if config.SESSION_AGENT_LOCKS.get(sid) is owner_lock:
                    config.SESSION_AGENT_LOCKS.pop(sid, None)

    return SessionDeletionResult(
        sidecar_deleted=sidecar_deleted,
        state_db_cleanup_failed=state_db_cleanup_failed,
    )
