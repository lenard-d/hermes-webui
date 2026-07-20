"""Behavioral contract for safe WebUI session mutations."""

from __future__ import annotations

import copy

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


class _FailingSaveSession(_FakeSession):
    def __init__(self, *args, fail_on_call: int, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_on_call = fail_on_call
        self.save_calls = 0
        self.durable_titles = []

    def save(self, **kwargs):
        assert self.lock is not None and self.lock.held is True
        self.save_calls += 1
        if self.save_calls == self.fail_on_call:
            raise OSError(f"save {self.save_calls} failed")
        self.saved.append(kwargs)
        self.durable_titles.append(self.title)


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
    original_messages = [
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "Before"}],
            "metadata": {"labels": ["keep"]},
        }
    ]
    full = _FakeSession(
        "s1",
        metadata_only=False,
        messages=copy.deepcopy(original_messages),
    )
    full.lock = lock
    repository = SessionRepository(
        load=lambda _sid: full,
        load_full=lambda _sid: full,
        lock_for=lambda _sid: lock,
        cache_full=lambda _sid, _session: None,
    )

    mutation_error = RuntimeError("stop")
    with pytest.raises(RuntimeError, match="stop") as exc_info:
        with repository.edit("s1") as session:
            session.title = "Not persisted"
            session.messages[0]["content"][0]["text"] = "Mutated"
            session.messages[0]["metadata"]["labels"].append("discard")
            session.messages.append({"role": "user", "content": "discard"})
            raise mutation_error

    assert exc_info.value is mutation_error
    assert full.title == "Before"
    assert full.messages == original_messages
    assert full.saved == []
    assert full.lock is lock
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


def test_stream_writeback_reconciles_a_late_signal_after_the_first_save():
    from api.session_repository import SessionRepository

    lock = _RecordingLock()
    current = _FakeSession("s1", metadata_only=False, messages=[])
    current.active_stream_id = "stream-1"
    current.lock = lock
    reconciled = []
    repository = SessionRepository(
        load=lambda _sid: current,
        load_full=lambda _sid: current,
        lock_for=lambda _sid: lock,
        cache_full=lambda _sid, _session: None,
    )

    def mutate(session):
        assert lock.held is True
        session.title = "Completed"
        session.active_stream_id = None
        return "success"

    def reconcile_after_save(session):
        assert lock.held is True
        assert session.saved == [{"skip_index": True}]
        session.title = "Cancelled after save"
        reconciled.append(session.title)
        return True

    result = repository.commit_stream_writeback(
        "s1",
        expected_stream_id="stream-1",
        mutate=mutate,
        reconcile_after_save=reconcile_after_save,
    )

    assert result is not None
    assert result.session is current
    assert result.value == "success"
    assert result.reconciled_after_save is True
    assert result.reconciliation_error is None
    assert current.title == "Cancelled after save"
    assert current.saved == [{"skip_index": True}, {"skip_index": True}]
    assert reconciled == ["Cancelled after save"]
    assert lock.held is False


def test_stream_writeback_rejects_a_stale_generation_without_mutating_or_saving():
    from api.session_repository import SessionRepository

    lock = _RecordingLock()
    current = _FakeSession("s1", metadata_only=False, messages=[])
    current.active_stream_id = "newer-stream"
    current.lock = lock
    mutated = []
    repository = SessionRepository(
        load=lambda _sid: current,
        load_full=lambda _sid: current,
        lock_for=lambda _sid: lock,
        cache_full=lambda _sid, _session: None,
    )

    result = repository.commit_stream_writeback(
        "s1",
        expected_stream_id="older-stream",
        mutate=lambda _session: mutated.append(True),
    )

    assert result is None
    assert mutated == []
    assert current.saved == []
    assert lock.held is False


