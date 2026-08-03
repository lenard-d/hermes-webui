"""Regression test for PR #1341 + Opus pre-release review of v0.50.246.

PR #1341 added context_length/threshold_tokens/last_prompt_tokens fields to
the Session model — but didn't add the writer that actually populates them
during streaming. The pre-release review caught this: without the writer,
the user-visible bug (context-ring shows 0% after page reload) would NOT
have been fixed by #1341 alone.

This test verifies that:
1. After a streaming turn completes, the session's context_length /
   threshold_tokens / last_prompt_tokens are written from the agent's
   compressor BEFORE s.save() is called (so they land on disk).
2. GET /api/session response includes the populated values.
3. A reloaded session retains the populated values.

Implementation reference: api/streaming.py around line 2188 (the per-turn
post-merge save) writes from getattr(agent, 'context_compressor', None).
"""
import inspect
import json
from unittest.mock import patch
from urllib.parse import urlparse

from api.sessions import session_detail_projection

def test_terminal_projection_persists_context_fields_on_session(monkeypatch):
    """The terminal owner projects all three compressor fields before save."""
    from types import SimpleNamespace

    from api.runs import local_context_window

    monkeypatch.setattr(
        local_context_window,
        "_resolve_model_context_length",
        lambda *_args, **_kwargs: 0,
    )
    projection = local_context_window.ContextWindowProjection.from_agent(
        SimpleNamespace(
            model="context-model",
            base_url="",
            api_key="",
            context_compressor=SimpleNamespace(
                context_length=200_000,
                threshold_tokens=180_000,
                last_prompt_tokens=45_123,
            ),
        ),
        resolved_model="context-model",
        resolved_provider="openrouter",
        resolved_base_url="",
        resolved_api_key="",
        config={},
    )
    session = SimpleNamespace(
        context_length=0,
        threshold_tokens=0,
        last_prompt_tokens=0,
    )

    projection.persist_on(session)

    assert (session.context_length, session.threshold_tokens, session.last_prompt_tokens) == (
        200_000,
        180_000,
        45_123,
    )


def test_session_init_accepts_context_fields():
    """Session exposes and applies the three fields as explicit named kwargs."""
    from api.sessions.store import Session

    parameters = inspect.signature(Session).parameters
    values = {
        "context_length": 200000,
        "threshold_tokens": 180000,
        "last_prompt_tokens": 45123,
    }
    for field in values:
        assert field in parameters, f"Session.__init__ must accept {field}"
        assert parameters[field].default is None

    session = Session(session_id="context-init", **values)
    for field, expected in values.items():
        assert getattr(session, field) == expected


def test_session_metadata_fields_include_context_fields(tmp_path, monkeypatch):
    """Metadata-only loading restores all three persisted context fields."""
    from api.sessions import store as models

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", sessions_dir)

    values = {
        "context_length": 200000,
        "threshold_tokens": 180000,
        "last_prompt_tokens": 45123,
    }
    session = models.Session(
        session_id="context-metadata",
        messages=[{"role": "user", "content": "hello"}],
        **values,
    )
    session.save(skip_index=True)

    payload = json.loads(session.path.read_text(encoding="utf-8"))
    for field, expected in values.items():
        assert payload[field] == expected

    metadata = models.Session.load_metadata_only(session.session_id)
    assert metadata is not None
    for field, expected in values.items():
        assert getattr(metadata, field) == expected


def test_session_compact_exposes_context_fields():
    """Session.compact() exposes the three context values unchanged."""
    from api.sessions.store import Session

    values = {
        "context_length": 200000,
        "threshold_tokens": 180000,
        "last_prompt_tokens": 45123,
    }
    compact = Session(session_id="context-compact", **values).compact()
    for field, expected in values.items():
        assert compact[field] == expected


def test_routes_session_get_returns_context_fields():
    """GET /api/session serializes the persisted context-window values."""
    from api import routes
    from api.sessions.store import Session

    session = Session(
        session_id="context-route",
        context_length=200000,
        threshold_tokens=180000,
        last_prompt_tokens=45123,
    )
    captured = {}

    def capture_json(_handler, data, status=200):
        captured["data"] = data
        captured["status"] = status
        return True

    parsed = urlparse(
        "/api/session?session_id=context-route&messages=0&resolve_model=0"
    )
    with (
        patch("api.routes.get_session", return_value=session),
        patch("api.routes.j", side_effect=capture_json),
        patch("api.routes._session_visible_to_active_profile", return_value=True),
        patch("api.routes._clear_stale_stream_state", return_value=None),
        patch.object(session_detail_projection, "sidecar_lineage_messages", return_value=[]),
        patch.object(session_detail_projection, "merge_lineage_messages", return_value=[]),
        patch("api.routes._active_stream_ids", return_value=set()),
    ):
        assert routes.handle_get(object(), parsed) is True

    response = captured["data"]["session"]
    assert captured["status"] == 200
    assert response["context_length"] == 200000
    assert response["threshold_tokens"] == 180000
    assert response["last_prompt_tokens"] == 45123


def test_session_round_trip_persists_context_fields(tmp_path, monkeypatch):
    """Real round-trip: save a Session with the fields set, reload, fields still there.

    Patches SESSION_DIR on the live api.models module so we don't pollute
    sys.modules state and break test ordering for sibling tests that depend
    on a stable api.models import (e.g. test_session_sidecar_repair.py).
    """
    from api.sessions import store as models

    # Use tmp_path as the session dir for this test only
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", sessions_dir)

    s = models.Session(session_id="ctxtest1", title="Context test")
    s.context_length = 200000
    s.threshold_tokens = 180000
    s.last_prompt_tokens = 45123
    s.save()

    # Reload from disk
    s2 = models.Session.load("ctxtest1")
    assert s2 is not None, "Session should reload"
    assert s2.context_length == 200000, f"context_length lost on reload: got {s2.context_length}"
    assert s2.threshold_tokens == 180000, f"threshold_tokens lost on reload: got {s2.threshold_tokens}"
    assert s2.last_prompt_tokens == 45123, f"last_prompt_tokens lost on reload: got {s2.last_prompt_tokens}"
