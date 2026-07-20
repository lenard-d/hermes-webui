"""Compatibility alias for :mod:`api.runs.channels`."""

import sys
from api.runs import channels as _owner

sys.modules[__name__] = _owner
