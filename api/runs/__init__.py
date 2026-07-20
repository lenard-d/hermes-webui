"""Run-domain ownership for admitted browser turns.

The package keeps live execution state, admission, journaling, publication,
and cleanup behind one domain boundary.  HTTP routing and durable session
storage remain owned by their respective modules.
"""

from importlib import import_module
from typing import Any


_PUBLIC = {
    "LocalTurnRequest": (".admission", "LocalTurnRequest"),
    "TurnExecution": (".execution", "TurnExecution"),
    "checkpoint_user_message": (".admission", "checkpoint_user_message"),
    "prepare_session_for_turn": (".admission", "prepare_session_for_turn"),
    "start_local_turn": (".admission", "start_local_turn"),
    "start_session_turn": (".server_turn", "start_session_turn"),
}
__all__ = list(_PUBLIC)


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _PUBLIC[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value
