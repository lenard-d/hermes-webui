"""Live stream transport and control interface.

Run execution, transcript shaping, tool events, provider handling, and title
generation are owned by :mod:`api.runs`.  The private aliases below are a
temporary compatibility adapter for the unchanged import block in
``api.routes``.  Remove the run-owned aliases as soon as that block imports
``api.runs`` directly; no other caller should extend this adapter.
"""

from api.runs import run_agent_streaming as _run_agent_streaming  # noqa: F401
from api.runs.payloads import (  # noqa: F401
    _compact_for_echo_compare,
    _strip_compact_echo_suffix,
)
from api.runs.title_generation import (  # noqa: F401
    generate_session_title_for_session,
)
from api.runs.transcript import (  # noqa: F401
    _materialize_pending_user_turn_before_error,
)

from .live_controls import cancel_stream
from .transport import _sse, _sse_set_write_deadline  # noqa: F401

__all__ = (
    "cancel_stream",
)
