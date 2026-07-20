"""Compatibility alias for :mod:`api.runs.agent_runtime`."""

import sys
from api.runs import agent_runtime as _owner

sys.modules[__name__] = _owner
