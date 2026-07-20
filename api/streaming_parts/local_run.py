"""Compatibility alias for :mod:`api.runs.local`."""

import sys
from api.runs import local as _owner

sys.modules[__name__] = _owner
