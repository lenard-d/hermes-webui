"""Compatibility alias for :mod:`api.runs.execution`."""

import sys
from api.runs import execution as _owner

sys.modules[__name__] = _owner
