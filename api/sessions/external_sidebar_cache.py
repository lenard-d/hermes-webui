"""Bounded cache and rebuild coordination for external sidebar rows."""

from __future__ import annotations

import collections
import copy
import logging
import threading
import time
from .external_sidebar_projection import clear_sidecar_metadata_cache

logger = logging.getLogger(__name__)

_CLI_SESSIONS_CACHE_TTL_SECONDS = 5.0
_CLI_SESSIONS_CACHE_STREAMING_TTL_SECONDS = 45.0
_CLI_SESSIONS_CACHE_LOCK = threading.Lock()
_CLI_SESSIONS_CACHE_INFLIGHT: dict[tuple, threading.Event] = {}
_CLI_SESSIONS_CACHE_INVALIDATION_VERSION = 0
_CLI_SESSIONS_CACHE: collections.OrderedDict[tuple, tuple] = collections.OrderedDict()
_CLI_SESSIONS_CACHE_MAX_ENTRIES = 8
_CLI_SESSIONS_CACHE_WAIT_SECONDS = 0.25
_CLI_SESSIONS_CACHE_STALE_WAIT_SECONDS = 0.10


def clear_cli_sessions_cache() -> None:
    with _CLI_SESSIONS_CACHE_LOCK:
        global _CLI_SESSIONS_CACHE_INVALIDATION_VERSION
        _CLI_SESSIONS_CACHE_INVALIDATION_VERSION += 1
        _CLI_SESSIONS_CACHE.clear()
    # The sidecar-metadata projection cache is stat-keyed (self-invalidating on
    # any file change), but clear it alongside the CLI cache so an explicit
    # reset — a mutating sidebar action or test isolation — starts fully cold.
    clear_sidecar_metadata_cache()


def _copy_cli_sessions(sessions: list) -> list:
    return copy.deepcopy(sessions)


def _cli_sessions_cache_invalidation_stamp() -> int:
    with _CLI_SESSIONS_CACHE_LOCK:
        return int(_CLI_SESSIONS_CACHE_INVALIDATION_VERSION)


def _cli_sessions_cache_claim_rebuild(cache_key: tuple) -> tuple[threading.Event, bool]:
    with _CLI_SESSIONS_CACHE_LOCK:
        current = _CLI_SESSIONS_CACHE_INFLIGHT.get(cache_key)
        if current is not None:
            return current, False
        event = threading.Event()
        _CLI_SESSIONS_CACHE_INFLIGHT[cache_key] = event
        return event, True


def _cli_sessions_cache_done(cache_key: tuple, event: threading.Event | None) -> None:
    with _CLI_SESSIONS_CACHE_LOCK:
        if event is None:
            return
        if _CLI_SESSIONS_CACHE_INFLIGHT.get(cache_key) is event:
            _CLI_SESSIONS_CACHE_INFLIGHT.pop(cache_key, None)
    if event is not None:
        event.set()


def _cache_cli_sessions_if_current(
    cache_key: tuple,
    ttl: float,
    invalidation_stamp: int,
    sessions: list,
) -> bool:
    with _CLI_SESSIONS_CACHE_LOCK:
        if _CLI_SESSIONS_CACHE_INVALIDATION_VERSION != invalidation_stamp:
            return False
        _CLI_SESSIONS_CACHE[cache_key] = (
            time.monotonic() + ttl,
            invalidation_stamp,
            _copy_cli_sessions(sessions),
        )
        _CLI_SESSIONS_CACHE.move_to_end(cache_key)
        while len(_CLI_SESSIONS_CACHE) > _CLI_SESSIONS_CACHE_MAX_ENTRIES:
            _CLI_SESSIONS_CACHE.popitem(last=False)
    return True


def _copy_fresh_cli_sessions_cache_entry(cache_key: tuple):
    with _CLI_SESSIONS_CACHE_LOCK:
        cached_entry = _CLI_SESSIONS_CACHE.get(cache_key)
        if cached_entry is None:
            return None
        if len(cached_entry) == 3:
            cached_expires_at, cached_stamp, cached_sessions = cached_entry
        else:
            cached_expires_at, cached_sessions = cached_entry
            cached_stamp = _CLI_SESSIONS_CACHE_INVALIDATION_VERSION
        if cached_stamp != _CLI_SESSIONS_CACHE_INVALIDATION_VERSION:
            _CLI_SESSIONS_CACHE.pop(cache_key, None)
            return None
        if cached_expires_at <= time.monotonic():
            return None
        # LRU: a fresh hit is the most-recently-used entry.
        _CLI_SESSIONS_CACHE.move_to_end(cache_key)
        return _copy_cli_sessions(cached_sessions)


def _load_and_cache_cli_sessions(
    *,
    cache_key: tuple,
    ttl: float,
    invalidation_stamp: int,
    load_sessions,
    stale_sessions,
    stale_stamp,
    all_profiles: bool,
    db_path,
) -> list:
    try:
        sessions = load_sessions()
    except Exception as _cli_err:
        logger.warning(
            "get_cli_sessions() failed — check state.db schema or path (%s): %s",
            "all profiles" if all_profiles else db_path, _cli_err,
        )
        if stale_sessions is not None and stale_stamp == _cli_sessions_cache_invalidation_stamp():
            return stale_sessions
        return []
    _cache_cli_sessions_if_current(
        cache_key,
        ttl,
        invalidation_stamp,
        sessions,
    )
    return _copy_cli_sessions(sessions)


def _reload_cli_sessions_after_inflight(
    *,
    cache_key: tuple,
    ttl: float,
    stale_sessions,
    stale_stamp,
    load_sessions,
    all_profiles: bool,
    db_path: str,
) -> list:
    while True:
        event, is_owner = _cli_sessions_cache_claim_rebuild(cache_key)
        if is_owner:
            break
        wait_finished = False
        try:
            wait_finished = bool(
                event.wait(
                    _CLI_SESSIONS_CACHE_STALE_WAIT_SECONDS
                    if stale_sessions is not None
                    else _CLI_SESSIONS_CACHE_WAIT_SECONDS
                )
            )
        except Exception:
            pass
        cached_sessions = _copy_fresh_cli_sessions_cache_entry(cache_key)
        if cached_sessions is not None:
            return cached_sessions
        if stale_sessions is not None and stale_stamp == _cli_sessions_cache_invalidation_stamp():
            return stale_sessions
        if not wait_finished:
            fallback_invalidation_stamp = _cli_sessions_cache_invalidation_stamp()
            return _load_and_cache_cli_sessions(
                cache_key=cache_key,
                ttl=ttl,
                invalidation_stamp=fallback_invalidation_stamp,
                load_sessions=load_sessions,
                stale_sessions=stale_sessions,
                stale_stamp=stale_stamp,
                all_profiles=all_profiles,
                db_path=db_path,
            )
    try:
        invalidation_stamp = _cli_sessions_cache_invalidation_stamp()
        return _load_and_cache_cli_sessions(
            cache_key=cache_key,
            ttl=ttl,
            invalidation_stamp=invalidation_stamp,
            load_sessions=load_sessions,
            stale_sessions=stale_sessions,
            stale_stamp=stale_stamp,
            all_profiles=all_profiles,
            db_path=db_path,
        )
    finally:
        _cli_sessions_cache_done(cache_key, event)
