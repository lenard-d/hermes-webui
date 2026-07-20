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


def test_legacy_run_modules_reexport_canonical_symbols_without_aliasing_modules():
    pairs = {
        "api.runtime_state": ("api.runs.runtime_state", "RUNTIME_STATE"),
        "api.turn_admission": ("api.runs.admission", "start_local_turn"),
        "api.turn_execution": ("api.runs.execution", "TurnExecution"),
        "api.run_event_sink": ("api.runs.event_sink", "RunEventSink"),
        "api.run_journal": ("api.runs.journal", "RunJournalWriter"),
        "api.stream_channel": ("api.runs.channels", "StreamChannel"),
        "api.agent_runtime": ("api.runs.agent_runtime", "require_ai_agent_class"),
        "api.runtime_adapter": ("api.runs.adapter", "build_runtime_adapter"),
        "api.gateway_chat": ("api.runs.gateway", "gateway_chat_config_status"),
        "api.background": ("api.runs.background", "track_background"),
    }

    for legacy_name, (owner_name, symbol) in pairs.items():
        legacy = importlib.import_module(legacy_name)
        owner = importlib.import_module(owner_name)
        assert legacy is not owner
        assert legacy.__name__ == legacy_name
        assert getattr(legacy, symbol) is getattr(owner, symbol)


def test_removed_streaming_parts_compatibility_package_is_not_importable():
    assert importlib.util.find_spec("api.streaming_parts") is None


def test_legacy_runtime_state_exports_share_owner_state():
    legacy = importlib.import_module("api.runtime_state")
    owner = importlib.import_module("api.runs.runtime_state")

    for name in (
        "RUNTIME_STATE",
        "STREAMS",
        "STREAM_SESSION_OWNERS",
        "CANCEL_FLAGS",
        "AGENT_INSTANCES",
        "ACTIVE_RUNS",
    ):
        assert getattr(legacy, name) is getattr(owner, name)


def test_legacy_background_exports_share_owner_registries():
    legacy = importlib.import_module("api.background")
    owner = importlib.import_module("api.runs.background")

    assert legacy._BACKGROUND_TASKS is owner._BACKGROUND_TASKS
    assert legacy._BTW_TRACKING is owner._BTW_TRACKING


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
        REPO_ROOT / "api" / "background_process" / "process_coordination.py",
        REPO_ROOT / "api" / "background_process" / "deferred_wakeups.py",
    )

    for source_path in paths:
        source = source_path.read_text(encoding="utf-8")
        assert "from api.runs import start_session_turn" in source
        assert "from api.routes import start_session_turn" not in source


def test_runs_package_exports_supported_cross_domain_interface():
    import api.runs as runs

    assert set(runs.__all__) == {
        "LegacyJournalRuntimeAdapter",
        "LocalTurnRequest",
        "TurnExecution",
        "bound_run_journal_snapshot_args",
        "checkpoint_user_message",
        "delete_run_journal",
        "find_run_summary",
        "gateway_api_key",
        "gateway_base_url",
        "gateway_run_for_stream",
        "get_background_results",
        "latest_run_summary",
        "prepare_session_for_turn",
        "read_run_events",
        "run_agent_streaming",
        "run_journal_path",
        "runtime_adapter_enabled",
        "runtime_last_run_finished_at",
        "runtime_transport_count",
        "runtime_transport_items",
        "runtime_worker_items",
        "start_local_turn",
        "start_session_turn",
        "webui_gateway_chat_enabled",
    }
