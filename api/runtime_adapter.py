"""Legacy imports for the run runtime-adapter seam.

New code should import :mod:`api.runs.adapter`.
"""

from api.runs.adapter import (
    ControlResult,
    LegacyJournalRuntimeAdapter,
    RunnerRuntimeAdapter,
    RunEventStream,
    RunStartResult,
    RunStatus,
    RuntimeAdapter,
    StartRunRequest,
    build_runtime_adapter,
    runtime_adapter_enabled,
    runtime_adapter_mode,
    runtime_adapter_runner_enabled,
)

__all__ = [
    "ControlResult",
    "LegacyJournalRuntimeAdapter",
    "RunnerRuntimeAdapter",
    "RunEventStream",
    "RunStartResult",
    "RunStatus",
    "RuntimeAdapter",
    "StartRunRequest",
    "build_runtime_adapter",
    "runtime_adapter_enabled",
    "runtime_adapter_mode",
    "runtime_adapter_runner_enabled",
]
