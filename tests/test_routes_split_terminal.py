"""Architecture and compatibility checks for embedded-terminal HTTP adapters."""

import io
import queue
import threading
from pathlib import Path
from types import SimpleNamespace

from api import routes
from api import terminal as terminal_domain
from api.routes_parts import terminal as terminal_http
from api.terminal import application


_LEGACY_ROUTE_EXPORTS = (
    "_terminal_session_lookup",
    "_terminal_remote_backend_enabled",
    "_handle_terminal_start",
    "_handle_terminal_input",
    "_handle_terminal_resize",
    "_handle_terminal_close",
    "_handle_terminal_output",
)


def test_terminal_is_real_package_with_small_public_interface():
    assert Path(terminal_domain.__file__).name == "__init__.py"
    assert not (Path(terminal_domain.__file__).parent.parent / "terminal.py").exists()
    assert set(terminal_domain.__all__) == {
        "REMOTE_BACKEND_UNSUPPORTED_ERROR",
        "REMOTE_BACKEND_UNSUPPORTED_MESSAGE",
        "RemoteTerminalBackendUnsupported",
        "TerminalEvent",
        "TerminalOutputSubscription",
        "TerminalSession",
        "attach_terminal_output",
        "close_all_terminals",
        "close_terminal",
        "get_terminal",
        "lookup_terminal_session",
        "resize_terminal",
        "start_terminal",
        "start_terminal_for_session",
        "terminal_remote_backend_enabled",
        "write_terminal",
    }


def test_terminal_domain_never_imports_route_or_streaming_facades():
    package_dir = Path(terminal_domain.__file__).parent
    source = "\n".join(path.read_text(encoding="utf-8") for path in package_dir.glob("*.py"))
    assert "api.routes" not in source
    assert "api.streaming" not in source
    assert "sys.modules" not in source


def test_terminal_legacy_exports_are_thin_routes_facade_adapters():
    for name in _LEGACY_ROUTE_EXPORTS:
        route_export = getattr(routes, name)
        assert route_export.__module__ == "api.routes"

    routes_source = Path(routes.__file__).read_text(encoding="utf-8")
    terminal_block = routes_source.split("from api import terminal as _terminal_domain", 1)[1]
    terminal_block = terminal_block.split("from api.routes_parts import media_files", 1)[0]
    assert "_install_routes_part" not in terminal_block
    assert "__routes_exports__" not in Path(terminal_http.__file__).read_text(encoding="utf-8")
    assert routes._REMOTE_TERMINAL_BACKEND_UNSUPPORTED_ERROR == (
        "remote_terminal_backend_unsupported"
    )
    assert routes._REMOTE_TERMINAL_BACKEND_UNSUPPORTED_MESSAGE == (
        "Embedded terminal is only supported for local terminal backends."
    )


def test_terminal_policy_resolves_session_backend_and_workspace_in_domain(monkeypatch, tmp_path):
    session = SimpleNamespace(workspace=str(tmp_path))
    seen = []
    monkeypatch.setattr(application.models, "get_session", lambda sid: session if sid == "s-1" else None)
    monkeypatch.setattr(application.config, "get_config", lambda: {"terminal": {"backend": "local"}})
    monkeypatch.setattr(
        application.workspace,
        "resolve_trusted_workspace",
        lambda raw: seen.append(raw) or tmp_path,
    )
    monkeypatch.setattr(
        application._RUNTIME,
        "start",
        lambda sid, workspace, **kwargs: (sid, workspace, kwargs),
    )

    result = application.start_terminal_for_session(
        " s-1 ", rows=31, cols=101, restart=True
    )

    assert result == (
        "s-1",
        tmp_path,
        {"rows": 31, "cols": 101, "restart": True},
    )
    assert seen == [str(tmp_path)]


def test_terminal_input_cap_blocks_before_domain_write(monkeypatch):
    writes = []
    monkeypatch.setattr(routes, "_embedded_terminal_gate_allows", lambda _handler: True)
    monkeypatch.setattr(
        terminal_http,
        "bad",
        lambda _handler, message, status=400: (message, status),
    )
    monkeypatch.setattr(
        terminal_domain,
        "write_terminal",
        lambda sid, data: writes.append((sid, data)),
    )

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
        self.sent_headers = []

    def send_response(self, status):
        self.responses.append(status)

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass


def test_terminal_output_keeps_deadline_heartbeat_and_terminal_close_contract(monkeypatch):
    class _ClosedOutput:
        def next_event(self, timeout):
            assert timeout == routes._SSE_HEARTBEAT_INTERVAL_SECONDS
            return None

        def is_closed_and_drained(self):
            return True

        def exit_code(self):
            return 17

    handler = _TerminalOutputHandler()
    calls = []
    monkeypatch.setattr(routes, "_embedded_terminal_gate_allows", lambda _handler: True)
    monkeypatch.setattr(terminal_http, "end_sse_headers", lambda _handler: calls.append("headers"))
    monkeypatch.setattr(routes, "_sse_set_write_deadline", lambda _handler: calls.append("deadline"))
    monkeypatch.setattr(routes, "_sse", lambda _handler, event, data: calls.append((event, data)))
    monkeypatch.setattr(terminal_domain, "attach_terminal_output", lambda sid: _ClosedOutput())

    result = routes._handle_terminal_output(
        handler,
        SimpleNamespace(query="session_id=s-1"),
    )

    assert result is True
    assert handler.responses == [200]
    assert handler.wfile.getvalue() == b": terminal heartbeat\n\n"
    assert calls == ["headers", "deadline", ("terminal_closed", {"exit_code": 17})]


def test_terminal_output_treats_client_disconnect_as_terminal(monkeypatch):
    class _DisconnectedOutput:
        def next_event(self, timeout):
            raise BrokenPipeError

    handler = _TerminalOutputHandler()
    monkeypatch.setattr(routes, "_embedded_terminal_gate_allows", lambda _handler: True)
    monkeypatch.setattr(terminal_http, "end_sse_headers", lambda _handler: None)
    monkeypatch.setattr(routes, "_sse_set_write_deadline", lambda _handler: None)
    monkeypatch.setattr(terminal_domain, "attach_terminal_output", lambda _sid: _DisconnectedOutput())

    assert routes._handle_terminal_output(
        handler,
        SimpleNamespace(query="session_id=s-1"),
    ) is True


def test_terminal_output_subscription_retains_session_across_registry_cleanup():
    closed = threading.Event()
    terminal_session = SimpleNamespace(
        output=queue.Queue(),
        closed=closed,
        proc=SimpleNamespace(poll=lambda: 23),
    )
    output = terminal_domain.TerminalOutputSubscription(terminal_session)
    terminal_session.output.put(("output", {"text": "still buffered"}))

    event = output.next_event(0.01)
    assert event == terminal_domain.TerminalEvent(
        "output", {"text": "still buffered"}
    )

    # The output handle owns a stable session reference, so registry removal
    # during process cleanup cannot invalidate an already-attached SSE request.
    closed.set()
    assert output.is_closed_and_drained() is True
    assert output.exit_code() == 23
