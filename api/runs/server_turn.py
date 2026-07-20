"""Public run-domain entry point for autonomous server-side turns.

The HTTP assembly layer installs the concrete orchestration callback during
startup.  Background domains call this module and therefore never depend on
the route facade; the callback remains late-bound so compatibility monkeypatch
seams and route-module reloads continue to work during the migration.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any


ServerTurnStarter = Callable[..., dict[str, Any]]

_STARTER_LOCK = threading.Lock()
_starter: ServerTurnStarter | None = None


def configure_start_session_turn(starter: ServerTurnStarter) -> None:
    """Install the application-composed server-turn orchestration callback."""
    if not callable(starter):
        raise TypeError("starter must be callable")
    global _starter
    with _STARTER_LOCK:
        _starter = starter


def start_session_turn(
    session_id: str,
    message: str,
    *,
    source: str = "process_wakeup",
) -> dict[str, Any]:
    """Start one autonomous turn through the configured run orchestration."""
    with _STARTER_LOCK:
        starter = _starter
    if starter is None:
        raise RuntimeError("server-turn orchestration is not configured")
    return starter(session_id, message, source=source)
