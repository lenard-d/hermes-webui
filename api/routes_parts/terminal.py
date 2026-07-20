"""Embedded-terminal HTTP routes and their local-backend safety policy."""

from __future__ import annotations

import queue
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from api.sse_chunked import end_sse_headers

if TYPE_CHECKING:
    from api.config import get_config
    from api.helpers import _sanitize_error, bad, j, require
    from api.models import get_session
    from api.routes import (
        _EMBEDDED_TERMINAL_GATE_DENIED_MESSAGE,
        _SSE_HEARTBEAT_INTERVAL_SECONDS,
        _embedded_terminal_gate_allows,
        _is_remote_terminal_backend,
        _sse,
        _sse_set_write_deadline,
    )
    from api.workspace import resolve_trusted_workspace


def _terminal_session_lookup(body_or_query):
    sid = str(body_or_query.get("session_id", "")).strip()
    if not sid:
        raise ValueError("session_id required")
    try:
        s = get_session(sid)
    except KeyError:
        raise KeyError("Session not found")  # noqa: B904 - preserve legacy exception shape
    return sid, s


_REMOTE_TERMINAL_BACKEND_UNSUPPORTED_ERROR = "remote_terminal_backend_unsupported"
_REMOTE_TERMINAL_BACKEND_UNSUPPORTED_MESSAGE = (
    "Embedded terminal is only supported for local terminal backends."
)


def _terminal_remote_backend_enabled() -> bool:
    terminal_cfg = get_config().get("terminal", {})
    return _is_remote_terminal_backend(terminal_cfg)


def _handle_terminal_start(handler, body):
    try:
        if not _embedded_terminal_gate_allows(handler):
            return bad(handler, _EMBEDDED_TERMINAL_GATE_DENIED_MESSAGE, 403)
        sid, session = _terminal_session_lookup(body)
        if _terminal_remote_backend_enabled():
            return j(
                handler,
                {
                    "error": _REMOTE_TERMINAL_BACKEND_UNSUPPORTED_ERROR,
                    "message": _REMOTE_TERMINAL_BACKEND_UNSUPPORTED_MESSAGE,
                },
                status=400,
            )
        workspace = resolve_trusted_workspace(getattr(session, "workspace", "") or "")
        from api.terminal import start_terminal
        term = start_terminal(
            sid,
            workspace,
            rows=int(body.get("rows") or 24),
            cols=int(body.get("cols") or 80),
            restart=bool(body.get("restart")),
        )
        return j(
            handler,
            {
                "ok": True,
                "session_id": sid,
                "workspace": term.workspace,
                "running": term.is_alive(),
            },
        )
    except KeyError as e:
        return bad(handler, str(e), 404)
    except ValueError as e:
        return bad(handler, str(e), 400)
    except Exception as e:
        return bad(handler, _sanitize_error(e), 500)


def _handle_terminal_input(handler, body):
    try:
        if not _embedded_terminal_gate_allows(handler):
            return bad(handler, _EMBEDDED_TERMINAL_GATE_DENIED_MESSAGE, 403)
        require(body, "session_id")
        data = str(body.get("data", ""))
        if len(data) > 8192:
            return bad(handler, "input too large", 413)
        from api.terminal import write_terminal
        write_terminal(body["session_id"], data)
        return j(handler, {"ok": True})
    except KeyError as e:
        return bad(handler, str(e), 404)
    except ValueError as e:
        return bad(handler, str(e), 400)
    except Exception as e:
        return bad(handler, _sanitize_error(e), 500)


def _handle_terminal_resize(handler, body):
    try:
        if not _embedded_terminal_gate_allows(handler):
            return bad(handler, _EMBEDDED_TERMINAL_GATE_DENIED_MESSAGE, 403)
        require(body, "session_id")
        from api.terminal import resize_terminal
        resize_terminal(
            body["session_id"],
            rows=int(body.get("rows") or 24),
            cols=int(body.get("cols") or 80),
        )
        return j(handler, {"ok": True})
    except KeyError as e:
        return bad(handler, str(e), 404)
    except ValueError as e:
        return bad(handler, str(e), 400)
    except Exception as e:
        return bad(handler, _sanitize_error(e), 500)


def _handle_terminal_close(handler, body):
    try:
        if not _embedded_terminal_gate_allows(handler):
            return bad(handler, _EMBEDDED_TERMINAL_GATE_DENIED_MESSAGE, 403)
        require(body, "session_id")
        from api.terminal import close_terminal
        closed = close_terminal(body["session_id"])
        return j(handler, {"ok": True, "closed": closed})
    except ValueError as e:
        return bad(handler, str(e), 400)


def _handle_terminal_output(handler, parsed):
    if not _embedded_terminal_gate_allows(handler):
        return bad(handler, _EMBEDDED_TERMINAL_GATE_DENIED_MESSAGE, 403)
    qs = parse_qs(parsed.query)
    sid = qs.get("session_id", [""])[0]
    if not sid:
        return bad(handler, "session_id required")
    from api.terminal import get_terminal
    term = get_terminal(sid)
    if term is None:
        return j(handler, {"error": "terminal not running"}, status=404)

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("X-Accel-Buffering", "no")
    handler.send_header("Connection", "close")
    end_sse_headers(handler)
    _sse_set_write_deadline(handler)  # Defect A: slow tab can't pin this thread
    try:
        while True:
            try:
                event, data = term.output.get(timeout=_SSE_HEARTBEAT_INTERVAL_SECONDS)
            except queue.Empty:
                handler.wfile.write(b": terminal heartbeat\n\n")
                handler.wfile.flush()
                if term.closed.is_set() and term.output.empty():
                    _sse(handler, "terminal_closed", {"exit_code": term.proc.poll()})
                    break
                continue
            _sse(handler, event, data)
            if event in ("terminal_closed", "terminal_error"):
                break
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        pass
    return True


__routes_exports__ = (
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
