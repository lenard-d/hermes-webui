"""Imports, shared stores, cache objects, and path safety.

Loaded behind :mod:`api.models`; use that compatibility facade in callers.
"""
# The facade seeds the original monolith namespace dynamically.  Pyflakes cannot
# see those names, while the copied implementation intentionally keeps its old
# global lookups so api.models monkeypatch seams remain effective.
# ruff: noqa: F401, F811, F821, F841, B007, B023, B904, B905
from api.models_parts._compat import seed_module_globals

seed_module_globals(globals())

"""Hermes Web UI -- Session model and in-memory session store."""
import collections
import copy
import datetime
import hashlib
import inspect
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

try:  # pragma: no cover - platform-specific imports.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover
    _fcntl = None

try:  # pragma: no cover - platform-specific imports.
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover
    _msvcrt = None

import api.config as _cfg
from api.compression_anchor import is_context_compression_marker
from api.config import (
    SESSION_DIR, SESSION_INDEX_FILE, SESSIONS, SESSIONS_MAX,
    LOCK, DEFAULT_WORKSPACE, DEFAULT_MODEL, PROJECTS_FILE, HOME,
    get_effective_default_model, _get_session_agent_lock,
)
from api.workspace import get_last_workspace
from api.usage import prompt_cache_hit_percent
from api.agent_sessions import (
    _is_continuation_session,
    is_cli_session_row,
    normalize_agent_session_source,
    open_state_db_readonly,
    read_importable_agent_session_rows,
    read_session_lineage_metadata,
)
from api.session_sources import import_source_metadata

logger = logging.getLogger("api.models")
CLI_VISIBLE_SESSION_LIMIT = 20
# How many messageful cron sessions to surface in the project-chip layer.
# Needs to exceed CLI_VISIBLE_SESSION_LIMIT so older cron runs stay
# addressable even when many newer non-cron sessions dominate the default
# sidebar window (#3172).
CRON_PROJECT_CHIP_LIMIT = 200
WEBHOOK_PROJECT_CHIP_LIMIT = 200
_CLI_SESSIONS_CACHE_TTL_SECONDS = 5.0
# While a turn is actively streaming, hold the CLI/cron projection longer than
# one poll interval (mirrors the route-level #4808 hold-down). The frontend
# polls /api/sessions on the static/sessions.js `_streamingPollMs` cadence.
# Pair this wider window with the stable streaming cache key below so repeated
# polls reuse the projection instead of re-running the expensive state.db
# CLI/cron projection. (#4842) Keep this strictly greater than
# `_streamingPollMs`/1000 (see tests/test_streaming_cache_ttl_vs_poll.py).
_CLI_SESSIONS_CACHE_STREAMING_TTL_SECONDS = 45.0
_CLI_SESSIONS_CACHE_LOCK = threading.Lock()
_CLI_SESSIONS_CACHE_INFLIGHT: "dict[tuple, threading.Event]" = {}
_CLI_SESSIONS_CACHE_INVALIDATION_VERSION = 0
# LRU-bounded (drop-oldest) so a long-lived process under churn — where the
# state.db fingerprint advances on every streamed message and the structural
# clear-on-mutation listener doesn't fire for every fingerprint advance — can't
# accumulate orphaned heavy deepcopies. Each value is a copy.deepcopy() of the
# full CLI/cron session list (the expensive projection behind #4842/#4672), so
# the cap is deliberately small. TTL is still the primary freshness control;
# the cap is the backstop that the plain dict previously lacked. Mirrors the
# _CLAUDE_CODE_PARSE_CACHE / _SIDECAR_METADATA_CACHE LRU pattern.
_CLI_SESSIONS_CACHE: "collections.OrderedDict[tuple, tuple]" = collections.OrderedDict()
_CLI_SESSIONS_CACHE_MAX_ENTRIES = 8
_CLI_SESSIONS_CACHE_WAIT_SECONDS = 0.25
# Event waits that keep stale rows visible while a rebuild is in flight.
_CLI_SESSIONS_CACHE_STALE_WAIT_SECONDS = 0.10

# Per-file parse cache for Claude Code JSONL transcripts (#4718/#4662 phase 4).
# ``~/.claude/projects`` is a GLOBAL, profile-independent directory, but the
# sidebar re-derives every Claude Code row from scratch on each /api/sessions
# build — fully re-reading and JSON-parsing up to CLAUDE_CODE_MAX_FILES
# transcripts line-by-line (hundreds of MB) just to recover a title + message
# count. That parse dominates the cold sidebar build (~650-1000ms measured on a
# 200-file / ~130MB tree) and it repeats on every profile switch, on the 5s
# CLI-cache expiry, and on every sidebar poll, because the higher CLI cache is
# keyed per active profile while the underlying transcripts never change between
# switches. This cache memoizes the EXPENSIVE per-file parse result keyed by the
# file's (path, mtime_ns, size, ctime_ns); a warm sidebar build then re-stats the
# files (~4ms for 200) instead of re-parsing them. Any external edit/append to a
# transcript changes mtime_ns/size/ctime_ns and transparently invalidates just
# that one file's entry. Bounded so a pathological projects tree can't grow it unbounded.
_CLAUDE_CODE_PARSE_CACHE_LOCK = threading.Lock()
_CLAUDE_CODE_PARSE_CACHE: "collections.OrderedDict[tuple, tuple]" = collections.OrderedDict()
_CLAUDE_CODE_PARSE_CACHE_MAX = 1000

