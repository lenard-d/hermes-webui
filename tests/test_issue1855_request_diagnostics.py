import json
import logging
from urllib.parse import urlsplit

import api.routes as routes
import api.sessions.cache as session_cache
import api.sessions.records as session_records
import api.sessions.sidebar as session_sidebar
from api.http import router
from api.http.context import UNHANDLED
from api.http.routes import (
    platform_mutations,
    provider_mutations,
    session_creation_mutations,
    session_queries,
)
from api.request_diagnostics import RequestDiagnostics
from api.sessions.records import Session


class _StageRecorder:
    def __init__(self):
        self.stages = []
        self.finished = False

    def stage(self, name):
        self.stages.append(name)

    def finish(self):
        self.finished = True


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
    for owner in (session_cache, session_records, session_sidebar):
        monkeypatch.setattr(owner, "SESSION_DIR", session_dir)
        monkeypatch.setattr(owner, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(
        session_sidebar,
        "_enrich_sidebar_lineage_metadata",
        lambda sessions: None,
    )
    session_records.SESSIONS.clear()

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
    rows = session_sidebar.all_sessions(diag=diag)

    assert [row["session_id"] for row in rows] == [s.session_id]
    assert "all_sessions.read_index" in diag.stages
    assert "all_sessions.overlay_lock" in diag.stages
    assert "all_sessions.lineage_metadata" in diag.stages


def test_get_sessions_passes_diagnostics_to_cache_owner(monkeypatch):
    from api import profiles as profiles_api

    diagnostics = []

    class DiagnosticsOwner:
        @classmethod
        def maybe_start(cls, method, path, **_kwargs):
            diag = _StageRecorder()
            diagnostics.append((method, path, diag))
            return diag

    context = dict(vars(routes))
    captured = {}

    def build_session_list_payload(**kwargs):
        captured["builder_diag"] = kwargs["diag"]
        return {"sessions": [], "cli_count": 0}

    def get_cached_session_list_payload(*, key, builder, diag):
        captured["cache_key"] = key
        captured["cache_diag"] = diag
        return builder()

    context.update(
        {
            "RequestDiagnostics": DiagnosticsOwner,
            "load_settings": lambda: {},
            "_session_list_cache_key": lambda **_kwargs: ("diagnostics",),
            "_get_cached_session_list_payload": get_cached_session_list_payload,
            "_build_session_list_cache_payload": build_session_list_payload,
            "_session_list_payload_to_response": lambda payload: payload,
            "j": lambda _handler, payload, **_kwargs: payload,
        }
    )
    monkeypatch.setattr(profiles_api, "get_active_profile_name", lambda: "default")

    result = session_queries.handle_get(
        object(),
        urlsplit("/api/sessions?all_profiles=1"),
        context,
    )

    assert result == {"sessions": [], "cli_count": 0}
    method, path, diag = diagnostics.pop(0)
    assert (method, path) == ("GET", "/api/sessions")
    assert captured == {
        "cache_key": ("diagnostics",),
        "cache_diag": diag,
        "builder_diag": diag,
    }
    assert diag.stages == ["load_settings", "response_write"]
    assert diag.finished is True
    assert diagnostics == []


def test_chat_start_passes_diagnostics_through_http_owner(monkeypatch):
    diagnostics = []

    class DiagnosticsOwner:
        @classmethod
        def maybe_start(cls, method, path, **_kwargs):
            diag = _StageRecorder()
            diagnostics.append((method, path, diag))
            return diag

    for owner in (platform_mutations, session_creation_mutations, provider_mutations):
        monkeypatch.setattr(owner, "handle_post", lambda *_args, **_kwargs: UNHANDLED)

    context = dict(vars(routes))
    captured = {}

    def handle_chat_start(_handler, body, diag=None):
        captured["body"] = body
        captured["diag"] = diag
        return True

    context.update(
        {
            "RequestDiagnostics": DiagnosticsOwner,
            "_csrf_exempt_path": lambda _path: False,
            "_check_csrf": lambda _handler: True,
            "_handle_extension_sidecar_proxy": lambda *_args, **_kwargs: False,
            "read_body": lambda _handler: {
                "session_id": "diagnostic-admission",
                "message": "trace admission",
            },
            "_guard_request_session_visibility": lambda *_args, **_kwargs: True,
            "_handle_chat_start": handle_chat_start,
        }
    )

    assert router.handle_post(
        object(),
        urlsplit("/api/chat/start"),
        context,
    ) is True
    method, path, diag = diagnostics.pop(0)
    assert (method, path) == ("POST", "/api/chat/start")
    assert captured == {
        "body": {
            "session_id": "diagnostic-admission",
            "message": "trace admission",
        },
        "diag": diag,
    }
    assert diag.stages == ["csrf", "read_body"]
    assert diagnostics == []


def test_local_turn_reports_internal_diagnostic_stages(monkeypatch):
    import api.config as config
    from api.runs import admission as turn_admission

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
            session_records.SESSIONS.pop("diagnostic-admission", None)
