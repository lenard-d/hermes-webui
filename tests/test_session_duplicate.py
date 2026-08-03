"""
End-to-end and owner-level tests for /api/session/duplicate.

Tests verify that:
1. A new session is created as a copy of the original
2. All messages are copied correctly
3. The duplicate is independent from the original
4. Error handling works properly
"""
from types import SimpleNamespace

import pytest

from tests.conftest import TEST_BASE, _post


def test_duplicate_session_handles_missing_session_id(cleanup_test_sessions):
    """
    Test that duplicate endpoint returns error when session_id is missing.
    """
    # Try to duplicate without session_id
    r = _post(TEST_BASE, '/api/session/duplicate', {})

    assert 'error' in r, "Should return error when session_id is missing"


def test_duplicate_session_handles_invalid_session_id(cleanup_test_sessions):
    """
    Test that duplicate endpoint returns error when session doesn't exist.
    """
    # Try to duplicate non-existent session
    r = _post(TEST_BASE, '/api/session/duplicate', {'session_id': 'nonexistent_xyz'})

    # Should return an error (could be auth error or not found)
    assert 'error' in r, "Should return error when session not found"
    # Check that we got some kind of error response
    assert r.get('error') is not None or 'error' in r, \
        f"Should return error when session not found. Got: {r}"


def test_duplicate_session_handles_empty_session_id(cleanup_test_sessions):
    """
    Test that duplicate endpoint returns error when session_id is empty string.
    """
    # Try to duplicate with empty session_id
    r = _post(TEST_BASE, '/api/session/duplicate', {'session_id': ''})

    assert 'error' in r, "Should return error when session_id is empty"


@pytest.fixture
def duplicate_owner_case(tmp_path, monkeypatch):
    """Run the dedicated HTTP owner with a rich source session."""
    import api.routes as route_facade
    from api.http.routes import session_creation_mutations
    from api.sessions import foreign_session_access

    source = route_facade.Session(
        session_id="source-session",
        title="Original",
        workspace=tmp_path,
        model="provider/model",
        model_provider="provider",
        messages=[{"role": "user", "content": ["hello", {"nested": [1]}]}],
        tool_calls=[{"name": "search", "args": {"queries": ["one"]}}],
        pinned=True,
        archived=True,
        project_id="project-1",
        profile="profile-1",
        input_tokens=11,
        output_tokens=7,
        estimated_cost=0.25,
        cache_read_tokens=5,
        cache_write_tokens=3,
        personality="focused",
        enabled_toolsets=["web"],
        context_length=32000,
        threshold_tokens=24000,
        truncation_watermark=123.0,
        truncation_boundary={"message_count": 1},
        context_messages=[{"role": "system", "content": {"facts": ["one"]}}],
        gateway_routing={"provider": "provider", "fallbacks": ["backup"]},
        gateway_routing_history=[{"provider": "provider"}],
        llm_title_generated=True,
        manual_title=True,
        composer_draft={"text": "draft", "attachments": ["a.txt"]},
        context_engine="legacy",
        context_engine_state={"cursor": [1]},
        parent_session_id="source-parent",
        active_stream_id="source-stream",
        pending_user_message="pending",
    )

    class SessionForDuplicate(route_facade.Session):
        @classmethod
        def load(cls, session_id):
            return source if session_id == source.session_id else None

    timeline = []
    published = []
    responses = []
    ctx = dict(vars(route_facade))
    ctx.update(
        Session=SessionForDuplicate,
        publish_session_list_changed=lambda reason, **details: timeline.append(
            ("event", reason, details)
        ),
        j=lambda _handler, payload, status=200, **_kwargs: (
            responses.append((payload, status)),
            timeline.append(("response", status)),
            True,
        )[-1],
    )
    monkeypatch.setattr(foreign_session_access, "is_view_only", lambda _sid: False)
    monkeypatch.setattr(
        foreign_session_access,
        "publish",
        lambda session, *, persist: (
            published.append((session, persist)),
            timeline.append(("materialize", persist)),
        ),
    )
    result = session_creation_mutations.handle_post(
        object(),
        SimpleNamespace(path="/api/session/duplicate"),
        {"session_id": source.session_id},
        None,
        ctx,
    )

    assert result is True
    assert len(published) == 1
    return SimpleNamespace(
        source=source,
        copied=published[0][0],
        persist=published[0][1],
        responses=responses,
        timeline=timeline,
    )


