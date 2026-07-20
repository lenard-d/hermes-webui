"""HTTP and SSE adapters for the embedded-terminal domain."""

from __future__ import annotations

from urllib.parse import parse_qs

from api import terminal
from api.helpers import _sanitize_error, bad, j, require
from api.sse_chunked import end_sse_headers


def handle_terminal_start(handler, body, *, gate, gate_denied_message):
    try:
        if not gate(handler):
            return bad(handler, gate_denied_message, 403)
        terminal_session = terminal.start_terminal_for_session(
            body.get("session_id"),
            rows=int(body.get("rows") or 24),
            cols=int(body.get("cols") or 80),
            restart=bool(body.get("restart")),
        )
        return j(
            handler,
            {
                "ok": True,
                "session_id": terminal_session.session_id,
                "workspace": terminal_session.workspace,
                "running": terminal_session.is_alive(),
            },
        )
    except terminal.RemoteTerminalBackendUnsupported as exc:
        return j(
            handler,
            {"error": exc.code, "message": str(exc)},
            status=400,
        )
    except KeyError as exc:
        return bad(handler, str(exc), 404)
    except ValueError as exc:
        return bad(handler, str(exc), 400)
    except Exception as exc:
        return bad(handler, _sanitize_error(exc), 500)


def handle_terminal_input(handler, body, *, gate, gate_denied_message):
    try:
        if not gate(handler):
            return bad(handler, gate_denied_message, 403)
        require(body, "session_id")
        data = str(body.get("data", ""))
        if len(data) > 8192:
            return bad(handler, "input too large", 413)
        terminal.write_terminal(body["session_id"], data)
        return j(handler, {"ok": True})
    except KeyError as exc:
        return bad(handler, str(exc), 404)
    except ValueError as exc:
        return bad(handler, str(exc), 400)
    except Exception as exc:
        return bad(handler, _sanitize_error(exc), 500)


def handle_terminal_resize(handler, body, *, gate, gate_denied_message):
    try:
        if not gate(handler):
            return bad(handler, gate_denied_message, 403)
        require(body, "session_id")
        terminal.resize_terminal(
            body["session_id"],
            rows=int(body.get("rows") or 24),
            cols=int(body.get("cols") or 80),
        )
        return j(handler, {"ok": True})
    except KeyError as exc:
        return bad(handler, str(exc), 404)
    except ValueError as exc:
        return bad(handler, str(exc), 400)
    except Exception as exc:
        return bad(handler, _sanitize_error(exc), 500)


def handle_terminal_close(handler, body, *, gate, gate_denied_message):
    try:
        if not gate(handler):
            return bad(handler, gate_denied_message, 403)
        require(body, "session_id")
        closed = terminal.close_terminal(body["session_id"])
        return j(handler, {"ok": True, "closed": closed})
    except ValueError as exc:
        return bad(handler, str(exc), 400)


def handle_terminal_output(
    handler,
    parsed,
    *,
    gate,
    gate_denied_message,
    heartbeat_seconds,
    send_event,
    set_write_deadline,
):
    if not gate(handler):
        return bad(handler, gate_denied_message, 403)
    session_id = parse_qs(parsed.query).get("session_id", [""])[0]
    if not session_id:
        return bad(handler, "session_id required")
    output = terminal.attach_terminal_output(session_id)
    if output is None:
        return j(handler, {"error": "terminal not running"}, status=404)

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("X-Accel-Buffering", "no")
    handler.send_header("Connection", "close")
    end_sse_headers(handler)
    set_write_deadline(handler)
    try:
        while True:
            event = output.next_event(heartbeat_seconds)
            if event is None:
                handler.wfile.write(b": terminal heartbeat\n\n")
                handler.wfile.flush()
                if output.is_closed_and_drained():
                    send_event(
                        handler,
                        "terminal_closed",
                        {"exit_code": output.exit_code()},
                    )
                    break
                continue
            send_event(handler, event.name, event.payload)
            if event.name in ("terminal_closed", "terminal_error"):
                break
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        pass
    return True
