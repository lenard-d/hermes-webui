"""Compatibility alias for :mod:`api.runs.admission`."""

import sys
from api.runs import admission as _owner

sys.modules[__name__] = _owner
