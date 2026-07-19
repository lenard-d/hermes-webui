"""Behavioral contract for safe WebUI session mutations."""

from __future__ import annotations

import pytest


class _RecordingLock:
    def __init__(self):
        self.held = False

    def __enter__(self):
        assert self.held is False
        self.held = True
        return self

    def __exit__(self, exc_type, exc, tb):
        self.held = False
        return False


class _BusyLock:
    def __init__(self):
        self.timeouts = []

    def acquire(self, *, timeout):
        self.timeouts.append(timeout)
        return False

    def release(self):
        raise AssertionError("a lock that was not acquired must not be released")


class _FakeSession:
    def __init__(self, sid: str, *, metadata_only: bool, messages: list[dict]):
        self.session_id = sid
        self._loaded_metadata_only = metadata_only
        self.messages = messages
        self.title = "Before"
        self.saved = []
        self.lock = None

    def save(self, **kwargs):
        assert self.lock is not None and self.lock.held is True
        self.saved.append(kwargs)


def test_edit_upgrades_metadata_stub_and_saves_under_the_session_lock():
    from api.session_repository import SessionRepository

    lock = _RecordingLock()
    stub = _FakeSession("s1", metadata_only=True, messages=[])
    full = _FakeSession("s1", metadata_only=False, messages=[{"role": "user", "content": "keep"}])
    full.lock = lock
    cached = []
    repository = SessionRepository(
        load=lambda _sid: stub,
        load_full=lambda _sid: full,
        lock_for=lambda _sid: lock,
        cache_full=lambda sid, session: cached.append((sid, session)),
    )

    with repository.edit("s1", session=stub, touch_updated_at=False) as session:
        assert lock.held is True
        session.title = "After"

    assert full.title == "After"
    assert full.messages == [{"role": "user", "content": "keep"}]
    assert full.saved == [{"touch_updated_at": False}]
    assert cached == [("s1", full)]


def test_edit_does_not_save_a_failed_mutation():
    from api.session_repository import SessionRepository

    lock = _RecordingLock()
    full = _FakeSession("s1", metadata_only=False, messages=[])
    full.lock = lock
    repository = SessionRepository(
        load=lambda _sid: full,
        load_full=lambda _sid: full,
        lock_for=lambda _sid: lock,
        cache_full=lambda _sid, _session: None,
    )

    with pytest.raises(RuntimeError, match="stop"):
        with repository.edit("s1") as session:
            session.title = "Not persisted"
            raise RuntimeError("stop")

    assert full.saved == []
    assert lock.held is False


def test_edit_reports_a_busy_session_when_the_bounded_lock_cannot_be_acquired():
    from api.session_repository import SessionBusyError, SessionRepository

    lock = _BusyLock()
    full = _FakeSession("s1", metadata_only=False, messages=[])
    repository = SessionRepository(
        load=lambda _sid: full,
        load_full=lambda _sid: full,
        lock_for=lambda _sid: lock,
        cache_full=lambda _sid, _session: None,
    )

    with pytest.raises(SessionBusyError, match="s1"):
        with repository.edit("s1", lock_timeout=0.25):
            pass

    assert lock.timeouts == [0.25]
    assert full.saved == []


def test_edit_can_skip_persistence_for_an_unchanged_mutation():
    from api.session_repository import SessionRepository

    lock = _RecordingLock()
    full = _FakeSession("s1", metadata_only=False, messages=[])
    full.lock = lock
    repository = SessionRepository(
        load=lambda _sid: full,
        load_full=lambda _sid: full,
        lock_for=lambda _sid: lock,
        cache_full=lambda _sid, _session: None,
    )

    with repository.edit("s1", save_when=lambda _session: False):
        pass

    assert full.saved == []


def test_edit_reloads_current_session_under_lock_instead_of_saving_stale_argument():
    from api.session_repository import SessionRepository

    lock = _RecordingLock()
    stale = _FakeSession(
        "s1",
        metadata_only=False,
        messages=[{"role": "user", "content": "old"}],
    )
    current = _FakeSession(
        "s1",
        metadata_only=False,
        messages=[
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "newer"},
        ],
    )
    stale.lock = lock
    current.lock = lock
    repository = SessionRepository(
        load=lambda _sid: current,
        load_full=lambda _sid: current,
        lock_for=lambda _sid: lock,
        cache_full=lambda _sid, _session: None,
    )

    with repository.edit("s1", session=stale) as session:
        session.title = "Renamed"

    assert current.title == "Renamed"
    assert current.messages[-1]["content"] == "newer"
    assert current.saved == [{}]
    assert stale.saved == []


def test_edit_uses_explicit_seed_only_when_repository_has_no_session():
    from api.session_repository import SessionRepository

    lock = _RecordingLock()
    seed = _FakeSession("new", metadata_only=False, messages=[])
    seed.lock = lock
    repository = SessionRepository(
        load=lambda sid: (_ for _ in ()).throw(KeyError(sid)),
        load_full=lambda sid: (_ for _ in ()).throw(KeyError(sid)),
        lock_for=lambda _sid: lock,
        cache_full=lambda _sid, _session: None,
    )

    with repository.edit("new", session=seed) as session:
        session.title = "Created"

    assert seed.saved == [{}]
