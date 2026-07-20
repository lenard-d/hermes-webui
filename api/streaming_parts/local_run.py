"""Legacy import for local run orchestration.

The implementation and all mutable runtime ownership live in
:mod:`api.runs.local` and the sibling run-domain owners.
"""

from api.runs import run_agent_streaming

__all__ = ["run_agent_streaming"]
