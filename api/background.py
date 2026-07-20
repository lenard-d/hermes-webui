"""Compatibility alias for :mod:`api.runs.background`."""

import sys
from api.runs import background as _owner

sys.modules[__name__] = _owner
