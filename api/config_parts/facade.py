"""Shared late-bound access to the :mod:`api.config` compatibility facade."""

from collections.abc import Callable
from types import ModuleType


_config_api_resolver: Callable[[], ModuleType] | None = None


def bind_config_api(resolver: Callable[[], ModuleType]) -> None:
    """Bind the facade without making config parts import it circularly."""
    global _config_api_resolver
    _config_api_resolver = resolver


def config_api() -> ModuleType:
    """Return the active facade so patches and reloads remain observable."""
    if _config_api_resolver is None:
        raise RuntimeError("config parts are not bound to the config facade")
    return _config_api_resolver()
