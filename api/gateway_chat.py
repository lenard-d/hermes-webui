"""Legacy imports for the run-owned Gateway implementation.

New code should import :mod:`api.runs.gateway`.  Private exports remain only
for established compatibility tests and can be removed with those callers.
"""

# ruff: noqa: F401 -- compatibility exports are consumed by importers

from api.runs.gateway import _run_gateway_chat_streaming
from api.runs.gateway_config import (
    gateway_chat_config_status,
    gateway_http_error_event as _gateway_http_error_event,
    gateway_use_runs_api_enabled as _gateway_use_runs_api_enabled,
    webui_chat_backend_mode,
    webui_gateway_chat_enabled,
)
from api.runs.gateway_events import (
    gateway_reasoning_delta as _gateway_reasoning_delta,
    gateway_runs_approval_event as _gateway_runs_approval_event,
    gateway_sse_delta as _gateway_sse_delta,
    gateway_sse_reasoning_delta as _gateway_sse_reasoning_delta,
    gateway_stream_usage as _gateway_stream_usage,
    gateway_tool_progress_event as _gateway_tool_progress_event,
)
from api.runs.gateway_runtime import _STREAM_RUN_IDS
from api.runs.gateway_transport import (
    _GATEWAY_READ_TIMEOUT_DEFAULT,
    gateway_read_timeout_secs as _gateway_read_timeout_secs,
    iter_sse_lines_cancellable as _iter_sse_lines_cancellable,
    stream_gateway_runs_api as _run_gateway_runs_api_streaming,
)

__all__ = [
    "gateway_chat_config_status",
    "webui_chat_backend_mode",
    "webui_gateway_chat_enabled",
]
