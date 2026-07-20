"""Temporary compatibility context for the incremental HTTP-router migration.

The old ``api.routes`` module is still a widely patched compatibility seam.
Route groups receive its current namespace explicitly per request so existing
callers keep working while implementations move without code-object rebinding
or reverse imports into that facade.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any, Final, TypeAlias


RouteContext: TypeAlias = MutableMapping[str, Any]
UNHANDLED: Final = object()
