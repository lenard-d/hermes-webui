"""Authoritative loading and persistence for mutable WebUI sessions.

Route handlers should describe a mutation, not reproduce the persistence
protocol.  ``SessionRepository.edit`` owns that protocol: acquire the
per-session lock, upgrade metadata-only projections, validate cache ownership,
and save only after a successful mutation.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, Iterator


class SessionBusyError(RuntimeError):
    """Raised when a bounded session edit cannot acquire its owner lock."""


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
        current = session if session is not None else self._load(sid)
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
