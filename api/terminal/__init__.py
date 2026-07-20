"""Embedded-terminal domain interface.

HTTP and SSE adapters should use only this module.  Session/process state and
its complete lifecycle remain private to the package implementation.
"""

from .application import (
    REMOTE_BACKEND_UNSUPPORTED_ERROR,
    REMOTE_BACKEND_UNSUPPORTED_MESSAGE,
    RemoteTerminalBackendUnsupported,
    lookup_terminal_session,
    start_terminal_for_session,
    terminal_remote_backend_enabled,
)
from .lifecycle import (
    TerminalEvent,
    TerminalOutputSubscription,
    TerminalSession,
    _RUNTIME,
)


start_terminal = _RUNTIME.start
get_terminal = _RUNTIME.get
attach_terminal_output = _RUNTIME.attach_output
write_terminal = _RUNTIME.write
resize_terminal = _RUNTIME.resize
close_terminal = _RUNTIME.close
close_all_terminals = _RUNTIME.close_all


__all__ = [
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
]
