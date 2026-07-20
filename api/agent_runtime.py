"""Legacy imports for the run-owned agent runtime guard.

New code should import :mod:`api.runs.agent_runtime`.  This module deliberately
re-exports only the supported compatibility surface and owns no runtime state.
"""

from api.runs.agent_runtime import (
    AgentRuntimeChangedError,
    ensure_agent_runtime_current,
    get_ai_agent_class,
    require_ai_agent_class,
)

__all__ = [
    "AgentRuntimeChangedError",
    "ensure_agent_runtime_current",
    "get_ai_agent_class",
    "require_ai_agent_class",
]
