"""Legacy imports for the run-owned Gateway implementation.

New code should import :mod:`api.runs.gateway`.  Private exports remain only
for established compatibility tests and can be removed with those callers.
"""

# ruff: noqa: F401 -- compatibility exports are consumed by importers

from api.runs.gateway import (
    _GATEWAY_READ_TIMEOUT_DEFAULT,
    _STREAM_RUN_IDS,
    _gateway_http_error_event,
    _gateway_read_timeout_secs,
    _gateway_reasoning_delta,
    _gateway_runs_approval_event,
    _gateway_sse_delta,
    _gateway_sse_reasoning_delta,
    _gateway_stream_usage,
    _gateway_tool_progress_event,
    _gateway_use_runs_api_enabled,
    _iter_sse_lines_cancellable,
    _run_gateway_chat_streaming,
    _run_gateway_runs_api_streaming,
    gateway_chat_config_status,
    webui_chat_backend_mode,
    webui_gateway_chat_enabled,
)

__all__ = [
    "gateway_chat_config_status",
    "webui_chat_backend_mode",
    "webui_gateway_chat_enabled",
]