# Per-file cache for the UI-owned sidecar metadata (title + archived) that the
# state.db sidebar projection overlays onto each CLI/cron row (#4842). The
# projection calls _state_projection_sidecar_metadata() once per row in BOTH
# the main visible pass AND the higher-capped (CRON_PROJECT_CHIP_LIMIT=200)
# cron-only second pass, and each call was an uncached open() + 64KB prefix
# read + a pure-Python JSON-key scan. On a cron-heavy profile that is up to
# ~200 sidecar file reads per /api/sessions build — and because the enclosing
# _CLI_SESSIONS_CACHE is keyed on a state.db content fingerprint that advances
# on every streamed message row, that whole scan was re-paid on essentially
# every streaming poll during a live turn (the "100% CPU / multi-second get_cli_sessions"
# in #4842/#4808/#4672). This memoizes the parse result keyed by the sidecar's
# (path, mtime_ns, size, ctime_ns) stat signature: a warm projection re-stats
# each file (~1 stat) instead of re-reading+parsing it, while any genuine
# rename/archive/edit bumps the signature and transparently invalidates just
# that one entry. Bounded so a pathological session store can't grow it without
# limit. Mirrors the Claude Code parse cache (#4718).
_SIDECAR_METADATA_CACHE_LOCK = threading.Lock()
_SIDECAR_METADATA_CACHE: "collections.OrderedDict[tuple, dict]" = collections.OrderedDict()
_SIDECAR_METADATA_CACHE_MAX = 2000

# #5854: authoritative facts for a LEGACY (pre-#5854) sidecar whose scenes
# serialize before `messages`, so the cheap metadata-prefix read can't recover
# its message_count or scene fingerprint. Without this, an unchanged legacy
# large-scene session would full-parse on every poll (recreating the #4633
# churn for legacy files) and could not be LRU-evicted. Populated once per file
# from a full Session.load(); keyed by the sidecar's stat signature so any edit
# invalidates it. Bounded. Value: {"message_count": int, "scene_index": dict}.
_LEGACY_SIDECAR_FACTS_LOCK = threading.Lock()
_LEGACY_SIDECAR_FACTS: "collections.OrderedDict[tuple, dict]" = collections.OrderedDict()
_LEGACY_SIDECAR_FACTS_MAX = 2000

# ---------------------------------------------------------------------------
# Stale temp-file cleanup
# ---------------------------------------------------------------------------
# Both Session.save() and _write_session_index() use the atomic-write pattern:
#   write to  <path>.tmp.<pid>.<tid>  →  os.replace() to final path
# If the process crashes between write and replace the .tmp file is left
# behind.  Because the name embeds pid + tid, leftover files can never be
# reused by a different process/thread, so they are safe to remove on the
# next startup.  _cleanup_stale_tmp_files() is called from the full-rebuild
# path of _write_session_index (i.e. at first index access / startup) and
# removes any *.tmp.* file whose mtime is older than one hour.
# ---------------------------------------------------------------------------

_STALE_TMP_AGE_SECONDS = 3600  # 1 hour


# ---------------------------------------------------------------------------
# Windows-safe os.replace() with retry
# ---------------------------------------------------------------------------
# On Windows, os.replace() raises WinError 5 (ERROR_ACCESS_DENIED) when the
# target file is momentarily locked by another process (antivirus scanner,
# browser polling the session JSON, etc.).  This helper retries with
# exponential backoff on PermissionError, which is the Python exception
# mapped from WinError 5.  On non-Windows platforms it is a thin wrapper
# (one attempt, no delay).
# ---------------------------------------------------------------------------

_WINDOWS_REPLACE_MAX_RETRIES = 5
_WINDOWS_REPLACE_INITIAL_DELAY = 0.05  # 50 ms


