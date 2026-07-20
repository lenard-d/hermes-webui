"""Compatibility alias for :mod:`api.runs.event_sink`."""

import sys
from api.runs import event_sink as _owner

sys.modules[__name__] = _owner
