"""Compatibility alias for :mod:`api.runs.gateway`."""

import sys
from api.runs import gateway as _owner

sys.modules[__name__] = _owner