def test_stream_writeback_does_not_resurrect_a_missing_session_from_caller_seed():
    from api.session_repository import SessionRepository

    lock = _RecordingLock()
    seed = _FakeSession("deleted", metadata_only=False, messages=[])
    seed.active_stream_id = "stream-1"
    seed.lock = lock
    cached = []
    mutated = []
    indexed = []
    repository = SessionRepository(
        load=lambda sid: (_ for _ in ()).throw(KeyError(sid)),
        load_full=lambda sid: (_ for _ in ()).throw(KeyError(sid)),
        lock_for=lambda _sid: lock,
        cache_full=lambda sid, session: cached.append((sid, session)),
        write_index=lambda sessions: indexed.extend(sessions),
    )

    result = repository.commit_stream_writeback(
        "deleted",
        expected_stream_id="stream-1",
        session=seed,
        mutate=lambda _session: mutated.append(True),
    )

    assert result is None
    assert cached == []
    assert mutated == []
    assert seed.saved == []
    assert indexed == []
    assert lock.held is False


def test_stream_writeback_discards_reconcile_mutation_when_no_resave_is_requested():
    from api.session_repository import SessionRepository

    lock = _RecordingLock()
    current = _FakeSession("s1", metadata_only=False, messages=[])
    current.active_stream_id = "stream-1"
    current.lock = lock
    cached = []
    indexed_titles = []
    repository = SessionRepository(
        load=lambda _sid: current,
        load_full=lambda _sid: current,
        lock_for=lambda _sid: lock,
        cache_full=lambda _sid, session: cached.append(session),
        write_index=lambda sessions: indexed_titles.extend(
            session.title for session in sessions
        ),
    )

    def mutate(session):
        session.title = "Completed"
        session.active_stream_id = None
        return "success"

    def reconcile_without_resave(session):
        session.title = "Unpersisted reconciliation"
        return False

    result = repository.commit_stream_writeback(
        "s1",
        expected_stream_id="stream-1",
        mutate=mutate,
        reconcile_after_save=reconcile_without_resave,
    )

    assert result is not None
    assert result.session is current
    assert result.session.title == "Completed"
    assert result.value == "success"
    assert result.reconciled_after_save is False
    assert result.reconciliation_error is None
    assert cached == [current]
    assert cached[0].title == "Completed"
    assert indexed_titles == ["Completed"]
    assert current.saved == [{"skip_index": True}]
    assert lock.held is False


def test_stream_writeback_restores_memory_when_the_first_durable_save_fails():
    from api.session_repository import SessionRepository

    lock = _RecordingLock()
    current = _FailingSaveSession(
        "s1",
        metadata_only=False,
        messages=[],
        fail_on_call=1,
    )
    current.active_stream_id = "stream-1"
    current.lock = lock
    repository = SessionRepository(
        load=lambda _sid: current,
        load_full=lambda _sid: current,
        lock_for=lambda _sid: lock,
        cache_full=lambda _sid, _session: None,
    )

    with pytest.raises(OSError, match="save 1 failed"):
        repository.commit_stream_writeback(
            "s1",
            expected_stream_id="stream-1",
            mutate=lambda session: (
                setattr(session, "title", "Completed"),
                setattr(session, "active_stream_id", None),
            ),
        )

    assert current.title == "Before"
    assert current.active_stream_id == "stream-1"
    assert current.durable_titles == []
    assert lock.held is False


def test_stream_writeback_keeps_the_first_commit_when_reconciliation_save_fails():
    from api.session_repository import SessionRepository

    lock = _RecordingLock()
    current = _FailingSaveSession(
        "s1",
        metadata_only=False,
        messages=[],
        fail_on_call=2,
    )
    current.active_stream_id = "stream-1"
    current.lock = lock
    repository = SessionRepository(
        load=lambda _sid: current,
        load_full=lambda _sid: current,
        lock_for=lambda _sid: lock,
        cache_full=lambda _sid, _session: None,
    )

    result = repository.commit_stream_writeback(
        "s1",
        expected_stream_id="stream-1",
        mutate=lambda session: (
            setattr(session, "title", "Completed"),
            setattr(session, "active_stream_id", None),
        ),
        reconcile_after_save=lambda session: (
            setattr(session, "title", "Cancelled after save") or True
        ),
    )

    assert result is not None
    assert result.reconciled_after_save is False
    assert isinstance(result.reconciliation_error, OSError)
    assert current.title == "Completed"
    assert current.active_stream_id is None
    assert current.durable_titles == ["Completed"]
    assert lock.held is False


