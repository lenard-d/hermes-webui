"""Sidecar metadata overlay for state projections.

Loaded behind :mod:`api.models`; use that compatibility facade in callers.
"""
# The facade seeds the original monolith namespace dynamically.  Pyflakes cannot
# see those names, while the copied implementation intentionally keeps its old
# global lookups so api.models monkeypatch seams remain effective.
# ruff: noqa: F401, F811, F821, F841, B007, B023, B904, B905
from api.models_parts._compat import seed_module_globals

seed_module_globals(globals())

def clear_sidecar_metadata_cache() -> None:
    """Drop all memoized sidebar-projection sidecar metadata (test/lifecycle hook)."""
    with _SIDECAR_METADATA_CACHE_LOCK:
        _SIDECAR_METADATA_CACHE.clear()


def _state_projection_sidecar_metadata(sid: str) -> dict:
    """Return UI-owned metadata (title + archived) for a state.db-projected row.

    Memoized by the sidecar file's (path, mtime_ns, size, ctime_ns) stat
    signature so the sidebar projection — which calls this once per row in both
    the visible pass and the up-to-200-row cron pass — pays a single os.stat per
    file on a warm build instead of an open() + 64KB read + JSON-key scan
    (#4842). A rename/archive/edit bumps the signature and invalidates just that
    entry, so a stale title/archived flag is impossible without re-reading.
    Returns a COPY so callers can't mutate the cached dict.

    NOTE: this stat-gates on ``SESSION_DIR / f'{sid}.json'`` because that file is
    ``Session.load_metadata_only``'s sole source for title+archived. If that ever
    stops being true (metadata moves to another store), this gate would short-
    circuit before the real source — update both together.
    """
    default = {"title": None, "archived": False}
    if not is_safe_session_id(sid):
        return dict(default)
    p = SESSION_DIR / f'{sid}.json'
    try:
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size, st.st_ctime_ns)
    except OSError:
        # No sidecar file (the common case for a pure state.db row) or it
        # vanished mid-build — nothing to project, and nothing worth caching.
        return dict(default)

    with _SIDECAR_METADATA_CACHE_LOCK:
        hit = _SIDECAR_METADATA_CACHE.get(key)
        if hit is not None:
            _SIDECAR_METADATA_CACHE.move_to_end(key)
            return dict(hit)

    metadata = dict(default)
    try:
        webui_meta = Session.load_metadata_only(sid)
    except Exception:
        webui_meta = None
    if webui_meta:
        title = getattr(webui_meta, 'title', None)
        if title:
            metadata["title"] = title
        metadata["archived"] = bool(getattr(webui_meta, 'archived', False))

    with _SIDECAR_METADATA_CACHE_LOCK:
        # Re-check under lock in case a concurrent build populated it; either
        # entry is equally valid for the same stat signature.
        if key not in _SIDECAR_METADATA_CACHE:
            _SIDECAR_METADATA_CACHE[key] = metadata
            _SIDECAR_METADATA_CACHE.move_to_end(key)
            while len(_SIDECAR_METADATA_CACHE) > _SIDECAR_METADATA_CACHE_MAX:
                _SIDECAR_METADATA_CACHE.popitem(last=False)
    return dict(metadata)

__all__ = ['clear_sidecar_metadata_cache', '_state_projection_sidecar_metadata']
