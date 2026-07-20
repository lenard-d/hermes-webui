"""Regression coverage for #1823 Kanban stale-client/board-pointer failures."""

from __future__ import annotations
from tests.frontend_asset_contract import family_source


import io
import json
import pytest
from types import SimpleNamespace
from urllib.parse import urlparse

from api import routes

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
PANELS = family_source("panels")


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.response_headers = []
        self.wfile = io.BytesIO()
        self.rfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass

    def body_json(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def test_unknown_kanban_endpoint_get_returns_stale_client_diagnostic():
    """Obsolete/stale JS should not collapse to a bare `not found` 404."""
    handler = _FakeHandler()
    handled = routes.handle_get(handler, urlparse("/api/kanban/obsolete-shape"))

    assert handled is True
    assert handler.status == 404
    error = handler.body_json()["error"]
    assert error != "not found"
    assert "unknown Kanban endpoint: GET /api/kanban/obsolete-shape" in error
    assert "stale cached bundle" in error
    assert "Hard refresh now" in error


@pytest.mark.parametrize("method", ("GET", "POST", "PATCH", "DELETE"))
def test_unknown_kanban_endpoint_routes_are_wrapped_for_all_methods(method):
    handler = _FakeHandler()
    handler_fn = getattr(routes, f"handle_{method.lower()}")

    handled = handler_fn(handler, urlparse("/api/kanban/obsolete-shape"))

    assert handled is True
    assert handler.status == 404
    error = handler.body_json()["error"]
    assert f"unknown Kanban endpoint: {method} /api/kanban/obsolete-shape" in error
    assert "stale cached bundle" in error


def test_kanban_stale_client_error_renders_hard_refresh_escape_hatch():
    assert "function _kanbanLooksLikeStaleClientError(err)" in PANELS
    assert "err.status === 404" in PANELS
    assert "msg.includes('unknown kanban endpoint')" in PANELS
    assert "msg.includes('stale cached bundle')" in PANELS
    assert "Kanban needs a hard refresh" in PANELS
    assert "Hard refresh now" in PANELS
    assert "navigator.serviceWorker.getRegistrations()" in PANELS
    assert "caches.keys()" in PANELS
    assert "window.location.reload()" in PANELS


@pytest.mark.parametrize(
    ("method", "path", "payload_attr", "payload_error"),
    [
        ("GET", "/api/kanban/tasks/abc/log", "_task_log_payload", "task not found"),
        ("POST", "/api/kanban/boards", "_create_board_payload", "invalid board payload"),
        ("PATCH", "/api/kanban/boards/abc", "_update_board_payload", "invalid patch payload"),
        ("DELETE", "/api/kanban/links", "_link_tasks_payload", "invalid delete payload"),
    ],
)
def test_inner_handler_bad_response_does_not_emit_double_404(
    method, path, payload_attr, payload_error, monkeypatch
):
    """Regression: when the kanban bridge already sent a response via bad()
    (returns None), the unknown-endpoint wrapper must not concatenate a second
    404 body on the wire. Only an explicit `False` from the bridge means the
    path was unmatched.
    """
    from api.kanban import http as kanban_http

    # Force one kanban payload helper to hit bad() and return None, so the
    # wrapper path should not append _kanban_unknown_endpoint.
    monkeypatch.setattr(
        kanban_http, payload_attr, lambda *a, **kw: (_ for _ in ()).throw(LookupError(payload_error))
    )

    handler = _FakeHandler()
    handler_fn = getattr(routes, f"handle_{method.lower()}")
    if method == "GET":
        handled = handler_fn(handler, urlparse(path))
    else:
        handled = handler_fn(handler, urlparse(path))

    assert handled is True
    assert handler.status == 404
    body = handler.wfile.getvalue().decode("utf-8")
    # Exactly one JSON object should have been written. Two concatenated
    # objects would produce something like `}{` between them.
    assert body.count("}{") == 0, f"double response detected: {body!r}"
    payload = json.loads(body)
    assert payload["error"] == payload_error


def test_kanban_load_resolves_board_before_board_scoped_requests():
    boards_pos = PANELS.find("await loadKanbanBoards();")
    config_pos = PANELS.find("api('/api/kanban/config' + _kanbanBoardQuery())")
    assert boards_pos != -1
    assert config_pos != -1
    assert boards_pos < config_pos
    assert "_kanbanSetSavedBoard('default');" in PANELS
