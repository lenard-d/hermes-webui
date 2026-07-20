"""Compatibility alias for :mod:`api.runs.journal`."""

import sys
from api.runs import journal as _owner

sys.modules[__name__] = _owner
