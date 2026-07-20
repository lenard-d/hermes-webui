"""Issue #4183: regenerate-title must materialize CLI/TUI sessions from state.db.

The /api/session/title/regenerate endpoint must use session materialization
so sessions that only exist in state.db (no sidecar JSON) can have their titles
regenerated instead of returning "Session not found".
"""

from types import SimpleNamespace
from urllib.parse import urlparse

import api.routes as routes
from api.http.routes import session_mutations


def _context(**overrides):
    context = dict(routes.__dict__)
    context.update(overrides)
    return context


def test_regenerate_endpoint_uses_materialize_fallback():
    calls = []
    responses = []
    session = SimpleNamespace(
        session_id="state-db-only",
        title="CLI Session",
        compact=lambda: {"session_id": "state-db-only", "title": "Generated title"},
    )

    def materialize(sid):
        calls.append(("materialize", sid))
        return session

    def persist(current, title, **_kwargs):
        calls.append(("persist", current.session_id, title))
        current.title = title

    context = _context(
        _get_or_materialize_session=materialize,
        get_session=lambda _sid: (_ for _ in ()).throw(
            AssertionError("bare session lookup must not serve this endpoint")
        ),
        generate_session_title_for_session=lambda *_args, **_kwargs: (
            "Generated title",
            "llm",
            "raw",
        ),
        _persist_generated_session_title=persist,
        bad=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("materializable state.db session was rejected")
        ),
        j=lambda _handler, payload, **_kwargs: responses.append(payload) or True,
    )

    result = session_mutations.handle_post(
        object(),
        urlparse("/api/session/title/regenerate"),
        {"session_id": session.session_id},
        None,
        context,
    )

    assert result is True
    assert calls == [
        ("materialize", session.session_id),
        ("persist", session.session_id, "Generated title"),
    ]
    assert responses[0]["title"] == "Generated title"


def test_regenerate_endpoint_catches_permission_error():
    responses = []

    def reject_read_only(_sid):
        raise PermissionError("foreign owner")

    context = _context(
        _get_or_materialize_session=reject_read_only,
        generate_session_title_for_session=lambda *_args, **_kwargs: (
            _ for _ in ()
        ).throw(AssertionError("title generation must not run after refusal")),
        bad=lambda _handler, message, status=400: responses.append(
            (message, status)
        )
        or True,
    )

    result = session_mutations.handle_post(
        object(),
        urlparse("/api/session/title/regenerate"),
        {"session_id": "read-only-import"},
        None,
        context,
    )

    assert result is True
    assert responses == [
        ("Read-only imported sessions cannot regenerate titles", 403)
    ]
