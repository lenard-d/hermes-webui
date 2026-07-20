"""Process-local correlation state for Gateway-backed runs.

The Gateway owns execution, while WebUI owns the browser stream.  This module
keeps the one process-local correlation required to relay an approval response
from a WebUI ``stream_id`` to the corresponding Gateway ``run_id``.
"""

from __future__ import annotations

import threading


_STREAM_RUN_IDS: dict[str, str] = {}
_STREAM_RUN_IDS_LOCK = threading.Lock()


def bind_gateway_run(stream_id: str, run_id: str) -> None:
    """Bind a local browser stream to its authoritative Gateway run."""
    normalized_stream_id = str(stream_id or "").strip()
    normalized_run_id = str(run_id or "").strip()
    if not normalized_stream_id or not normalized_run_id:
        return
    with _STREAM_RUN_IDS_LOCK:
        _STREAM_RUN_IDS[normalized_stream_id] = normalized_run_id


def gateway_run_for_stream(stream_id: str) -> str | None:
    """Return the Gateway run currently correlated with ``stream_id``."""
    with _STREAM_RUN_IDS_LOCK:
        return _STREAM_RUN_IDS.get(str(stream_id or ""))


def release_gateway_run(stream_id: str) -> str | None:
    """Release and return a stream's Gateway run correlation."""
    with _STREAM_RUN_IDS_LOCK:
        return _STREAM_RUN_IDS.pop(str(stream_id or ""), None)


def clear_gateway_run_correlations() -> None:
    """Clear correlations for isolated tests and process teardown."""
    with _STREAM_RUN_IDS_LOCK:
        _STREAM_RUN_IDS.clear()
