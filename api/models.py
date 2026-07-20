"""Legacy import seam for the session package.

New code imports :mod:`api.sessions` or one of its cohesive modules directly.
This adapter deliberately owns no state and performs no module rebinding.
"""

from __future__ import annotations

from api.sessions import store as _store


def __getattr__(name: str):
    try:
        return getattr(_store, name)
    except AttributeError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc


def __dir__() -> list[str]:
    return sorted(set(globals()) | {name for name in dir(_store) if not name.startswith("__")})


# Keep the common historical imports visible to static tools without copying
# mutable stores or caches into this compatibility module.
Session = _store.Session
get_session = _store.get_session
new_session = _store.new_session
all_sessions = _store.all_sessions
is_safe_session_id = _store.is_safe_session_id

# Preserve wildcard imports at this outer compatibility seam without copying
# any of the store's mutable state into this module.
__all__ = [name for name in dir(_store) if not name.startswith("__")]
