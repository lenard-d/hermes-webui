"""Late-bound access to the :mod:`api.profiles` compatibility facade."""

from collections.abc import Callable
from contextvars import ContextVar
from functools import wraps
from types import ModuleType


_profiles_api_resolver: Callable[[], ModuleType] | None = None
_active_profiles_api: ContextVar[ModuleType | None] = ContextVar(
    "active_profiles_api", default=None
)


def bind_profiles_api(resolver: Callable[[], ModuleType]) -> None:
    """Bind implementation modules to the live compatibility facade."""
    global _profiles_api_resolver
    _profiles_api_resolver = resolver


def profiles_api() -> ModuleType:
    """Return the facade so historical monkeypatch seams remain observable."""
    active = _active_profiles_api.get()
    if active is not None:
        return active
    if _profiles_api_resolver is None:
        raise RuntimeError("profile parts are not bound to the profiles facade")
    return _profiles_api_resolver()


def bind_profile_function(facade: ModuleType, implementation: Callable) -> Callable:
    """Bind one implementation call to the facade instance exporting it.

    Tests intentionally import fresh ``api.profiles`` module objects and later
    restore an older object in ``sys.modules``.  A process-global resolver alone
    cannot distinguish those instances.  The wrapper supplies the exporting
    facade through a context-local for the duration of each call, including
    nested calls through other facade functions.
    """

    @wraps(implementation)
    def bound(*args, **kwargs):
        token = _active_profiles_api.set(facade)
        try:
            return implementation(*args, **kwargs)
        finally:
            _active_profiles_api.reset(token)

    return bound
