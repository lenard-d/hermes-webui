"""Late binding to the public ``api.streaming`` compatibility facade."""

from __future__ import annotations

import sys
from types import ModuleType


def streaming_api() -> ModuleType:
    """Return the canonical facade without importing it from a part module.

    Resolving through ``sys.modules`` avoids an import cycle and, importantly,
    ensures implementations observe monkeypatches applied to ``api.streaming``.
    The facade calls this only after its own module object is registered.
    """
    module = sys.modules.get("api.streaming")
    if module is None:  # pragma: no cover - a facade call cannot reach this
        raise RuntimeError("api.streaming is not loaded")
    return module
