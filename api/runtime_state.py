"""Compatibility alias for :mod:`api.runs.runtime_state`."""

import sys
from api.runs import runtime_state as _owner

sys.modules[__name__] = _owner