def test_duplicate_session_endpoint_exists(duplicate_owner_case):
    case = duplicate_owner_case
    assert case.responses[0][1] == 200
    assert case.responses[0][0]["session"]["session_id"] == case.copied.session_id


def test_duplicate_creates_independent_session(duplicate_owner_case):
    case = duplicate_owner_case
    assert case.copied.session_id != case.source.session_id
    assert case.copied.parent_session_id is None
    case.copied.messages[0]["content"][1]["nested"].append(2)
    case.copied.tool_calls[0]["args"]["queries"].append("two")
    assert case.source.messages[0]["content"][1]["nested"] == [1]
    assert case.source.tool_calls[0]["args"]["queries"] == ["one"]


def test_duplicate_session_copies_title_logic(duplicate_owner_case):
    assert duplicate_owner_case.copied.title == "Original (copy)"


def test_duplicate_session_copies_messages_logic(duplicate_owner_case):
    case = duplicate_owner_case
    assert case.copied.messages == case.source.messages
    assert case.copied.messages is not case.source.messages


def test_duplicate_session_copies_model_logic(duplicate_owner_case):
    case = duplicate_owner_case
    assert case.copied.model == case.source.model
    assert case.copied.model_provider == case.source.model_provider


def test_duplicate_session_copies_workspace_logic(duplicate_owner_case):
    assert duplicate_owner_case.copied.workspace == duplicate_owner_case.source.workspace


def test_duplicate_session_copies_all_session_properties(duplicate_owner_case):
    case = duplicate_owner_case
    copied_fields = (
        "project_id",
        "profile",
        "input_tokens",
        "output_tokens",
        "estimated_cost",
        "cache_read_tokens",
        "cache_write_tokens",
        "personality",
        "enabled_toolsets",
        "context_length",
        "threshold_tokens",
        "truncation_watermark",
        "truncation_boundary",
        "llm_title_generated",
        "manual_title",
        "context_engine",
    )
    for field in copied_fields:
        assert getattr(case.copied, field) == getattr(case.source, field)


def test_duplicate_uses_deepcopy_for_messages(duplicate_owner_case):
    case = duplicate_owner_case
    mutable_fields = (
        "messages",
        "tool_calls",
        "context_messages",
        "gateway_routing",
        "gateway_routing_history",
        "composer_draft",
        "context_engine_state",
    )
    for field in mutable_fields:
        assert getattr(case.copied, field) == getattr(case.source, field)
        assert getattr(case.copied, field) is not getattr(case.source, field)


def test_duplicate_explicitly_persists_before_cache_publication(duplicate_owner_case):
    case = duplicate_owner_case
    assert case.persist is True
    assert [entry[0] for entry in case.timeline] == [
        "materialize",
        "event",
        "response",
    ]
    assert case.timeline[1][1] == "session_duplicate"


def test_duplicate_resets_pinned_and_archived(duplicate_owner_case):
    assert duplicate_owner_case.source.pinned is True
    assert duplicate_owner_case.source.archived is True
    assert duplicate_owner_case.copied.pinned is False
    assert duplicate_owner_case.copied.archived is False


def test_duplicate_returns_404_when_session_not_found():
    import api.routes as route_facade
    from api.http.routes import session_creation_mutations

    class MissingSession(route_facade.Session):
        @classmethod
        def load(cls, _session_id):
            return None

    errors = []
    ctx = dict(vars(route_facade))
    ctx.update(
        Session=MissingSession,
        _session_is_subagent_view_only=lambda _session_id: False,
        bad=lambda _handler, message, status=400: errors.append((message, status))
        or True,
    )

    result = session_creation_mutations.handle_post(
        object(),
        SimpleNamespace(path="/api/session/duplicate"),
        {"session_id": "missing"},
        None,
        ctx,
    )

    assert result is True
    assert errors == [("Session not found", 404)]


def test_duplicate_owner_ignores_unrelated_routes():
    import api.routes as route_facade
    from api.http.context import UNHANDLED
    from api.http.routes import session_creation_mutations

    result = session_creation_mutations.handle_post(
        object(),
        SimpleNamespace(path="/api/session/not-duplicate"),
        {},
        None,
        dict(vars(route_facade)),
    )

    assert result is UNHANDLED
