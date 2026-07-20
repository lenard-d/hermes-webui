"""Session/workspace policy for starting an embedded terminal."""

from __future__ import annotations

from api import config, models, workspace

from .lifecycle import TerminalSession, _RUNTIME


REMOTE_BACKEND_UNSUPPORTED_ERROR = "remote_terminal_backend_unsupported"
REMOTE_BACKEND_UNSUPPORTED_MESSAGE = (
    "Embedded terminal is only supported for local terminal backends."
)


class RemoteTerminalBackendUnsupported(ValueError):
    code = REMOTE_BACKEND_UNSUPPORTED_ERROR

    def __init__(self) -> None:
        super().__init__(REMOTE_BACKEND_UNSUPPORTED_MESSAGE)


def normalize_session_id(value) -> str:
    session_id = str(value or "").strip()
    if not session_id:
        raise ValueError("session_id required")
    return session_id


def lookup_terminal_session(session_id):
    session_id = normalize_session_id(session_id)
    try:
        session = models.get_session(session_id)
    except KeyError:
        raise KeyError("Session not found") from None
    return session_id, session


def terminal_remote_backend_enabled() -> bool:
    terminal_config = config.get_config().get("terminal", {})
    return workspace.is_remote_terminal_backend(terminal_config)


def start_terminal_for_session(
    session_id,
    *,
    rows: int = 24,
    cols: int = 80,
    restart: bool = False,
) -> TerminalSession:
    """Resolve one authoritative local workspace and start its session terminal."""
    session_id, session = lookup_terminal_session(session_id)
    if terminal_remote_backend_enabled():
        raise RemoteTerminalBackendUnsupported
    trusted_workspace = workspace.resolve_trusted_workspace(
        getattr(session, "workspace", "") or ""
    )
    return _RUNTIME.start(
        session_id,
        trusted_workspace,
        rows=rows,
        cols=cols,
        restart=restart,
    )