def test_write_owner_rejects_deleted_seed_before_cache_publication():
    from api.session_repository import SessionRepository, SessionWriteRejected

    lock = _RecordingLock()
    seed = _FakeSession("deleted", metadata_only=False, messages=[])
    seed.lock = lock
    cached = []
    repository = SessionRepository(
        load=lambda sid: (_ for _ in ()).throw(KeyError(sid)),
        load_full=lambda sid: (_ for _ in ()).throw(KeyError(sid)),
        lock_for=lambda _sid: lock,
        cache_full=lambda sid, session: cached.append((sid, session)),
    )

    with pytest.raises(SessionWriteRejected):
        with repository.write_owner(
            "deleted",
            session=seed,
            reject_when=lambda: True,
        ):
            pass

    assert cached == []
    assert seed.saved == []
    assert lock.held is False


def test_admission_compensation_generation_mismatch_is_noop():
    from api.session_repository import SessionRepository

    lock = _RecordingLock()
    current = _FakeSession("s1", metadata_only=False, messages=[])
    current.active_stream_id = "newer"
    current.lock = lock
    restored = []
    indexed = []
    repository = SessionRepository(
        load=lambda _sid: current,
        load_full=lambda _sid: current,
        lock_for=lambda _sid: lock,
        cache_full=lambda _sid, _session: None,
        write_index=lambda sessions: indexed.extend(sessions),
    )

    committed = repository.compensate_failed_admission(
        "s1",
        expected_stream_id="older",
        restore=lambda _session: restored.append(True),
    )

    assert committed is False
    assert restored == []
    assert current.saved == []
    assert indexed == []


def test_admission_compensation_does_not_resurrect_missing_baseline_sidecar():
    from api.session_repository import SessionRepository

    lock = _RecordingLock()
    current = _FakeSession("s1", metadata_only=False, messages=[])
    current.active_stream_id = "stream-1"
    current.lock = lock
    restored = []
    repository = SessionRepository(
        load=lambda _sid: current,
        load_full=lambda _sid: current,
        lock_for=lambda _sid: lock,
        cache_full=lambda _sid, _session: None,
        sidecar_exists=lambda _sid: False,
    )

    committed = repository.compensate_failed_admission(
        "s1",
        expected_stream_id="stream-1",
        restore=lambda _session: restored.append(True),
        baseline_persisted=True,
    )

    assert committed is False
    assert restored == []
    assert current.saved == []


def test_first_turn_compensation_restores_memory_without_creating_sidecar():
    from api.session_repository import SessionRepository

    lock = _RecordingLock()
    current = _FakeSession("new", metadata_only=False, messages=[])
    current.active_stream_id = "stream-1"
    current.lock = lock
    pruned = []
    callbacks = []
    repository = SessionRepository(
        load=lambda _sid: current,
        load_full=lambda _sid: pytest.fail("missing sidecar must not be loaded"),
        lock_for=lambda _sid: lock,
        cache_full=lambda _sid, _session: None,
        sidecar_exists=lambda _sid: False,
        prune_index=pruned.append,
    )

    committed = repository.compensate_failed_admission(
        "new",
        expected_stream_id="stream-1",
        restore=lambda session: setattr(session, "active_stream_id", None),
        on_committed=lambda: callbacks.append(True),
        baseline_persisted=False,
    )

    assert committed is True
    assert current.active_stream_id is None
    assert current.saved == []
    assert pruned == ["new"]
    assert callbacks == [True]


