"""SSE framing and connection write-deadline policy."""

from __future__ import annotations

import json
import logging
import os


logger = logging.getLogger(__name__)

# Keep the application heartbeat comfortably below the 25-second kernel
# keepalive failure window configured by server.py.
SSE_HEARTBEAT_INTERVAL_SECONDS = 5
_SSE_HEARTBEAT_INTERVAL_SECONDS = SSE_HEARTBEAT_INTERVAL_SECONDS

try:
    _raw_deadline = os.getenv("HERMES_WEBUI_SSE_WRITE_DEADLINE") or os.getenv("HERMES_SSE_WRITE_DEADLINE")
    SSE_WRITE_DEADLINE_SECONDS = float(_raw_deadline or "20.0")
except (TypeError, ValueError):
    SSE_WRITE_DEADLINE_SECONDS = 20.0
if SSE_WRITE_DEADLINE_SECONDS <= 0:
    SSE_WRITE_DEADLINE_SECONDS = 20.0


def _sse(handler, event, data):
    """Write one SSE event to the response stream."""
    payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    handler.wfile.write(payload.encode('utf-8'))
    handler.wfile.flush()

def _sse_set_write_deadline(handler, seconds=None):
    """Best-effort: arm a socket write deadline on an SSE handler.

    Call once, right after end_headers(), in every long-lived SSE endpoint.
    Never raises — an unusual/missing transport just keeps the pre-fix
    (no-deadline) behaviour for that single connection rather than breaking
    the stream setup.
    """
    if seconds is None:
        seconds = SSE_WRITE_DEADLINE_SECONDS
    try:
        conn = getattr(handler, "connection", None)
        if conn is not None and hasattr(conn, "settimeout"):
            conn.settimeout(seconds)
    except Exception:
        logger.debug("Failed to arm SSE write deadline", exc_info=True)