def _safe_replace(src: Path, dst: Path) -> None:
    """Atomic replace with retries on Windows file-locking errors."""
    if os.name != 'nt':
        os.replace(src, dst)
        return

    delay = _WINDOWS_REPLACE_INITIAL_DELAY
    for attempt in range(_WINDOWS_REPLACE_MAX_RETRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _WINDOWS_REPLACE_MAX_RETRIES - 1:
                raise
            time.sleep(delay)
            delay *= 2  # 50 -> 100 -> 200 -> 400 -> 800 ms


# Serializes index writers so concurrent Session.save() calls cannot race on
# stale baselines while still allowing LOCK to be released before disk I/O.
_INDEX_WRITE_LOCK = threading.RLock()
_SESSION_INDEX_REBUILD_LOCK = threading.Lock()
_SESSION_INDEX_REBUILD_THREAD = None
_SESSION_INDEX_REBUILD_THREAD_TARGET: tuple[Path, Path] | None = None

# Serializes ``_record_webui_zero_message_orphan_tombstone`` /
# ``_clear_webui_zero_message_orphan_tombstone`` so two concurrent sidebar
# polls (or a poll racing ``Session.save`` / ``new_session`` /
# ``import_cli_session``) cannot lose each other's load-modify-write/unlink.
# Without this lock each operation rewrites the entire tombstone file from
# scratch, so a concurrent recorder and clearer can land last-writer-wins and
# silently drop each other's update — defeating the self-healing invariant
# that ``Session.save`` clears the tombstone the same poll that re-prunes
# would otherwise re-add the row for. ``threading.Lock`` is sufficient (the
# WebUI sidebar polling path is single-process) but must wrap the WHOLE
# load-modify-write/unlink sequence in both helpers.
_WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_LOCK = threading.Lock()
_WEBUI_DELETED_SESSION_TOMBSTONE_LOCK = threading.Lock()

# Path-safety contract for session IDs.  Accept alphanumerics, underscore, and
# hyphen so API/gateway-issued ids (``api-*``, ``reachy-voice-*``) round-trip
# through filesystem load/save/delete/worktree paths without traversal risk.
# Dots and slashes are rejected so the id can never name a parent directory
# or hide an unexpected extension.
_SAFE_SID_CHARS = frozenset(
    '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_-'
)


def is_safe_session_id(sid) -> bool:
    """Return True iff ``sid`` is a non-empty path-safe session id.

    Centralizes the validation previously duplicated across
    ``Session.load``, ``Session.load_metadata_only``,
    ``_repair_stale_pending``, ``/api/session/worktree/remove``, and
    ``/api/session/delete`` so every call site agrees on what characters
    are allowed.  See #3023.
    """
    if not sid or not isinstance(sid, str):
        return False
    return all(c in _SAFE_SID_CHARS for c in sid)

__all__ = ['collections', 'copy', 'datetime', 'hashlib', 'inspect', 'json', 'logging', 'math', 'os', 're', 'threading', 'time', 'uuid', 'closing', 'contextmanager', 'Path', '_fcntl', '_msvcrt', '_cfg', 'is_context_compression_marker', 'SESSION_DIR', 'SESSION_INDEX_FILE', 'SESSIONS', 'SESSIONS_MAX', 'LOCK', 'DEFAULT_WORKSPACE', 'DEFAULT_MODEL', 'PROJECTS_FILE', 'HOME', 'get_effective_default_model', '_get_session_agent_lock', 'get_last_workspace', 'prompt_cache_hit_percent', '_is_continuation_session', 'is_cli_session_row', 'normalize_agent_session_source', 'open_state_db_readonly', 'read_importable_agent_session_rows', 'read_session_lineage_metadata', 'import_source_metadata', 'logger', 'CLI_VISIBLE_SESSION_LIMIT', 'CRON_PROJECT_CHIP_LIMIT', 'WEBHOOK_PROJECT_CHIP_LIMIT', '_CLI_SESSIONS_CACHE_TTL_SECONDS', '_CLI_SESSIONS_CACHE_STREAMING_TTL_SECONDS', '_CLI_SESSIONS_CACHE_LOCK', '_CLI_SESSIONS_CACHE_INFLIGHT', '_CLI_SESSIONS_CACHE_INVALIDATION_VERSION', '_CLI_SESSIONS_CACHE', '_CLI_SESSIONS_CACHE_MAX_ENTRIES', '_CLI_SESSIONS_CACHE_WAIT_SECONDS', '_CLI_SESSIONS_CACHE_STALE_WAIT_SECONDS', '_CLAUDE_CODE_PARSE_CACHE_LOCK', '_CLAUDE_CODE_PARSE_CACHE', '_CLAUDE_CODE_PARSE_CACHE_MAX', '_SIDECAR_METADATA_CACHE_LOCK', '_SIDECAR_METADATA_CACHE', '_SIDECAR_METADATA_CACHE_MAX', '_LEGACY_SIDECAR_FACTS_LOCK', '_LEGACY_SIDECAR_FACTS', '_LEGACY_SIDECAR_FACTS_MAX', '_STALE_TMP_AGE_SECONDS', '_WINDOWS_REPLACE_MAX_RETRIES', '_WINDOWS_REPLACE_INITIAL_DELAY', '_safe_replace', '_INDEX_WRITE_LOCK', '_SESSION_INDEX_REBUILD_LOCK', '_SESSION_INDEX_REBUILD_THREAD', '_SESSION_INDEX_REBUILD_THREAD_TARGET', '_WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_LOCK', '_WEBUI_DELETED_SESSION_TOMBSTONE_LOCK', '_SAFE_SID_CHARS', 'is_safe_session_id']
