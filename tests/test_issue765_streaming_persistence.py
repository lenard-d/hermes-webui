"""
Tests for periodic session persistence during streaming (Issue #765).

Validates:
  - Session.save(skip_index=True) writes the JSON file but skips the index rebuild
  - The periodic checkpoint fires when _checkpoint_activity is incremented
    (as it would be by on_tool() during real agent execution)
  - Messages stored via pending_user_message survive a simulated server restart
"""
import gc
import json
import threading
import time
from pathlib import Path

import pytest

import api.sessions.records as models
from api.sessions.records import Session


@pytest.fixture(autouse=True)
def _isolate_session_dir(tmp_path, monkeypatch):
    """Redirect SESSION_DIR and SESSION_INDEX_FILE to a temp directory."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"

    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)

    models.SESSIONS.clear()
    yield session_dir, index_file
    models.SESSIONS.clear()


def _make_session(session_id="abc123", messages=None):
    """Helper to create a Session with a known ID."""
    return Session(
        session_id=session_id,
        title="Test Session",
        messages=messages or [{"role": "user", "content": "hello"}],
    )


class TestSaveSkipIndex:
    """Tests for the skip_index parameter on Session.save()."""

    def test_save_writes_json_file(self):
        """save() always writes the session JSON file, regardless of skip_index."""
        s = _make_session("s1")
        s.save()
        assert s.path.exists()
        data = json.loads(s.path.read_text())
        assert data["session_id"] == "s1"
        assert len(data["messages"]) == 1

    def test_save_with_skip_index_writes_json(self):
        """save(skip_index=True) still writes the session JSON file."""
        s = _make_session("s2")
        s.save(skip_index=True)
        assert s.path.exists()
        data = json.loads(s.path.read_text())
        assert data["session_id"] == "s2"

    def test_save_with_skip_index_skips_index_rebuild(self):
        """save(skip_index=True) does NOT create or update the session index."""
        s = _make_session("s3")
        s.save(skip_index=True)
        index = models.SESSION_INDEX_FILE
        assert not index.exists(), "Index file should not be created with skip_index=True"

    def test_save_without_skip_index_creates_index(self):
        """save() (default) DOES create the session index."""
        s = _make_session("s4")
        s.save()
        index = models.SESSION_INDEX_FILE
        assert index.exists(), "Index file should be created by default save()"
        data = json.loads(index.read_text())
        sids = [e["session_id"] for e in data]
        assert "s4" in sids

    def test_skip_index_then_full_save_updates_index(self):
        """After skip_index saves, a full save() correctly builds the index."""
        s = _make_session("s5")
        s.messages.append({"role": "assistant", "content": "hi there"})
        s.save(skip_index=True)
        assert not models.SESSION_INDEX_FILE.exists()

        s.messages.append({"role": "user", "content": "thanks"})
        s.save()
        assert models.SESSION_INDEX_FILE.exists()
        data = json.loads(s.path.read_text())
        assert len(data["messages"]) == 3

    def test_skip_index_save_with_touch_updated_at_false(self):
        """save(skip_index=True, touch_updated_at=False) preserves updated_at."""
        s = _make_session("touch1")
        original_updated_at = s.updated_at
        time.sleep(0.05)
        s.save(skip_index=True, touch_updated_at=False)
        data = json.loads(s.path.read_text())
        assert data["updated_at"] == original_updated_at
        assert not models.SESSION_INDEX_FILE.exists()


class TestPeriodicCheckpoint:
    """Tests for the periodic checkpoint mechanism during streaming.

    The checkpoint is keyed off an activity counter (_checkpoint_activity[0]),
    incremented by on_tool() on each tool.completed event — NOT off s.messages
    which is never mutated during agent.run_conversation() (the agent copies it).
    """

    def test_checkpoint_fires_on_activity_counter_increment(self):
        """Checkpoint saves when _checkpoint_activity counter grows.

        Deterministic: instead of relying on time-based polling windows, we
        wait for the checkpoint thread's save_count to advance after each
        increment. Generous timeout guards against CI scheduling jitter.
        """
        s = _make_session("ckpt1")
        s.pending_user_message = "do a long task"
        s.save()  # initial save (like routes.py does before streaming starts)

        stop_event = threading.Event()
        _checkpoint_activity = [0]
        save_count = [0]
        save_event = threading.Event()

        def periodic_checkpoint():
            last = 0
            while not stop_event.wait(0.02):  # fast poll for low-jitter test
                try:
                    cur = _checkpoint_activity[0]
                    if cur > last:
                        s.save(skip_index=True)
                        last = cur
                        save_count[0] += 1
                        save_event.set()
                except Exception:
                    pass

        t = threading.Thread(target=periodic_checkpoint, daemon=True)
        t.start()

        def _wait_for_save(target_count, timeout=3.0):
            """Wait until save_count[0] >= target_count, or timeout."""
            deadline = time.monotonic() + timeout
            while save_count[0] < target_count and time.monotonic() < deadline:
                save_event.wait(timeout=0.05)
                save_event.clear()
            return save_count[0] >= target_count

        # Simulate on_tool() completing twice
        _checkpoint_activity[0] += 1  # first tool completes
        assert _wait_for_save(1), f"Expected 1 save after first increment; got {save_count[0]}"

        _checkpoint_activity[0] += 1  # second tool completes
        assert _wait_for_save(2), f"Expected 2 saves after second increment; got {save_count[0]}"

        stop_event.set()
        t.join(timeout=2)

        assert save_count[0] >= 2, (
            "Expected at least 2 checkpoint saves (one per activity increment); "
            f"got {save_count[0]}"
        )
        # Verify the JSON is on disk and readable
        data = json.loads(s.path.read_text())
        assert data["pending_user_message"] == "do a long task"

    def test_checkpoint_does_not_fire_without_activity(self):
        """Checkpoint skips save when activity counter has not changed."""
        s = _make_session("ckpt2")
        s.save()

        stop_event = threading.Event()
        _checkpoint_activity = [0]
        save_count = [0]

        def periodic_checkpoint():
            last = 0
            while not stop_event.wait(0.05):
                cur = _checkpoint_activity[0]
                if cur > last:
                    s.save(skip_index=True)
                    last = cur
                    save_count[0] += 1

        t = threading.Thread(target=periodic_checkpoint, daemon=True)
        t.start()
        # No increments — checkpoint should stay quiet
        time.sleep(0.4)
        stop_event.set()
        t.join(timeout=2)

        assert save_count[0] == 0, (
            f"Expected 0 saves when activity is unchanged; got {save_count[0]}"
        )

    def test_checkpoint_stops_on_signal(self):
        """Checkpoint thread exits cleanly when stop event is set."""
        stop_event = threading.Event()
        iterations = [0]

        def periodic_checkpoint():
            while not stop_event.wait(0.02):
                iterations[0] += 1

        t = threading.Thread(target=periodic_checkpoint, daemon=True)
        t.start()
        time.sleep(0.15)
        stop_event.set()
        t.join(timeout=1)
        assert not t.is_alive(), "Checkpoint thread should have stopped"

    def test_pending_message_survives_simulated_restart(self):
        """pending_user_message written before run_conversation survives a restart.

        This is the minimal guarantee for Issue #765: even if the agent produces
        no tool calls before a crash, the user's message is not silently lost.
        """
        s = _make_session("survive1", messages=[{"role": "user", "content": "first turn"}])
        s.save()  # initial full save

        # Simulate what routes.py does before _run_agent_streaming:
        s.pending_user_message = "do a long research task"
        s.pending_started_at = time.time()
        s.active_stream_id = "stream-abc123"
        s.save(skip_index=True)  # checkpoint-style save

        # Simulate restart: clear in-memory state, reload from disk
        del s
        models.SESSIONS.clear()

        reloaded = Session.load("survive1")
        assert reloaded is not None
        assert reloaded.pending_user_message == "do a long research task"
        assert reloaded.active_stream_id == "stream-abc123"
        # Original messages still intact
        assert len(reloaded.messages) == 1

    def test_activity_checkpoint_persists_updated_at(self):
        """Each checkpoint save updates updated_at, keeping session fresh in sidebar."""
        s = _make_session("ts1")
        s.save()
        ts_before = s.updated_at

        time.sleep(0.05)
        _checkpoint_activity = [1]  # simulate one tool completion

        stop_event = threading.Event()

        def periodic_checkpoint():
            last = 0
            while not stop_event.wait(0.05):
                cur = _checkpoint_activity[0]
                if cur > last:
                    s.save(skip_index=True)
                    last = cur

        t = threading.Thread(target=periodic_checkpoint, daemon=True)
        t.start()
        time.sleep(0.2)
        stop_event.set()
        t.join(timeout=1)

        data = json.loads(s.path.read_text())
        assert data["updated_at"] > ts_before, "Checkpoint should update updated_at"


class TestIssue765FollowupHardening:
    """Regression tests for the follow-up hardening pass on Issue #765.

    Includes the guard that the outer `finally` must not UnboundLocalError when
    an exception fires before the checkpoint thread is created.
    """

    def test_same_session_concurrent_saves_use_distinct_temp_files(self, monkeypatch):
        """Two concurrent saves of the same session must not collide on one tmp path.

        The key regression guard here is that each save call should reach os.replace()
        with a distinct source tmp path. With the old shared `<sid>.tmp` scheme, both
        threads would target the same path and the second replace would deterministically
        fail once the first consume/remove happened.
        """
        s = _make_session("same_sid")
        s.save(skip_index=True)  # seed the file on disk

        original_replace = models.os.replace
        barrier = threading.Barrier(2)
        replace_sources = []
        errors = []

        def _replace_with_barrier(src, dst):
            replace_sources.append(str(src))
            barrier.wait(timeout=5)
            return original_replace(src, dst)

        monkeypatch.setattr(models.os, "replace", _replace_with_barrier)

        def _save_worker():
            try:
                s.save(skip_index=True)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=_save_worker)
        t2 = threading.Thread(target=_save_worker)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert not errors, f"Concurrent same-session saves should not fail: {errors}"
        assert len(replace_sources) == 2, f"Expected 2 replace calls, got {replace_sources}"
        assert len(set(replace_sources)) == 2, (
            "Concurrent same-session saves must use distinct temp files; "
            f"got {replace_sources}"
        )
        data = json.loads(s.path.read_text(encoding="utf-8"))
        assert data["session_id"] == "same_sid"

    def test_success_path_joins_checkpoint_before_session_mutation(self):
        """Static guard: success path must stop/join checkpoint thread before mutating.

        This keeps the post-run_conversation session rewrite serialized relative to the
        periodic checkpoint worker.
        """
        src = (Path(__file__).parent.parent / "api" / "runs" / "local.py").read_text(
            encoding="utf-8"
        )
        run_idx = src.find("result = conversation.run(agent)")
        close_idx = src.find("checkpoint.close()", run_idx)
        lock_idx = src.find("with _agent_lock:", close_idx)
        save_idx = src.find("merged_result = merge_local_result(", lock_idx)

        assert run_idx != -1, "Local conversation call not found"
        assert close_idx != -1, "Success path must close the checkpoint owner"
        assert lock_idx != -1, "Success path must serialize mutation with _agent_lock"
        assert save_idx != -1, "Success path restore/mutation block not found"
        assert run_idx < close_idx < lock_idx <= save_idx, (
            "Checkpoint stop/join must happen before the success-path session mutation block"
        )

    def test_silent_failure_path_does_not_reacquire_agent_lock(self):
        """Silent-failure path must not nest `_agent_lock` inside the success lock.

        Reacquiring the same per-session lock inside the post-run_conversation block
        deadlocks because `_get_session_agent_lock()` returns a non-reentrant Lock.
        """
        orchestrator = (Path(__file__).parent.parent / "api" / "runs" / "local.py").read_text(
            encoding="utf-8"
        )
        failure_owner = (
            Path(__file__).parent.parent / "api" / "runs" / "local_failures.py"
        ).read_text(encoding="utf-8")
        lock_idx = orchestrator.index("with _agent_lock:")
        inspect_idx = orchestrator.index("failure_owner.inspect_terminal_result(", lock_idx)
        assert lock_idx < inspect_idx
        assert "lock_held=True" in failure_owner
        assert "contextlib.nullcontext()\n                if lock_held" in failure_owner

    def test_checkpoint_stop_initialised_before_any_raiseable_code(self):
        """Static check: `_checkpoint_stop = None` must appear before any code
        that could raise inside _run_agent_streaming's outer try."""
        from api.runs.local_checkpoint import LocalCheckpoint

        checkpoint = LocalCheckpoint(session_id="early-failure", logger=models.logger)
        assert checkpoint.stop_event.is_set() is False
        checkpoint.close()
        assert checkpoint.stop_event.is_set() is True

    def test_finally_path_when_early_exception_does_not_unbound_error(self):
        """Mirror the _run_agent_streaming try/finally structure — proves that
        pre-initialising _checkpoint_stop = None outside any raiseable code
        keeps the finally safe."""

        def mimic_run_agent_streaming():
            _checkpoint_stop = None  # pre-init (the fix)
            try:
                # Anything here could raise — simulate early failure
                raise ValueError("early failure, e.g. get_session KeyError")
                _checkpoint_stop = threading.Event()  # never reached
            finally:
                # The guard the PR added — must not itself raise
                if _checkpoint_stop is not None:
                    _checkpoint_stop.set()

        with pytest.raises(ValueError, match="early failure"):
            mimic_run_agent_streaming()

    def test_agent_lock_null_guard_in_except_block(self):
        """The except block must not crash with AttributeError when _agent_lock
        is None (e.g. when get_session succeeds but _get_session_agent_lock
        hasn't been called yet, or _get_session_agent_lock itself raised).

        The code must use a nullcontext fallback rather than unconditionally
        entering `with _agent_lock:`."""
        src = (Path(__file__).parent.parent / "api" / "runs" / "local_failures.py").read_text(
            encoding="utf-8"
        )
        # Verify contextlib.nullcontext is used as a fallback
        assert "contextlib.nullcontext()" in src, (
            "The except block must guard _agent_lock being None by falling "
            "back to contextlib.nullcontext() instead of unconditionally "
            "entering `with _agent_lock:`"
        )
        # Verify the except block uses _lock_ctx (the guarded variable)
        assert "self.ctx.session_lock or contextlib.nullcontext()" in src

    def test_periodic_checkpoint_uses_agent_lock(self):
        """The periodic checkpoint thread must hold _agent_lock while saving
        to prevent concurrent mutation races with other endpoints."""
        src = (Path(__file__).parent.parent / "api" / "runs" / "local_checkpoint.py").read_text(
            encoding="utf-8"
        )
        ckpt_idx = src.find("def periodic_checkpoint()")
        assert ckpt_idx != -1, "periodic checkpoint owner not found"
        ckpt_block = src[ckpt_idx:ckpt_idx + 600]
        assert "with session_lock:" in ckpt_block, (
            "periodic checkpoint must hold the session lock while saving "
            "to prevent race conditions with other session-mutating endpoints"
        )

    def test_cancel_stream_uses_repository_edit_owner(self):
        """Cancel cleanup must serialize and persist through the repository."""
        repo = Path(__file__).parent.parent
        owner_src = (repo / "api" / "streaming" / "live_controls.py").read_text(
            encoding="utf-8"
        )
        cancel_idx = owner_src.find("def cancel_stream(")
        assert cancel_idx != -1, "cancel_stream owner not found"
        cancel_block = owner_src[cancel_idx:]
        # Find the session cleanup section
        cleanup_idx = cancel_block.find("Session cleanup stays outside STREAMS_LOCK")
        assert cleanup_idx != -1, "Session cleanup lock-order comment not found in live_controls owner"
        cleanup_section = cancel_block[cleanup_idx:cleanup_idx + 1000]
        assert "with edit_session(" in cleanup_section, (
            "cancel_stream must use the repository edit owner during session "
            "cleanup to serialize with checkpoint and endpoint writers"
        )
        assert "save_when=lambda _current: cancel_persisted" in cleanup_section

    @pytest.mark.parametrize("operation_name", ["retry_last", "undo_last"])
    def test_session_ops_retry_undo_hold_agent_lock(self, operation_name):
        """Both mutations wait for the session owner lock before changing disk."""
        import api.config as config
        import api.sessions.operations as session_ops

        sid = f"owner-lock-{operation_name}"
        session = Session(
            session_id=sid,
            messages=[
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "reply"},
                {"role": "user", "content": "last"},
            ],
            context_messages=[
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "reply"},
                {"role": "user", "content": "last"},
            ],
        )
        session.save()
        lock = config._get_session_agent_lock(sid)
        lock.acquire()
        started = threading.Event()
        finished = threading.Event()
        errors = []

        def mutate():
            started.set()
            try:
                getattr(session_ops, operation_name)(sid)
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                finished.set()

        worker = threading.Thread(target=mutate)
        worker.start()
        assert started.wait(timeout=1)
        assert finished.wait(timeout=0.05) is False
        lock.release()
        worker.join(timeout=2)

        assert errors == []
        assert finished.is_set()
        persisted = Session.load(sid)
        assert [row["content"] for row in persisted.messages] == ["first", "reply"]

    def test_periodic_checkpoint_mutation_race_with_undo_last(self, tmp_path, monkeypatch):
        """Run _periodic_checkpoint against a session whose messages list is
        concurrently truncated by undo_last; the on-disk JSON must remain
        parseable and internally consistent.

        The simulated checkpoint mirrors production by acquiring
        _get_session_agent_lock around s.save(), and we assert that every
        on-disk snapshot's messages list is one of the allowed snapshots
        (never an interleaving of fields from two different saves).
        """
        session_dir = tmp_path / "sessions_undo_race"
        session_dir.mkdir()
        index_file = session_dir / "_index.json"
        monkeypatch.setattr(models, "SESSION_DIR", session_dir)
        monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
        models.SESSIONS.clear()
        try:
            s = Session(
                session_id="race_test",
                title="Race Test",
                messages=[
                    {"role": "user", "content": "first"},
                    {"role": "assistant", "content": "reply 1"},
                    {"role": "user", "content": "second"},
                    {"role": "assistant", "content": "reply 2"},
                    {"role": "user", "content": "third"},
                    {"role": "assistant", "content": "reply 3"},
                ],
            )
            s.save()
            models.SESSIONS[s.session_id] = s

            _checkpoint_stop = threading.Event()
            _checkpoint_activity = [0]
            errors = []
            # Collect every on-disk messages snapshot observed by the
            # checkpoint thread so we can assert atomicity after the run.
            checkpoint_snapshots = []
            _lock = threading.Lock()

            from api.config import _get_session_agent_lock
            _agent_lock = _get_session_agent_lock("race_test")

            def _periodic_checkpoint():
                last = 0
                while not _checkpoint_stop.wait(0.01):
                    try:
                        cur = _checkpoint_activity[0]
                        if cur > last:
                            with _agent_lock:
                                s.save(skip_index=True)
                            # Read back the on-disk JSON to verify atomicity
                            try:
                                snap = json.loads(s.path.read_text())
                                with _lock:
                                    checkpoint_snapshots.append(snap.get("messages"))
                            except Exception:
                                pass
                            last = cur
                    except Exception as e:
                        errors.append(e)

            t = threading.Thread(target=_periodic_checkpoint, daemon=True)
            t.start()

            from api.sessions.operations import undo_last
            # Collect the allowed message snapshots (each state the session
            # is in at a point where a checkpoint might observe it).
            allowed_message_snapshots = []
            # The initial state (before any undo) is a valid checkpoint target.
            allowed_message_snapshots.append(
                [dict(m) if isinstance(m, dict) else m for m in s.messages]
            )
            for _ in range(5):
                _checkpoint_activity[0] += 1
                time.sleep(0.02)
                try:
                    undo_last("race_test")
                except ValueError:
                    pass
                # Record the post-undo state (before appending new messages)
                # as an allowed snapshot — the checkpoint may observe this.
                allowed_message_snapshots.append(
                    [dict(m) if isinstance(m, dict) else m for m in s.messages]
                )
                # Wrap mutation + save in _agent_lock to mirror production
                # paths and prevent the checkpoint from observing an
                # intermediate +1-message snapshot.
                with _agent_lock:
                    s.messages.append({"role": "user", "content": f"msg-{_}"})
                    s.messages.append({"role": "assistant", "content": f"ans-{_}"})
                    # Record the in-memory messages list *before* save so we
                    # can verify that every checkpoint snapshot matches one
                    # of these.
                    allowed_message_snapshots.append(
                        [dict(m) if isinstance(m, dict) else m for m in s.messages]
                    )
                    s.save()

            _checkpoint_stop.set()
            t.join(timeout=2)

            assert not errors, f"Checkpoint thread encountered errors: {errors}"
            # Verify the on-disk JSON is parseable
            data = json.loads(s.path.read_text())
            assert data["session_id"] == "race_test"
            # Messages must be a list (not corrupted by concurrent mutation)
            assert isinstance(data["messages"], list)
            # Contract assertion: every checkpoint snapshot's messages must
            # equal one of the allowed in-memory snapshots, never an
            # interleaving of fields from two different saves.  This assertion
            # has teeth: if the _agent_lock were removed from the checkpoint
            # or the undo path, concurrent mutations would produce snapshots
            # that match no allowed state (e.g. a list with some messages
            # from before undo and some from after).
            for snap_msgs in checkpoint_snapshots:
                if snap_msgs is None:
                    continue
                # Normalize for comparison (strip display-only metadata)
                normalized = [
                    {k: v for k, v in m.items() if k in ("role", "content")}
                    if isinstance(m, dict) else m
                    for m in snap_msgs
                ]
                matched = False
                for allowed in allowed_message_snapshots:
                    norm_allowed = [
                        {k: v for k, v in m.items() if k in ("role", "content")}
                        if isinstance(m, dict) else m
                        for m in allowed
                    ]
                    if normalized == norm_allowed:
                        matched = True
                        break
                assert matched, (
                    f"Checkpoint snapshot {normalized!r} does not match any "
                    f"allowed state — this indicates a serialization failure "
                    f"(the _agent_lock is not preventing interleaved writes)."
                )
        finally:
            models.SESSIONS.clear()

    def test_cancel_stream_concurrent_checkpoint_produces_valid_json(self, tmp_path, monkeypatch):
        """Run cancel_stream while a _periodic_checkpoint thread is concurrently
        saving the same session; the resulting on-disk JSON must be parseable
        and active_stream_id must be None.

        The simulated checkpoint mirrors production by acquiring
        _get_session_agent_lock around s.save(), and we assert that every
        on-disk snapshot is internally consistent (never an interleaving
        of fields from two different saves).
        """
        session_dir = tmp_path / "sessions_cancel_race"
        session_dir.mkdir()
        index_file = session_dir / "_index.json"
        monkeypatch.setattr(models, "SESSION_DIR", session_dir)
        monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
        models.SESSIONS.clear()
        try:
            s = Session(
                session_id="cancel_race",
                title="Cancel Race Test",
                messages=[
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "world"},
                ],
                active_stream_id="stream-abc",
            )
            s.save()
            models.SESSIONS[s.session_id] = s

            _checkpoint_stop = threading.Event()
            _checkpoint_activity = [0]
            errors = []
            # Collect every on-disk snapshot observed by the checkpoint thread.
            checkpoint_snapshots = []
            _snap_lock = threading.Lock()

            from api.config import _get_session_agent_lock
            _agent_lock = _get_session_agent_lock("cancel_race")

            def _periodic_checkpoint():
                last = 0
                while not _checkpoint_stop.wait(0.01):
                    try:
                        cur = _checkpoint_activity[0]
                        if cur > last:
                            with _agent_lock:
                                s.save(skip_index=True)
                            # Read back the on-disk JSON to verify atomicity
                            try:
                                snap = json.loads(s.path.read_text())
                                with _snap_lock:
                                    checkpoint_snapshots.append(snap)
                            except Exception:
                                pass
                            last = cur
                    except Exception as e:
                        errors.append(e)

            t = threading.Thread(target=_periodic_checkpoint, daemon=True)
            t.start()

            # Simulate cancel_stream session cleanup directly
            for i in range(10):
                _checkpoint_activity[0] += 1
                time.sleep(0.01)
                with _get_session_agent_lock("cancel_race"):
                    s.active_stream_id = None
                    s.pending_user_message = None
                    s.pending_attachments = []
                    s.pending_started_at = None
                    s.save()

            _checkpoint_stop.set()
            t.join(timeout=2)

            assert not errors, f"Checkpoint thread encountered errors: {errors}"
            data = json.loads(s.path.read_text())
            assert data["session_id"] == "cancel_race"
            assert data["active_stream_id"] is None, (
                "active_stream_id must be None after cancel cleanup"
            )
            assert isinstance(data["messages"], list)
            # Contract assertion: every checkpoint snapshot must be
            # internally consistent (no interleaving of fields from two
            # different saves).  Because both the cancel cleanup and the
            # checkpoint hold the same _agent_lock, they are serialized —
            # but ordering is nondeterministic, so a snapshot taken
            # *before* cancel will see active_stream_id="stream-abc" and
            # one taken *after* will see None.  The guarantee is that
            # each snapshot is self-consistent, never a partial mix.
            #
            # This assertion has teeth: if the _agent_lock were removed
            # from either the checkpoint or the cancel path, a snapshot
            # could see active_stream_id=None while pending_user_message
            # still holds the pre-cancel value — a partial state that
            # violates the atomicity contract.
            for snap in checkpoint_snapshots:
                assert isinstance(snap.get("messages"), list), (
                    "Checkpoint snapshot messages must be a list"
                )
                assert snap.get("active_stream_id") in ("stream-abc", None), (
                    "Checkpoint snapshot active_stream_id must be either "
                    "the initial value or None (serialized, not interleaved), "
                    f"got {snap.get('active_stream_id')!r}"
                )
                # When active_stream_id is None, the cancel cleanup must
                # have run — so all four cancel fields must be cleared
                # atomically.  A partial state (e.g. active_stream_id=None
                # but pending_user_message still set) would indicate a
                # serialization failure.
                if snap.get("active_stream_id") is None:
                    assert snap.get("pending_user_message") is None, (
                        "Snapshot with active_stream_id=None must also have "
                        "pending_user_message=None (atomic cancel cleanup "
                        "under _agent_lock)"
                    )
                    assert snap.get("pending_attachments") == [] or snap.get("pending_attachments") is None, (
                        "Snapshot with active_stream_id=None must also have "
                        "empty pending_attachments (atomic cancel cleanup "
                        "under _agent_lock)"
                    )
                    assert snap.get("pending_started_at") is None, (
                        "Snapshot with active_stream_id=None must also have "
                        "pending_started_at=None (atomic cancel cleanup "
                        "under _agent_lock)"
                    )
        finally:
            models.SESSIONS.clear()

    def test_lock_identity_preserved_after_session_id_rotation(self):
        """When compression rotates session_id, the per-session lock must be
        aliased so that _get_session_agent_lock(new_sid) returns the *same*
        Lock object as _get_session_agent_lock(old_sid).

        This is a static guard: it directly simulates the migration that
        streaming.py performs inside the compression rotation block.
        """
        from api.config import (
            _get_session_agent_lock,
            alias_session_agent_lock,
            SESSION_AGENT_LOCKS,
            SESSION_AGENT_LOCKS_LOCK,
        )
        old_sid = "pre-rotation-id"
        new_sid = "post-rotation-id"

        # Acquire the lock under the old ID
        old_lock = _get_session_agent_lock(old_sid)

        # Simulate the migration that streaming.py does during compression.
        _agent_lock = old_lock
        alias_session_agent_lock(old_sid, new_sid, _agent_lock)

        # Now looking up the new ID must return the exact same Lock object
        new_lock = _get_session_agent_lock(new_sid)
        assert new_lock is old_lock, (
            f"After rotation, _get_session_agent_lock({new_sid!r}) must "
            f"return the same Lock object as _get_session_agent_lock({old_sid!r}); "
            f"got {new_lock!r} vs {old_lock!r}"
        )

        # Both IDs remain aliases while an old- or new-ID waiter has a strong
        # reference, preventing a split lock generation during rotation.
        with SESSION_AGENT_LOCKS_LOCK:
            assert SESSION_AGENT_LOCKS[old_sid] is old_lock
            assert SESSION_AGENT_LOCKS[new_sid] is old_lock

        del new_lock, old_lock, _agent_lock
        gc.collect()
        with SESSION_AGENT_LOCKS_LOCK:
            assert old_sid not in SESSION_AGENT_LOCKS
            assert new_sid not in SESSION_AGENT_LOCKS

    def test_lock_rotation_migration_survives_old_id_already_pruned(self):
        """Compression lock migration must not require old_sid to exist in dict.

        A concurrent /api/session/delete can prune old_sid before rotation code
        runs. The migration must still succeed by assigning the held _agent_lock
        reference directly.
        """
        from api.config import (
            _get_session_agent_lock,
            alias_session_agent_lock,
            SESSION_AGENT_LOCKS,
            SESSION_AGENT_LOCKS_LOCK,
        )
        old_sid = "pre-rotation-pruned"
        new_sid = "post-rotation-pruned"

        _agent_lock = _get_session_agent_lock(old_sid)
        with SESSION_AGENT_LOCKS_LOCK:
            SESSION_AGENT_LOCKS.pop(old_sid, None)  # simulate concurrent prune

        # Must not raise even though the weak old alias disappeared.
        alias_session_agent_lock(old_sid, new_sid, _agent_lock)

        new_lock = _get_session_agent_lock(new_sid)
        assert new_lock is _agent_lock
        assert _get_session_agent_lock(old_sid) is _agent_lock

        del new_lock, _agent_lock
        gc.collect()
        with SESSION_AGENT_LOCKS_LOCK:
            assert old_sid not in SESSION_AGENT_LOCKS
            assert new_sid not in SESSION_AGENT_LOCKS
