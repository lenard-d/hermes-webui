"""Run-domain ownership for admitted browser turns.

The package keeps live execution state, admission, journaling, publication,
and cleanup behind one domain boundary.  HTTP routing and durable session
storage remain owned by their respective modules.
"""

from importlib import import_module
from typing import Any


_PUBLIC = {
    "bound_run_journal_snapshot_args": (
        ".journal",
        "bound_run_journal_snapshot_args",
    ),
    "LegacyJournalRuntimeAdapter": (".adapter", "LegacyJournalRuntimeAdapter"),
    "LocalTurnRequest": (".admission", "LocalTurnRequest"),
    "TurnExecution": (".execution", "TurnExecution"),
    "checkpoint_user_message": (".admission", "checkpoint_user_message"),
    "delete_run_journal": (".journal", "delete_run_journal"),
    "find_run_summary": (".journal", "find_run_summary"),
    "get_background_results": (".background", "get_results"),
    "latest_run_summary": (".journal", "latest_run_summary"),
    "prepare_session_for_turn": (".admission", "prepare_session_for_turn"),
    "read_run_events": (".journal", "read_run_events"),
    "run_agent_streaming": (".local_entrypoint", "run_agent_streaming"),
    "run_journal_path": (".journal", "run_journal_path"),
    "runtime_adapter_enabled": (".adapter", "runtime_adapter_enabled"),
    "runtime_last_run_finished_at": (
        ".runtime_state",
        "runtime_last_run_finished_at",
    ),
    "runtime_transport_count": (".runtime_state", "runtime_transport_count"),
    "runtime_transport_items": (".runtime_state", "runtime_transport_items"),
    "runtime_worker_items": (".runtime_state", "runtime_worker_items"),
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
