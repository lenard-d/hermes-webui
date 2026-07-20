import json
import logging
from pathlib import Path

import api.sessions.store as models
from api.sessions.store import Session
from api.request_diagnostics import RequestDiagnostics


class _StageRecorder:
    def __init__(self):
        self.stages = []

    def stage(self, name):
        self.stages.append(name)


def test_request_diagnostics_timeout_record_includes_stage_and_thread_stacks(caplog):
    logger = logging.getLogger("test.issue1855.timeout")
    diag = RequestDiagnostics(
        "GET",
        "/api/sessions?all_profiles=1",
        logger=logger,
        timeout_seconds=5,
        auto_start=False,
    )
    diag.stage("all_sessions.read_index")

    with caplog.at_level(logging.WARNING, logger=logger.name):
        diag._on_timeout()

    assert len(caplog.records) == 1
    record = json.loads(caplog.records[0].args[0])
    assert record["method"] == "GET"
    assert record["path"] == "/api/sessions"
    assert record["current_stage"] == "all_sessions.read_index"
    assert record["elapsed_ms"] >= 0
    assert any(stage["name"] == "all_sessions.read_index" for stage in record["stages"])
    assert record["thread_stacks"]


def test_request_diagnostics_maybe_start_is_limited_to_issue1855_paths():
    assert RequestDiagnostics.maybe_start("GET", "/api/sessions") is not None
    assert RequestDiagnostics.maybe_start("POST", "/api/chat/start") is not None
    assert RequestDiagnostics.maybe_start("GET", "/health") is None
    assert RequestDiagnostics.maybe_start("POST", "/api/session/new") is None


def test_all_sessions_reports_internal_index_stages(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "_enrich_sidebar_lineage_metadata", lambda sessions: None)
    models.SESSIONS.clear()

    s = Session(
        session_id="issue1855_indexed",
        title="Indexed",
        messages=[{"role": "user", "content": "hi", "timestamp": 100}],
    )
    s.path.write_text(json.dumps(s.__dict__, ensure_ascii=False), encoding="utf-8")
    index_file.write_text(
        json.dumps(
            [
                {
                    "session_id": s.session_id,
                    "title": s.title,
                    "updated_at": s.updated_at,
                    "workspace": s.workspace,
                    "model": s.model,
                    "message_count": 1,
                    "created_at": s.created_at,
                    "pinned": False,
                    "archived": False,
                    "last_message_at": 100,
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    diag = _StageRecorder()
    rows = models.all_sessions(diag=diag)

    assert [row["session_id"] for row in rows] == [s.session_id]
    assert "all_sessions.read_index" in diag.stages
    assert "all_sessions.overlay_lock" in diag.stages
    assert "all_sessions.lineage_metadata" in diag.stages


def test_issue1855_target_routes_are_wired_to_diagnostics(monkeypatch):
    import api.config as config
    import api.turn_admission as turn_admission

    src = Path("api/routes.py").read_text(encoding="utf-8")

    assert 'RequestDiagnostics.maybe_start("GET", parsed.path' in src
    assert "all_sessions(diag=diag, include_lineage_metadata=False)" in src
    assert 'RequestDiagnostics.maybe_start("POST", parsed.path' in src
    assert "_handle_chat_start(handler, body, diag=diag)" in src
    for stage in ("read_body", "resolve_model_provider", "response_write"):
        assert stage in src

    class Session:
        session_id = "diagnostic-admission"
        profile = None
        title = "Diagnostics"
        active_stream_id = None
        pending_user_message = None
        pending_started_at = None
        messages = []
        worktree_path = None

        def save(self, *args, **kwargs):
            return None

    monkeypatch.setattr(
        turn_admission,
        "append_turn_journal_event",
        lambda session_id, event: {**event, "session_id": session_id},
    )
    monkeypatch.setattr(turn_admission, "set_last_workspace", lambda _path: None)
    diag = _StageRecorder()
    result = turn_admission.start_local_turn(
        Session(),
        turn_admission.LocalTurnRequest(
            message="trace admission",
            attachments=[],
            workspace="/tmp/workspace",
            model="test-model",
        ),
        worker_target=lambda *_a, **_k: None,
        clear_stale_stream=lambda _session: False,
        diag=diag,
    )
    try:
        for stage in (
            "active_stream_check",
            "session_lock_wait",
            "save_pending_state",
            "stream_registration",
            "worker_thread_start",
        ):
            assert stage in diag.stages
    finally:
        config.finish_runtime_run(result["stream_id"])
        with config.LOCK:
            models.SESSIONS.pop("diagnostic-admission", None)
