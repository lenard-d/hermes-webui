"""Compatibility checks for the embedded-terminal route-domain extraction."""

import io
import queue
import threading
from types import SimpleNamespace

from api import routes
from api.routes_parts import terminal


_TERMINAL_FUNCTION_EXPORTS = (
    "_terminal_session_lookup",
    "_terminal_remote_backend_enabled",
    "_handle_terminal_start",
    "_handle_terminal_input",
    "_handle_terminal_resize",
    "_handle_terminal_close",
    "_handle_terminal_output",
)


def test_terminal_part_declares_the_complete_cohesive_http_owner():
    assert terminal.__routes_exports__ == (
        "_terminal_session_lookup",
        "_REMOTE_TERMINAL_BACKEND_UNSUPPORTED_ERROR",
        "_REMOTE_TERMINAL_BACKEND_UNSUPPORTED_MESSAGE",
        "_terminal_remote_backend_enabled",
        "_handle_terminal_start",
        "_handle_terminal_input",
        "_handle_terminal_resize",
        "_handle_terminal_close",
        "_handle_terminal_output",
    )


def test_terminal_exports_remain_owned_by_routes_facade():
    for name in _TERMINAL_FUNCTION_EXPORTS:
        route_export = getattr(routes, name)
        assert route_export.__module__ == "api.routes"
        assert route_export.__globals__ is vars(routes)

    assert routes._REMOTE_TERMINAL_BACKEND_UNSUPPORTED_ERROR == (
        "remote_terminal_backend_unsupported"
    )
    assert routes._REMOTE_TERMINAL_BACKEND_UNSUPPORTED_MESSAGE == (
        "Embedded terminal is only supported for local terminal backends."
    )


def test_terminal_backend_policy_resolves_facade_monkeypatches_at_call_time(monkeypatch):
    seen = []
    terminal_config = {"backend": "ssh"}

    monkeypatch.setattr(routes, "get_config", lambda: {"terminal": terminal_config})
    monkeypatch.setattr(
        routes,
        "_is_remote_terminal_backend",
        lambda config: seen.append(config) or True,
    )

    assert routes._terminal_remote_backend_enabled() is True
    assert seen == [terminal_config]


def test_terminal_session_lookup_resolves_facade_monkeypatch_at_call_time(monkeypatch):
    session = object()
    monkeypatch.setattr(routes, "get_session", lambda sid: session if sid == "s-1" else None)

    assert routes._terminal_session_lookup({"session_id": " s-1 "}) == ("s-1", session)


def test_terminal_input_cap_still_blocks_before_pty_write(monkeypatch):
    writes = []
    monkeypatch.setattr(routes, "_embedded_terminal_gate_allows", lambda _handler: True)
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, message, status=400: (message, status),
    )
    monkeypatch.setattr("api.terminal.write_terminal", lambda sid, data: writes.append((sid, data)))

    result = routes._handle_terminal_input(
        object(),
        {"session_id": "s-1", "data": "x" * 8193},
    )

    assert result == ("input too large", 413)
    assert writes == []


class _TerminalOutputHandler:
    def __init__(self):
        self.wfile = io.BytesIO()
        self.responses = []
        self.headers = []

    def send_response(self, status):
        self.responses.append(status)

    def send_header(self, name, value):
        self.headers.append((name, value))

    def end_headers(self):
        pass


def test_terminal_output_keeps_deadline_heartbeat_and_terminal_close_contract(monkeypatch):
    class _IdleOutput:
        def get(self, *, timeout):
            raise queue.Empty

        def empty(self):
            return True

    closed = threading.Event()
    closed.set()
    term = SimpleNamespace(
        output=_IdleOutput(),
        closed=closed,
        proc=SimpleNamespace(poll=lambda: 17),
    )
    handler = _TerminalOutputHandler()
    calls = []

    monkeypatch.setattr(routes, "_embedded_terminal_gate_allows", lambda _handler: True)
    monkeypatch.setattr(routes, "end_sse_headers", lambda _handler: calls.append("headers"))
    monkeypatch.setattr(routes, "_sse_set_write_deadline", lambda _handler: calls.append("deadline"))
    monkeypatch.setattr(
        routes,
        "_sse",
        lambda _handler, event, data: calls.append((event, data)),
    )
    monkeypatch.setattr("api.terminal.get_terminal", lambda sid: term if sid == "s-1" else None)

    result = routes._handle_terminal_output(
        handler,
        SimpleNamespace(query="session_id=s-1"),
    )

    assert result is True
    assert handler.responses == [200]
    assert handler.wfile.getvalue() == b": terminal heartbeat\n\n"
    assert calls == ["headers", "deadline", ("terminal_closed", {"exit_code": 17})]


def test_terminal_output_still_treats_client_disconnect_as_terminal(monkeypatch):
    class _DisconnectedOutput:
        def get(self, *, timeout):
            raise BrokenPipeError

    term = SimpleNamespace(output=_DisconnectedOutput())
    handler = _TerminalOutputHandler()

    monkeypatch.setattr(routes, "_embedded_terminal_gate_allows", lambda _handler: True)
    monkeypatch.setattr(routes, "end_sse_headers", lambda _handler: None)
    monkeypatch.setattr(routes, "_sse_set_write_deadline", lambda _handler: None)
    monkeypatch.setattr("api.terminal.get_terminal", lambda _sid: term)

    assert routes._handle_terminal_output(
        handler,
        SimpleNamespace(query="session_id=s-1"),
    ) is True
