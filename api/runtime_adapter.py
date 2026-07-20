"""Compatibility alias for :mod:`api.runs.adapter`."""

import sys
from api.runs import adapter as _owner

sys.modules[__name__] = _owner
