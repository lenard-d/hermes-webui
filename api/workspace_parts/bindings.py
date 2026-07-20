"""Late binding to the public :mod:`api.workspace` compatibility facade."""

from __future__ import annotations

import sys
from types import ModuleType


def workspace_api() -> ModuleType:
    """Return the canonical facade so historical monkeypatch seams stay live."""
    module = sys.modules.get("api.workspace")
    if module is None:  # pragma: no cover - facade calls cannot reach this
        raise RuntimeError("api.workspace is not loaded")
    return module