def test_first_turn_compensation_discards_matching_provisional_sidecar():
    from api.session_repository import SessionRepository

    lock = _RecordingLock()
    current = _FakeSession("new", metadata_only=False, messages=[])
    current.active_stream_id = "stream-1"
    current.lock = lock
    discarded = []
    pruned = []
    repository = SessionRepository(
        load=lambda _sid: current,
        load_full=lambda _sid: current,
        lock_for=lambda _sid: lock,
        cache_full=lambda _sid, _session: None,
        sidecar_exists=lambda _sid: True,
        discard_sidecar=discarded.append,
        prune_index=pruned.append,
    )

    committed = repository.compensate_failed_admission(
        "new",
        expected_stream_id="stream-1",
        restore=lambda session: setattr(session, "active_stream_id", None),
        baseline_persisted=False,
    )

    assert committed is True
    assert current.active_stream_id is None
    assert current.saved == []
    assert discarded == ["new"]
    assert pruned == ["new"]


def test_first_turn_discard_failure_restores_provisional_memory_state():
    from api.session_repository import SessionRepository

    lock = _RecordingLock()
    current = _FakeSession("new", metadata_only=False, messages=[])
    current.active_stream_id = "stream-1"
    current.lock = lock
    repository = SessionRepository(
        load=lambda _sid: current,
        load_full=lambda _sid: current,
        lock_for=lambda _sid: lock,
        cache_full=lambda _sid, _session: None,
        sidecar_exists=lambda _sid: True,
        discard_sidecar=lambda _sid: (_ for _ in ()).throw(
            OSError("discard unavailable")
        ),
    )

    def restore(session):
        session.title = "Restored baseline"
        session.active_stream_id = None

    with pytest.raises(OSError, match="discard unavailable"):
        repository.compensate_failed_admission(
            "new",
            expected_stream_id="stream-1",
            restore=restore,
            baseline_persisted=False,
        )

    assert current.title == "Before"
    assert current.active_stream_id == "stream-1"
    assert current.saved == []


def test_admission_compensation_restores_memory_when_restore_raises():
    from api.session_repository import SessionRepository

    lock = _RecordingLock()
    current = _FakeSession("s1", metadata_only=False, messages=[])
    current.active_stream_id = "stream-1"
    current.lock = lock
    repository = SessionRepository(
        load=lambda _sid: current,
        load_full=lambda _sid: current,
        lock_for=lambda _sid: lock,
        cache_full=lambda _sid, _session: None,
    )

    def fail_after_mutation(session):
        session.title = "Partially restored"
        raise RuntimeError("restore failed")

    with pytest.raises(RuntimeError, match="restore failed"):
        repository.compensate_failed_admission(
            "s1",
            expected_stream_id="stream-1",
            restore=fail_after_mutation,
        )

    assert current.title == "Before"
    assert current.active_stream_id == "stream-1"
    assert current.saved == []
    assert lock.held is False


def test_admission_compensation_commits_before_marker_callback_and_tolerates_index_failure():
    from api.session_repository import SessionRepository

    lock = _RecordingLock()
    current = _FakeSession("s1", metadata_only=False, messages=[])
    current.active_stream_id = "stream-1"
    current.lock = lock
    order = []

    def fail_index(_sessions):
        order.append("index")
        raise OSError("index unavailable")

    repository = SessionRepository(
        load=lambda _sid: current,
        load_full=lambda _sid: current,
        lock_for=lambda _sid: lock,
        cache_full=lambda _sid, _session: None,
        write_index=fail_index,
    )

    def restore(session):
        order.append("restore")
        session.active_stream_id = None

    def markers():
        assert lock.held is True
        assert current.saved == [
            {
                "touch_updated_at": False,
                "skip_index": True,
                "_admission_compensation": True,
            }
        ]
        order.append("markers")

    committed = repository.compensate_failed_admission(
        "s1",
        expected_stream_id="stream-1",
        restore=restore,
        on_committed=markers,
    )

    assert committed is True
    assert order == ["restore", "index", "markers"]
    assert current.active_stream_id is None
    assert lock.held is False
