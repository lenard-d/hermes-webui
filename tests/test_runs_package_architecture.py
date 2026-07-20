"""Architecture guards for the run-domain package boundary."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RUNS_ROOT = REPO_ROOT / "api" / "runs"


def test_config_runtime_aliases_share_the_run_owner_objects():
    from api import config
    from api.runs import runtime_state

    assert config.RUNTIME_STATE is runtime_state.RUNTIME_STATE
    assert config.STREAMS is runtime_state.STREAMS
    assert config.STREAM_SESSION_OWNERS is runtime_state.STREAM_SESSION_OWNERS
    assert config.ACTIVE_RUNS is runtime_state.ACTIVE_RUNS


def test_legacy_run_modules_resolve_to_their_canonical_owners():
    pairs = {
        "api.runtime_state": "api.runs.runtime_state",
        "api.turn_admission": "api.runs.admission",
        "api.turn_execution": "api.runs.execution",
        "api.run_event_sink": "api.runs.event_sink",
        "api.run_journal": "api.runs.journal",
        "api.stream_channel": "api.runs.channels",
        "api.agent_runtime": "api.runs.agent_runtime",
        "api.runtime_adapter": "api.runs.adapter",
        "api.gateway_chat": "api.runs.gateway",
        "api.background": "api.runs.background",
        "api.streaming_parts.local_run": "api.runs.local",
    }

    for legacy_name, owner_name in pairs.items():
        assert importlib.import_module(legacy_name) is importlib.import_module(owner_name)


def test_run_domain_does_not_import_http_route_modules():
    forbidden = ("api.routes", "api.routes_parts")

    for source_path in RUNS_ROOT.glob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            imported: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                imported = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = (node.module,)
            assert not any(
                name == blocked or name.startswith(f"{blocked}.")
                for name in imported
                for blocked in forbidden
            ), f"{source_path.name} imports HTTP ownership: {imported}"


def test_background_turn_starters_depend_on_the_run_interface():
    paths = (
        REPO_ROOT / "api" / "background_process.py",
        REPO_ROOT / "api" / "background_process_parts" / "deferred_wakeups.py",
    )

    for source_path in paths:
        source = source_path.read_text(encoding="utf-8")
        assert "from api.runs import start_session_turn" in source
        assert "from api.routes import start_session_turn" not in source


def test_runs_package_exports_only_high_level_turn_entry_points():
    import api.runs as runs

    assert set(runs.__all__) == {
        "LocalTurnRequest",
        "TurnExecution",
        "checkpoint_user_message",
        "prepare_session_for_turn",
        "start_local_turn",
        "start_session_turn",
    }
