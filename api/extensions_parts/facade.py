"""Late-bound access to the :mod:`api.extensions` compatibility facade."""

from collections.abc import Callable
from types import ModuleType


_extensions_api_resolver: Callable[[], ModuleType] | None = None


def bind_extensions_api(resolver: Callable[[], ModuleType]) -> None:
    """Bind the facade without making extension parts import it circularly."""
    global _extensions_api_resolver
    _extensions_api_resolver = resolver


def extensions_api() -> ModuleType:
    """Return the live facade so historical monkeypatches stay observable."""
    if _extensions_api_resolver is None:
        raise RuntimeError("extension parts are not bound to the extensions facade")
    return _extensions_api_resolver()
