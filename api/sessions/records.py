
"""Durable session records, sidecars, indexes, caches, and projections.

This is the cohesive persistence implementation behind :mod:`api.sessions`.
It intentionally keeps the ordering-sensitive sidecar/index/cache machinery in
one module: splitting that state previously required a mutable module-registry
facade and made no file independently own the persistence protocol.
"""
import collections
import copy
import datetime
import hashlib
import inspect
from importlib.util import find_spec
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
from .sources import import_source_metadata

logger = logging.getLogger("api.models")
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




def _cleanup_stale_tmp_files() -> None:
    """Best-effort removal of stale ``*.tmp.*`` files from SESSION_DIR.

    Only files whose mtime is older than ``_STALE_TMP_AGE_SECONDS`` are
    removed so that in-flight writes from a long-running sibling process
    are not disturbed.  Errors are logged and swallowed — this must never
    prevent startup.
    """
    cutoff = time.time() - _STALE_TMP_AGE_SECONDS
    try:
        for p in SESSION_DIR.glob('*.tmp.*'):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
                    logger.debug("Cleaned up stale tmp file: %s", p.name)
            except OSError:
                pass  # best-effort
    except Exception:
        pass  # SESSION_DIR may not exist yet; that's fine


_PERSISTED_SESSION_IDS_CACHE: tuple[Path | None, int | None, frozenset[str]] = (None, None, frozenset())


def _persisted_session_ids_snapshot() -> frozenset[str]:
    """Return persisted session ids, caching the directory snapshot by mtime.

    `/api/sessions` and incremental index writes may run every few seconds. A
    full `SESSION_DIR.glob('*.json')` on a large session directory is expensive,
    and doing that scan while request threads contend on LOCK makes the sidebar
    look like it was designed by a committee of glaciers. Cache the listing until
    the directory mtime changes, and let callers take the snapshot before
    entering critical sections.
    """
    global _PERSISTED_SESSION_IDS_CACHE
    try:
        dir_mtime_ns = SESSION_DIR.stat().st_mtime_ns
    except Exception:
        dir_mtime_ns = None
    cached_dir, cached_mtime_ns, cached_ids = _PERSISTED_SESSION_IDS_CACHE
    if cached_dir == SESSION_DIR and cached_mtime_ns == dir_mtime_ns:
        return cached_ids
    try:
        ids = frozenset(
            p.stem
            for p in SESSION_DIR.glob('*.json')
            if not p.name.startswith('_')
        )
    except Exception:
        ids = frozenset()
    _PERSISTED_SESSION_IDS_CACHE = (SESSION_DIR, dir_mtime_ns, ids)
    return ids


def _session_dir_has_persisted_session_files() -> bool:
    """Return True when the current session dir has at least one session JSON file."""
    try:
        return any(not p.name.startswith('_') for p in SESSION_DIR.glob('*.json'))
    except Exception:
        return False


def _rebuild_session_index_background(expected_session_dir: Path, expected_index_file: Path) -> None:
    global _SESSION_INDEX_REBUILD_THREAD, _SESSION_INDEX_REBUILD_THREAD_TARGET
    current_thread = threading.current_thread()
    try:
        with _SESSION_INDEX_REBUILD_LOCK:
            if SESSION_DIR != expected_session_dir or SESSION_INDEX_FILE != expected_index_file:
                return
        _write_session_index(
            updates=None,
            session_dir=expected_session_dir,
            session_index_file=expected_index_file,
        )
    except Exception:
        logger.debug("Background session-index rebuild failed", exc_info=True)
    finally:
        with _SESSION_INDEX_REBUILD_LOCK:
            if _SESSION_INDEX_REBUILD_THREAD is current_thread and _SESSION_INDEX_REBUILD_THREAD_TARGET == (
                expected_session_dir,
                expected_index_file,
            ):
                _SESSION_INDEX_REBUILD_THREAD = None
                _SESSION_INDEX_REBUILD_THREAD_TARGET = None


def _start_session_index_rebuild_thread() -> None:
    """Start one background full-index rebuild if the index is missing."""
    global _SESSION_INDEX_REBUILD_THREAD, _SESSION_INDEX_REBUILD_THREAD_TARGET
    target = (SESSION_DIR, SESSION_INDEX_FILE)
    with _SESSION_INDEX_REBUILD_LOCK:
        if SESSION_INDEX_FILE.exists():
            return
        if (
            _SESSION_INDEX_REBUILD_THREAD is not None
            and _SESSION_INDEX_REBUILD_THREAD.is_alive()
            and _SESSION_INDEX_REBUILD_THREAD_TARGET == target
        ):
            return
        _SESSION_INDEX_REBUILD_THREAD_TARGET = target
        _SESSION_INDEX_REBUILD_THREAD = threading.Thread(
            target=_rebuild_session_index_background,
            args=target,
            name="session-index-rebuild",
            daemon=True,
        )
        _SESSION_INDEX_REBUILD_THREAD.start()


def _index_entry_exists(session_id: str, in_memory_ids=None) -> bool:
    """Return True if an index entry still has backing state.

    A session can legitimately exist either as a persisted JSON file or as an
    in-memory Session object that has not been flushed yet.  This helper is used
    to prune stale `_index.json` rows left behind after session-id rotation or
    file removal.
    """
    if not session_id:
        return False
    if in_memory_ids is None:
        with LOCK:
            in_memory_ids = set(SESSIONS.keys())
    if session_id in in_memory_ids:
        return True
    p = SESSION_DIR / f'{session_id}.json'
    return p.exists()


def _write_session_index(updates=None, *, session_dir: Path | None = None, session_index_file: Path | None = None):
    """Update the session index file.

    When *updates* is provided (a list of Session objects whose compact
    entries should be refreshed), this does a targeted in-place update of
    the existing index — O(1) for single-session changes.  When *updates*
    is None, a full rebuild is performed (used on startup / first call).

    LOCK protects only in-memory session snapshots.  JSON parsing, payload
    construction, and disk I/O run outside LOCK so active-stream saves do not
    block ordinary session reads longer than necessary.  The on-disk index
    read-modify-write is NOT unsynchronized: it stays fully serialized by
    ``_INDEX_WRITE_LOCK`` (held across this whole function), so narrowing LOCK
    cannot introduce a lost-update or index-corruption race between writers.
    """
    session_dir = session_dir or SESSION_DIR
    session_index_file = session_index_file or SESSION_INDEX_FILE
    _tmp = session_index_file.with_suffix(f'.tmp.{os.getpid()}.{threading.current_thread().ident}')

    with _INDEX_WRITE_LOCK:
        # Lazy full-rebuild path — used when index doesn't exist yet.
        if updates is None or not session_index_file.exists():
            _cleanup_stale_tmp_files()  # best-effort sweep on startup / first call
            entry_map: dict[str, dict] = {}
            for p in session_dir.glob('*.json'):
                if p.name.startswith('_'):
                    continue
                try:
                    s = _load_session_from_path(p)
                    if s:
                        c = s.compact()
                        sid = c.get('session_id')
                        if sid:
                            # Dedup by session_id: prefer entry with more messages
                            # (handles old-format session_xxx.json files alongside
                            #  WebUI-format xxx.json with the same session_id)
                            existing = entry_map.get(sid)
                            if existing is None or (
                                c.get('message_count', 0) > existing.get('message_count', 0)
                            ):
                                entry_map[sid] = c
                except Exception:
                    logger.debug("Failed to load session from %s", p)
            entries = list(entry_map.values())

            existing_ids = set(entry_map.keys())
            with LOCK:
                in_memory_entries = [
                    s.compact()
                    for s in SESSIONS.values()
                    if s.session_id not in existing_ids
                ]
            entries.extend(in_memory_entries)
            entries.sort(key=lambda s: s.get('updated_at', 0), reverse=True)
            _payload = json.dumps(entries, ensure_ascii=False, indent=2)

            try:
                with open(_tmp, 'w', encoding='utf-8') as f:
                    f.write(_payload)
                    f.flush()
                    os.fsync(f.fileno())
                _safe_replace(_tmp, session_index_file)
            except Exception:
                # Best-effort cleanup of stale tmp on failure
                try:
                    _tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
            return

        # Fast path: patch existing index with updated sessions.
        # This avoids loading every session file on every single save().
        _fallback = False
        try:
            # Avoid N filesystem exists() checks under LOCK by collecting
            # on-disk IDs once before entering the critical section.
            on_disk_ids = _persisted_session_ids_snapshot()
            existing = json.loads(session_index_file.read_bytes())
            if not isinstance(existing, list):
                raise ValueError("session index must be a list")
            with LOCK:
                in_memory_ids = set(SESSIONS.keys())
                updated_map = {s.session_id: s.compact() for s in updates}

            existing = [
                e for e in existing
                if (e.get('session_id') in in_memory_ids or e.get('session_id') in on_disk_ids)
            ]

            existing_ids = {e.get('session_id') for e in existing}
            # Add any updated entries not yet in the index.
            for sid, entry in updated_map.items():
                if sid not in existing_ids:
                    existing.append(entry)
            # Replace matching entries in-place.
            for i, e in enumerate(existing):
                sid = e.get('session_id')
                if sid in updated_map:
                    existing[i] = updated_map[sid]
            existing.sort(key=lambda s: s.get('updated_at', 0), reverse=True)
            _payload = json.dumps(existing, ensure_ascii=False, indent=2)

            try:
                with open(_tmp, 'w', encoding='utf-8') as f:
                    f.write(_payload)
                    f.flush()
                    os.fsync(f.fileno())
                _safe_replace(_tmp, session_index_file)
            except Exception:
                try:
                    _tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
        except Exception:
            _fallback = True

    if _fallback:
        # Corrupt or missing index — fall back to full rebuild (called outside LOCK to avoid deadlock).
        # Propagate the resolved target so a rebuild scoped to a specific session dir
        # (the background rebuild thread) falls back to rebuilding THAT dir's index,
        # not the global SESSION_DIR (Opus advisor, stage-344 — defensive; today the
        # only kwargs-caller passes updates=None and never reaches the fast path).
        _write_session_index(
            updates=None,
            session_dir=session_dir,
            session_index_file=session_index_file,
        )


def prune_session_from_index(session_id: str) -> None:
    """Remove one session row from the persisted sidebar index if present."""
    sid = str(session_id or "")
    if not sid or not SESSION_INDEX_FILE.exists():
        return
    _tmp = SESSION_INDEX_FILE.with_suffix(f'.tmp.{os.getpid()}.{threading.current_thread().ident}')

    _fallback = False
    with _INDEX_WRITE_LOCK:
        try:
            with LOCK:
                existing = json.loads(SESSION_INDEX_FILE.read_bytes())
                if not isinstance(existing, list):
                    raise ValueError("session index must be a list")
                pruned = [e for e in existing if e.get('session_id') != sid]
                if len(pruned) == len(existing):
                    return
                _payload = json.dumps(pruned, ensure_ascii=False, indent=2)

            try:
                with open(_tmp, 'w', encoding='utf-8') as f:
                    f.write(_payload)
                    f.flush()
                    os.fsync(f.fileno())
                _safe_replace(_tmp, SESSION_INDEX_FILE)
            except Exception:
                try:
                    _tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
        except Exception:
            _fallback = True

    if _fallback:
        _write_session_index(updates=None)


# ---------------------------------------------------------------------------
# #4985 webui zero-message orphan tombstone
# ---------------------------------------------------------------------------
# ``prune_session_from_index()`` only removes a row from SESSION_INDEX_FILE —
# the on-disk sidecar at ``SESSION_DIR / f"{sid}.json"`` is intentionally
# kept (it may hold legitimate WebUI-owned metadata a future code path wants
# to recover). On the next ``/api/sessions`` poll, ``all_sessions()``'s
# ``recover_missing_index_sidecars`` pass (``missing_persisted_ids``) sees
# the orphaned sidecar, re-loads it via ``Session.load_metadata_only()``,
# and writes it back to SESSION_INDEX_FILE — undoing the prune.
#
# For #4985 zero-message webui orphans, that round-trip would also re-add
# the row to the sidebar (it survives #1171 because it is titled or has a
# stale positive message_count), so the next prune fires again. N orphans
# therefore cost 2N fsync'd index writes + N state.db probes per poll,
# forever. The fix is a small, dedicated tombstone set written alongside
# the prune: any sid in the tombstone is skipped by
# ``recover_missing_index_sidecars`` (no re-add to index) and is therefore
# never re-presented to the prune batch.
#
# The file lives in SESSION_DIR (sibling of _index.json) so it is
# profile-local and survives across processes, and is intentionally NOT
# itself listed as a session sidecar — it is excluded from
# ``_persisted_session_ids_snapshot()`` via the same ``name.startswith('_')``
# convention (its name starts with ``.``, a dot — but we add a dedicated
# check below for paranoia). Bounded size keeps the file from growing
# without limit on long-running installs.
# ---------------------------------------------------------------------------

WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_CAP = 500
WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_VERSION = 1
WEBUI_DELETED_SESSION_TOMBSTONE_CAP = 1000
WEBUI_DELETED_SESSION_TOMBSTONE_VERSION = 1


def _webui_zero_message_orphan_tombstone_file() -> "Path":
    """Return the current tombstone file path.

    Resolved at call time (not module load) so tests that monkeypatch
    ``SESSION_DIR`` (e.g. ``_real_pipeline``) get a per-test path without
    having to also rewrite the module-level constant. Mirrors how
    ``SESSION_INDEX_FILE`` is computed but resolved at call time so the
    real path tracks the live ``SESSION_DIR``.
    """
    return SESSION_DIR / "_pruned_webui_orphans.json"


def _load_webui_zero_message_orphan_tombstone() -> frozenset[str]:
    """Return sids we've explicitly pruned as webui zero-message orphans.

    Degrades to ``frozenset()`` on any read error, missing file, version
    mismatch, or schema mismatch so the recovery path never accidentally
    admits a row that should stay tombstoned.
    """
    p = _webui_zero_message_orphan_tombstone_file()
    if not p.exists():
        return frozenset()
    try:
        raw = json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        logger.debug(
            "Failed to load webui zero-message orphan tombstone",
            exc_info=True,
        )
        return frozenset()
    if not isinstance(raw, dict):
        return frozenset()
    try:
        if int(raw.get("version", 0)) != WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_VERSION:
            return frozenset()
    except (TypeError, ValueError):
        return frozenset()
    ids = raw.get("ids", [])
    if not isinstance(ids, list):
        return frozenset()
    return frozenset(
        str(sid).strip() for sid in ids if str(sid or "").strip()
    )


def _save_webui_zero_message_orphan_tombstone(ids) -> None:
    """Persist the tombstone set with a bounded size cap (lexicographically-first N entries).

    Sorts + dedupes so the on-disk file is deterministic and diff-friendly.
    Atomic write via ``.tmp.<pid>.<tid>`` + ``os.replace`` mirrors
    ``_write_session_index`` and ``Session.save`` so a crash mid-write does
    not leave a half-written tombstone file.

    Note on eviction order: ``sorted_ids[:WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_CAP]``
    keeps the lexicographically-FIRST ``N`` sids (sorted ascending), not the
    last-pruned ``N``. Session ids are random UUIDs (``uuid.uuid4().hex[:12]``),
    so the eviction is effectively random across installs; the cap exists to
    keep the file bounded on long-running installs, not to implement FIFO
    pruning. If true FIFO is ever needed, switch the slice to ``[-N:]`` and
    keep an insertion-ordered data structure.
    """
    try:
        sorted_ids = sorted(set(
            str(sid).strip() for sid in (ids or []) if str(sid or "").strip()
        ))
    except TypeError:
        return
    if len(sorted_ids) > WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_CAP:
        sorted_ids = sorted_ids[-WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_CAP:]
    payload = {
        "version": WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_VERSION,
        "ids": sorted_ids,
    }
    p = _webui_zero_message_orphan_tombstone_file()
    _tmp = None
    try:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        _tmp = p.with_suffix(
            f'.tmp.{os.getpid()}.{threading.current_thread().ident}'
        )
        with open(_tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(_tmp, p)
    except Exception:
        logger.debug(
            "Failed to save webui zero-message orphan tombstone",
            exc_info=True,
        )
        if _tmp is not None:
            try:
                _tmp.unlink(missing_ok=True)
            except Exception:
                pass


def _record_webui_zero_message_orphan_tombstone(sid: str) -> None:
    """Add ``sid`` to the tombstone.

    No-op if already present (avoids re-sorting and re-fsync'ing on every
    redundant prune). Called from the ``#4985`` prune helper in
    ``api.routes`` immediately after ``prune_session_from_index``.

    Wraps the entire load-modify-write in ``_WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_LOCK``
    so two concurrent sidebar polls (or a poll racing ``Session.save`` /
    ``new_session`` / ``import_cli_session``) cannot lose each other's
    writes. Without the lock each operation rewrites the entire tombstone
    file from scratch, so a concurrent recorder and clearer can land
    last-writer-wins and silently drop each other's update — defeating the
    self-healing invariant that ``Session.save`` clears the tombstone the
    same poll that the prune helper re-prunes would otherwise re-add the
    row for.
    """
    sid = str(sid or "").strip()
    if not sid:
        return
    with _WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_LOCK:
        current = set(_load_webui_zero_message_orphan_tombstone())
        if sid in current:
            return
        current.add(sid)
        _save_webui_zero_message_orphan_tombstone(current)


def _clear_webui_zero_message_orphan_tombstone(sid: str) -> None:
    """Remove ``sid`` from the tombstone.

    Called when a new Session is created with an explicit sid (e.g.
    ``new_session()`` / ``import_cli_session()``) and belt-and-suspenders
    whenever ``Session.save`` writes a real conversation (a save with
    ``len(messages) > 0`` proves the row is alive, so the tombstone entry
    must drop). Safe to call with an unknown sid (no-op). If the tombstone
    becomes empty as a result, the file is removed entirely so an empty
    poll-time load stays free.

    Wraps the entire load-modify-write/unlink in
    ``_WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_LOCK`` so concurrent
    recorders/clearers cannot lose each other's writes — see the docstring
    on ``_record_webui_zero_message_orphan_tombstone``.
    """
    sid = str(sid or "").strip()
    if not sid:
        return
    with _WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_LOCK:
        current = set(_load_webui_zero_message_orphan_tombstone())
        if sid not in current:
            return
        current.discard(sid)
        if current:
            _save_webui_zero_message_orphan_tombstone(current)
            return
        try:
            _webui_zero_message_orphan_tombstone_file().unlink(missing_ok=True)
        except Exception:
            logger.debug(
                "Failed to remove empty webui zero-message orphan tombstone",
                exc_info=True,
            )


def _webui_deleted_session_tombstone_file() -> "Path":
    return SESSION_DIR / "_deleted_webui_sessions.json"


def _load_webui_deleted_session_tombstone() -> frozenset[str]:
    p = _webui_deleted_session_tombstone_file()
    if not p.exists():
        return frozenset()
    try:
        raw = json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        logger.debug("Failed to load webui deleted-session tombstone", exc_info=True)
        return frozenset()
    if not isinstance(raw, dict):
        return frozenset()
    try:
        if int(raw.get("version", 0)) != WEBUI_DELETED_SESSION_TOMBSTONE_VERSION:
            return frozenset()
    except (TypeError, ValueError):
        return frozenset()
    ids = raw.get("ids", [])
    if not isinstance(ids, list):
        return frozenset()
    return frozenset(
        str(sid).strip() for sid in ids if str(sid or "").strip()
    )


def _save_webui_deleted_session_tombstone(ids) -> None:
    try:
        sorted_ids = sorted(set(
            str(sid).strip() for sid in (ids or []) if str(sid or "").strip()
        ))
    except TypeError:
        return
    if len(sorted_ids) > WEBUI_DELETED_SESSION_TOMBSTONE_CAP:
        sorted_ids = sorted_ids[-WEBUI_DELETED_SESSION_TOMBSTONE_CAP:]
    payload = {
        "version": WEBUI_DELETED_SESSION_TOMBSTONE_VERSION,
        "ids": sorted_ids,
    }
    p = _webui_deleted_session_tombstone_file()
    _tmp = None
    try:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        _tmp = p.with_suffix(
            f'.tmp.{os.getpid()}.{threading.current_thread().ident}'
        )
        with open(_tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(_tmp, p)
    except Exception:
        logger.debug("Failed to save webui deleted-session tombstone", exc_info=True)
        if _tmp is not None:
            try:
                _tmp.unlink(missing_ok=True)
            except Exception:
                pass


def _record_webui_deleted_session_tombstone(sid: str) -> None:
    sid = str(sid or "").strip()
    if not sid:
        return
    with _WEBUI_DELETED_SESSION_TOMBSTONE_LOCK:
        current = set(_load_webui_deleted_session_tombstone())
        if sid in current:
            return
        current.add(sid)
        _save_webui_deleted_session_tombstone(current)


def _clear_webui_deleted_session_tombstone(sid: str) -> None:
    sid = str(sid or "").strip()
    if not sid:
        return
    with _WEBUI_DELETED_SESSION_TOMBSTONE_LOCK:
        current = set(_load_webui_deleted_session_tombstone())
        if sid not in current:
            return
        current.discard(sid)
        if current:
            _save_webui_deleted_session_tombstone(current)
            return
        try:
            _webui_deleted_session_tombstone_file().unlink(missing_ok=True)
        except Exception:
            logger.debug("Failed to remove empty webui deleted-session tombstone", exc_info=True)


def _content_has_reasoning_only_parts(content) -> bool:
    if not isinstance(content, list) or not content:
        return False
    saw_reasoning = False
    for part in content:
        if not isinstance(part, dict):
            if str(part or '').strip():
                return False
            continue
        part_type = str(part.get('type') or '').lower()
        if part_type in {'thinking', 'reasoning'}:
            text = part.get('thinking') or part.get('reasoning') or part.get('text') or ''
            if str(text).strip():
                saw_reasoning = True
            continue
        if part_type == 'text' and str(part.get('text') or part.get('content') or '').strip():
            return False
        if part_type not in {'text', 'thinking', 'reasoning'}:
            return False
    return saw_reasoning


def _active_stream_ids():
    # Runtime ownership combines live transports with detached workers so stale
    # repair cannot misclassify a provider-blocked or cancelling run as dead.
    return set(_cfg.runtime_active_run_ids())


def _recovered_model_context_projection(message: dict) -> dict | None:
    if not isinstance(message, dict):
        return None
    projected = dict(message)
    projected.pop('reasoning', None)
    if projected.get('_error'):
        return None
    if _content_has_reasoning_only_parts(projected.get('content')):
        if projected.get('tool_calls'):
            projected['content'] = ''
        else:
            return None
    projected_text = _normalize_journal_recovery_text(projected.get('content'))
    if not projected_text and not projected.get('tool_call_id') and not projected.get('tool_calls'):
        return None
    return projected


def _append_recovered_context_projection(
    session,
    context_messages: list,
    recovered: dict,
) -> None:
    recovered_text = _normalize_journal_recovery_text(recovered.get('content'))
    if recovered_text:
        if recovered.get('role') == 'user':
            if _message_matches_pending_checkpoint(
                context_messages[-1] if context_messages else None,
                recovered.get('content'),
                recovered.get('timestamp'),
                recovered.get('_source'),
                recovered.get('attachments'),
            ):
                return
        else:
            for existing in reversed(context_messages[-8:]):
                if not isinstance(existing, dict) or existing.get('role') != recovered.get('role'):
                    continue
                if _normalize_journal_recovery_text(existing.get('content')) == recovered_text:
                    return
    context_messages.append(dict(recovered))


def _seed_recovered_context_from_messages(session, context_messages: list) -> None:
    for message in getattr(session, 'messages', None) or []:
        projected = _recovered_model_context_projection(message)
        if projected is None:
            continue
        context_messages.append(projected)


def _append_recovered_turn_to_context(session, recovered: dict) -> None:
    context_messages = getattr(session, 'context_messages', None)
    if not isinstance(context_messages, list):
        context_messages = []
        session.context_messages = context_messages
    if not context_messages:
        _seed_recovered_context_from_messages(session, context_messages)
    projected = _recovered_model_context_projection(recovered)
    if projected is None:
        return
    _append_recovered_context_projection(session, context_messages, projected)


def _append_recovered_pending_turn(session, *, timestamp: int | None = None) -> dict | None:
    pending_text = str(session.pending_user_message or '')
    if not pending_text:
        return None
    recovered_ts = int(time.time())
    if isinstance(timestamp, (int, float)) and timestamp > 0:
        recovered_ts = int(timestamp)
    recovered: dict = {
        'role': 'user',
        'content': session.pending_user_message,
        'timestamp': recovered_ts,
        '_recovered': True,
    }
    pending_source = getattr(session, 'pending_user_source', None)
    if pending_source and pending_source != 'webui':
        recovered['_source'] = pending_source
    if session.pending_attachments:
        recovered['attachments'] = list(session.pending_attachments)
    session.messages.append(recovered)
    _append_recovered_turn_to_context(session, recovered)
    # The new user turn is now committed to messages (#3831): advance the
    # truncation watermark to the new message's timestamp so that
    # merge_session_messages_append_only() still filters out replaced
    # pre-edit rows from state.db whose timestamps fall below the boundary.
    # The merge's sidecar_advanced_past_watermark guard allows state.db rows
    # newer than the watermark, so post-edit turns are not dropped.
    # Never 0.0 (the truncate-to-empty sentinel, #2914).
    if getattr(session, 'truncation_watermark', None):
        session.truncation_watermark = recovered_ts
    return recovered


def _is_streaming_session(active_stream_id, active_stream_ids):
    return bool(active_stream_id and active_stream_id in active_stream_ids)

def _session_sort_timestamp(session):
    if isinstance(session, dict):
        return session.get('last_message_at') or session.get('updated_at') or 0
    return _last_message_timestamp(getattr(session, 'messages', None)) or getattr(session, 'updated_at', 0) or 0


def _message_timestamp(message):
    if not isinstance(message, dict):
        return None
    raw = message.get('_ts') or message.get('timestamp')
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _is_empty_partial_activity_message(message):
    """Return True for cancelled/recovered activity rows with no reply text."""
    if not isinstance(message, dict):
        return False
    if message.get('role') != 'assistant' or not message.get('_partial'):
        return False
    content = message.get('content', '')
    if isinstance(content, str):
        return not content.strip()
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if part.get('type') == 'text' and str(part.get('text') or part.get('content') or '').strip():
                    return False
                continue
            if str(part or '').strip():
                return False
        return True
    return not str(content or '').strip()


def _last_message_timestamp(messages, *, tail_window: int = 8):
    """perf(session-load-latency) Priority 1: bounded tail-scan.

    Old behavior: reversed-iterate ALL messages until a non-tool, non-empty
    message's timestamp is found. For a 2,730-message session on eMMC, that's
    ~500ms of Python attribute lookups, repeated on every /api/session
    response.

    New behavior: the messages array is chronologically ordered, so the
    last non-tool message is at the very end. We scan only the last
    ``tail_window`` messages — covers the realistic case where 1-3 tool
    rows sit after the last assistant/user message. Falls back to a full
    scan only when no timestamp is found in the window, which preserves
    exact correctness for messages with very large trailing tool clusters
    (rare in practice; we'd need >8 consecutive tool rows to hit it).
    """
    if not isinstance(messages, list):
        return None
    n = len(messages)
    start = max(0, n - max(1, int(tail_window)))
    # Walk from the end backwards. reversed() over a slice still creates
    # a full reverse iterator, but only the slice's elements are touched.
    for message in reversed(messages[start:]):
        if isinstance(message, dict) and message.get('role') == 'tool':
            continue
        if _is_empty_partial_activity_message(message):
            continue
        ts = _message_timestamp(message)
        if ts:
            return ts
    # Window miss — fall back to the original full-reversed scan. The
    # caller pays this cost only when the heuristic didn't find a hit,
    # which means the session is unusual (long tool tail or all-empty
    # messages).
    for message in reversed(messages):
        if isinstance(message, dict) and message.get('role') == 'tool':
            continue
        if _is_empty_partial_activity_message(message):
            continue
        ts = _message_timestamp(message)
        if ts:
            return ts
    return None


def _message_role(message):
    if not isinstance(message, dict):
        return ''
    return str(message.get('role', '')).strip().lower()


def _find_top_level_json_key(text, key):
    """Return the byte offset of a top-level JSON object key, if present."""
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            start = i
            i += 1
            escaped = False
            chars = []
            while i < n:
                c = text[i]
                if escaped:
                    chars.append(c)
                    escaped = False
                elif c == '\\':
                    escaped = True
                elif c == '"':
                    break
                else:
                    chars.append(c)
                i += 1
            if i >= n:
                return None
            if depth == 1 and ''.join(chars) == key:
                j = i + 1
                while j < n and text[j] in ' \t\r\n':
                    j += 1
                if j < n and text[j] == ':':
                    return start
        elif ch in '{[':
            depth += 1
        elif ch in '}]':
            depth -= 1
        i += 1
    return None


def _read_file_head(path: Path, max_prefix_bytes: int = 4096) -> str:
    """Read at most ``max_prefix_bytes`` bytes from ``path`` and decode UTF-8."""
    if not isinstance(path, Path):
        path = Path(path)
    if max_prefix_bytes <= 0:
        return ''
    with path.open('rb') as fp:
        return fp.read(max_prefix_bytes).decode('utf-8', errors='ignore')


def _read_metadata_json_prefix(path, max_prefix_bytes=65536):
    """Read only the metadata portion before the large arrays.

    #5854: stop at the top-level ``messages`` key OR the top-level
    ``anchor_activity_scenes`` key, whichever appears first. On the modern
    layout scenes serialize AFTER ``messages`` so this stops at ``messages`` as
    before (the prefix is now small — scene bodies are no longer in it). On the
    LEGACY layout scenes serialize BEFORE ``messages`` and can be 250-480KB, so
    stopping at ``anchor_activity_scenes`` keeps the read cheap and — critically
    — still captures ``message_count`` (which is written before both). Without
    the scenes-stop a legacy large-scene sidecar overflows ``max_prefix_bytes``
    and forces a full multi-MB parse on every poll (the #4633 churn).
    """
    buf = ''
    with open(path, 'r', encoding='utf-8') as f:
        while len(buf.encode('utf-8')) < max_prefix_bytes:
            chunk = f.read(4096)
            if not chunk:
                return None
            buf += chunk
            stop_pos = _find_top_level_json_key(buf, 'messages')
            scenes_pos = _find_top_level_json_key(buf, 'anchor_activity_scenes')
            if scenes_pos is not None and (stop_pos is None or scenes_pos < stop_pos):
                stop_pos = scenes_pos
            if stop_pos is None:
                continue
            prefix = buf[:stop_pos].rstrip()
            if prefix.endswith(','):
                prefix = prefix[:-1].rstrip()
            return f'{prefix}\n}}'
    return None


def _load_session_from_path(path: Path) -> "Session | None":
    """Load a session from an explicit JSON path without consulting SESSION_DIR."""
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    data['messages'], _collapsed_partials = _collapse_adjacent_duplicate_partials(data.get('messages'))
    return Session(**data)


def _lookup_index_message_count(session_id):
    """Return the indexed message count without loading the full session file."""
    return _index_message_count_map().get(str(session_id))


def _index_message_count_map(entries=None) -> dict[str, int]:
    """Return indexed message counts keyed by session id.

    ``load_metadata_only()`` is called in loops for stale lineage/sidebar rows.
    Reading and parsing ``_index.json`` once per row turns /api/sessions into an
    accidental O(n²) poll for old sidecars that predate persisted
    ``message_count``. Accepting already-loaded index rows lets callers reuse
    the index they just parsed.
    """
    if entries is None:
        try:
            entries = json.loads(SESSION_INDEX_FILE.read_bytes())
        except Exception:
            return {}
    if not isinstance(entries, list):
        return {}
    counts: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        sid = str(entry.get('session_id') or '')
        if not sid:
            continue
        count = entry.get('message_count')
        if not isinstance(count, int):
            try:
                count = int(count)
            except (TypeError, ValueError):
                continue
        if count >= 0:
            counts[sid] = count
    return counts


def _parse_nonnegative_int(value):
    if isinstance(value, int) and value >= 0:
        return value
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def model_explicit_pick_signature(model, model_provider) -> str:
    """Stable signature of a (model, provider) selection for #5979 explicit-pick
    provenance. The persisted ``Session.model_explicit_pick_signature`` is set to
    this when the user deliberately picks a model; the streaming resolver only
    treats a selection as deliberate when the CURRENT routing context produces
    the same signature. Any model/provider change (chat-start, session-update,
    normalization, provider repair) yields a different signature and thus
    invalidates the stale pick — so a #433 first-party leftover is never wrongly
    preserved. Uses \\x1f (unit separator) so it can't collide with model ids.
    """
    _m = str(model or "").strip()
    _p = str(model_provider or "").strip().lower()
    return f"{_m}\x1f{_p}"


class _SessionPersistenceMixin:
    @property
    def path(self):
        return SESSION_DIR / f'{self.session_id}.json'

    def save(
        self,
        touch_updated_at: bool = True,
        skip_index: bool = False,
        *,
        _admission_compensation: bool = False,
    ) -> None:
        if not is_safe_session_id(self.session_id):
            raise ValueError(f"Unsafe session_id {self.session_id!r}; refusing to write outside session store")
        # ── #1558 P0 guard ──────────────────────────────────────────────
        # Refuse to save a session that was loaded with metadata_only=True.
        # Such sessions have messages=[] (it's the whole point of the partial
        # load), and save() unconditionally writes self.messages to disk via
        # an atomic os.replace(). Saving a metadata-only stub thus wipes the
        # full conversation history — which is exactly the v0.50.279
        # _clear_stale_stream_state() regression that lost users 1000+
        # message conversations. Any caller that needs to mutate persisted
        # fields on a metadata-only session must reload with
        # metadata_only=False first.
        if getattr(self, '_loaded_metadata_only', False):
            raise RuntimeError(
                f"Refusing to save metadata-only session {self.session_id!r}: "
                f"would atomically overwrite on-disk messages with []. "
                f"Reload with metadata_only=False before mutating state. "
                f"See #1558."
            )
        if touch_updated_at:
            self.updated_at = time.time()
        # Write metadata fields first so load_metadata_only() can read them
        # without parsing the full messages array (which may be 400KB+).
        # Fields are listed in the order they should appear in the JSON file.
        METADATA_FIELDS = [
            'session_id', 'title', 'workspace', 'model', 'model_provider', 'model_explicit_pick_signature', 'created_at', 'updated_at',
            'pinned', 'archived', 'project_id', 'profile',
            'input_tokens', 'output_tokens', 'estimated_cost',
            'cache_read_tokens', 'cache_write_tokens',
            'personality', 'active_stream_id',
            'pending_user_message', 'pending_attachments', 'pending_started_at', 'pending_user_source',
            'compression_anchor_visible_idx', 'compression_anchor_message_key',
            'compression_anchor_summary', 'pre_compression_snapshot',
            'context_engine', 'compression_anchor_engine', 'compression_anchor_mode',
            'compression_anchor_details', 'context_engine_state',
            'context_length', 'threshold_tokens', 'last_prompt_tokens',
            'post_compression_context_tokens_estimate',
            'compression_recovery', 'recommended_recovery_action',
            'compression_recovery_source_session_id', 'compression_recovery_action',
            'truncation_watermark',
            'truncation_boundary',
            'clear_generation',
            'gateway_routing', 'gateway_routing_history', 'llm_title_generated', 'manual_title',
            'parent_session_id',
            'worktree_path', 'worktree_branch', 'worktree_repo_root', 'worktree_created_at',
            'is_cli_session', 'source_tag', 'raw_source', 'session_source', 'source_label',
            'user_id', 'chat_id', 'chat_type', 'thread_id', 'session_key', 'platform',
            'read_only',
            'enabled_toolsets', 'composer_draft',
            'process_wakeup_pause',
            'share_token', 'share_created_at',
        ]
        meta = {k: getattr(self, k, None) for k in METADATA_FIELDS}
        # #5854: message_count and a compact anchor-scene fingerprint go in the
        # metadata prefix (BEFORE messages) so load_metadata_only() and the
        # sidebar-poll freshness check never have to parse the full (250-480KB)
        # scene bodies. message_count is placed BEFORE anchor_scene_index so a
        # legacy-format reader that stops at a scene key still finds the count.
        # The full anchor_activity_scenes bodies serialize AFTER messages.
        meta['message_count'] = len(self.messages or [])
        meta['anchor_scene_index'] = _anchor_scene_index_from_records(self.anchor_activity_scenes)
        # Keep the in-memory fingerprint aligned with what we just persisted, so a
        # later metadata-only reload of THIS object (or any fingerprint reader)
        # sees the current value rather than a stale load-time snapshot (#5854
        # defense-in-depth; the cached-side freshness check reads real records,
        # not this, so this is belt-and-suspenders).
        self._anchor_scene_index = dict(meta['anchor_scene_index'])
        meta['messages'] = self.messages
        meta['tool_calls'] = self.tool_calls
        meta['anchor_activity_scenes'] = self.anchor_activity_scenes if isinstance(self.anchor_activity_scenes, dict) else {}
        # Fields not in METADATA_FIELDS (e.g. last_usage) go at the end. Exclude
        # the keys we placed explicitly above so they aren't emitted twice.
        _placed = {'message_count', 'anchor_scene_index', 'messages', 'tool_calls', 'anchor_activity_scenes'}
        extra = {k: v for k, v in self.__dict__.items()
                 if k not in METADATA_FIELDS and k not in _placed
                 and not k.startswith('_')}
        payload = json.dumps({**meta, **extra}, ensure_ascii=False, indent=2)

        # ── #1558 backup safeguard ──────────────────────────────────────
        # Before overwriting the session file, copy the previous version to
        # ``<sid>.json.bak`` IFF the previous file has more messages than the
        # incoming payload. The asymmetric guard means:
        #   * Normal grow-the-conversation saves never produce a backup
        #     (incoming messages >= existing) — keeps disk overhead near zero.
        #   * Any save that would shrink the messages array (the failure mode
        #     of #1558, plus anything similar in the future) leaves a recoverable
        #     snapshot of the pre-shrink state on disk.
        # The recovery path is api/session_recovery.py — at server startup and
        # via /api/session/recover, sessions whose JSON has fewer messages than
        # their .bak get restored automatically.
        try:
            if self.path.exists():
                existing_text = self.path.read_text(encoding='utf-8')
                try:
                    existing = json.loads(existing_text)
                    existing_msg_count = len(existing.get('messages') or [])
                except (json.JSONDecodeError, ValueError):
                    existing_msg_count = -1  # corrupt → always back up
                incoming_msg_count = len(self.messages or [])
                if (
                    existing_msg_count > 0
                    and incoming_msg_count == 0
                    and (self.active_stream_id or self.pending_user_message)
                    and not _admission_compensation
                ):
                    logger.warning(
                        "refusing to overwrite session %s messages with empty active/pending snapshot "
                        "(existing=%s, incoming=%s, stream=%s)",
                        self.session_id,
                        existing_msg_count,
                        incoming_msg_count,
                        self.active_stream_id,
                    )
                    return
                if existing_msg_count > incoming_msg_count and not _admission_compensation:
                    bak_path = self.path.with_suffix('.json.bak')
                    # SHOULD-FIX #2 (Opus): atomic write via tmp+replace,
                    # mirroring the main save() pattern below. Prevents a
                    # torn .bak from a crash mid-write or a concurrent
                    # backup-producing save. Recovery defends against a
                    # torn .bak (JSONDecodeError → no_action), so the
                    # failure mode pre-fix was "backup is lost"; with
                    # this fix the backup either lands cleanly or doesn't
                    # land at all.
                    try:
                        bak_tmp = bak_path.with_suffix(
                            f'.bak.tmp.{os.getpid()}.{threading.current_thread().ident}'
                        )
                        with open(bak_tmp, 'w', encoding='utf-8') as bf:
                            bf.write(existing_text)
                            bf.flush()
                            os.fsync(bf.fileno())
                        _safe_replace(bak_tmp, bak_path)
                    except OSError:
                        # Backup is best-effort; main save proceeds regardless.
                        try:
                            bak_tmp.unlink(missing_ok=True)
                        except Exception:
                            pass
        except OSError:
            pass

        tmp = self.path.with_suffix(f'.tmp.{os.getpid()}.{threading.current_thread().ident}')
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            _safe_replace(tmp, self.path)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            raise
        if not skip_index:
            _write_session_index(updates=[self])

        # #4985 belt-and-suspenders self-heal: a successful save with at
        # least one real message on the sidecar is unconditional proof the
        # row is alive (the #4985 "zero-message orphan" only ever exists
        # when ``len(self.messages) == 0``). Clear the tombstone so the
        # next ``/api/sessions`` poll does not need the prune helper to
        # run before the row re-appears — useful when the message-commit
        # happens on a poll that does not yet see state.db.messages rows
        # (e.g. the WebUI's own sidecar commit lands before the agent's
        # state.db append, or the helper is skipped via a different code
        # path). Wrapped because a tombstone failure must never block a
        # save. The helper's self-healing branch in
        # ``_prune_orphaned_webui_zero_message_sessions`` is the primary
        # fix; this is the belt.
        if self.messages:
            try:
                _clear_webui_zero_message_orphan_tombstone(self.session_id)
                _clear_webui_deleted_session_tombstone(self.session_id)
            except Exception:
                logger.debug(
                    "Failed to clear webui tombstone for %s",
                    self.session_id,
                    exc_info=True,
                )

    @classmethod
    def load(cls, sid):
        # Validate session ID format to prevent path traversal.  API/gateway
        # session ids may contain hyphens (for example ``api-*`` and
        # ``reachy-voice-*``); allow those but still reject dots/slashes.
        if not is_safe_session_id(sid):
            return None
        p = SESSION_DIR / f'{sid}.json'
        if not p.exists():
            return None
        # #5854: snapshot the stat signature BEFORE reading so a legacy-facts
        # cache write is only committed if the file didn't change under us
        # during the parse (TOCTOU guard against an atomic replace mid-read).
        _pre_read_sig = _sidecar_stat_signature(p)
        data = json.loads(p.read_text(encoding='utf-8'))
        data['messages'], _collapsed_partials = _collapse_adjacent_duplicate_partials(data.get('messages'))
        session = cls(**data)
        if _collapsed_partials:
            try:
                # Self-heal bloated sessions on first full load without touching
                # recency/index ordering; save() creates a .bak because this
                # intentionally shrinks the transcript (#2592).
                session.save(touch_updated_at=False, skip_index=True)
            except Exception:
                logger.debug("Failed to persist collapsed duplicate partials for %s", sid, exc_info=True)
        else:
            # #5854: for a LEGACY sidecar (no modern anchor_scene_index key), the
            # cheap metadata-prefix read cannot recover message_count/scenes when
            # scenes serialize before them, so cache the authoritative facts we
            # just parsed. This keeps the metadata-only path and the eviction
            # check from full-parsing this unchanged file again on every poll.
            # Keyed by stat signature, so any edit invalidates it; the next
            # save() rewrites the modern layout and the fallback stops firing.
            # expected_sig guards against an atomic replace during the read.
            # (When _collapsed_partials fired, save() above already rewrote the
            # modern layout, so no legacy caching is needed.)
            if 'anchor_scene_index' not in data:
                try:
                    _legacy_sidecar_facts_put(
                        sid,
                        len(getattr(session, 'messages', None) or []),
                        _anchor_scene_index_from_records(getattr(session, 'anchor_activity_scenes', None)),
                        expected_sig=_pre_read_sig,
                    )
                except Exception:
                    logger.debug("legacy sidecar facts cache populate failed for %s", sid, exc_info=True)
        return session

    @classmethod
    def load_metadata_only(cls, sid, *, index_message_counts=None):
        """Load only the compact metadata fields, skipping the messages array.

        Session JSON files have metadata fields (session_id, title, model, etc.)
        at the top level, before the large messages array. Read only up to the
        top-level "messages" field and synthesize a small metadata-only object.
        Falls back to load() for legacy or unexpected file layouts.
        """
        # Same path-safety contract as load(): hyphens are valid session ids,
        # path separators and traversal dots are not.
        if not is_safe_session_id(sid):
            return None
        p = SESSION_DIR / f'{sid}.json'
        if not p.exists():
            return None
        try:
            prefix = _read_metadata_json_prefix(p)
            if not prefix:
                return cls.load(sid)
            parsed = json.loads(prefix)
            needed = {'session_id', 'title', 'created_at', 'updated_at'}
            if not needed.issubset(parsed.keys()):
                return cls.load(sid)
            parsed['messages'] = []
            parsed['tool_calls'] = []
            session = cls(**parsed)
            sidecar_message_count = _parse_nonnegative_int(parsed.get('message_count'))
            index_message_count = None
            if sidecar_message_count is None:
                if index_message_counts is not None:
                    index_message_count = index_message_counts.get(str(sid))
                else:
                    index_message_count = _lookup_index_message_count(sid)
            # #5854 legacy-layout recovery: a pre-#5854 sidecar serialized
            # anchor_activity_scenes BEFORE message_count, so on a large-scene
            # legacy file the cheap prefix now stops at the scenes key and
            # captures NO message_count. The sidebar _index.json count can lag
            # behind external sidecar appends, so trusting it here would report a
            # stale/zero count and could drop an unsaved user tail on the next
            # get_session cache-replace. When the prefix carries NEITHER
            # message_count NOR the modern anchor_scene_index key (⇒ a legacy
            # file whose count fell after the scenes), recover the authoritative
            # facts. To avoid re-parsing an unchanged legacy file on every poll
            # (which would recreate the #4633 churn for legacy sidecars that are
            # never re-saved), consult a bounded stat-signature cache first and
            # only full-load on a miss, caching the result. The next save()
            # rewrites the modern layout so the fallback stops firing entirely.
            # A MODERN file always carries message_count in the prefix, so it
            # never reaches here — a genuine 0 stays 0.
            if (
                sidecar_message_count is None
                and 'anchor_scene_index' not in parsed
            ):
                _facts = _legacy_sidecar_facts_get(sid)
                if _facts is not None:
                    parsed['anchor_scene_index'] = _facts.get('scene_index') or {}
                    session = cls(**parsed)
                    session._metadata_message_count = _parse_nonnegative_int(_facts.get('message_count'))
                    session._loaded_metadata_only = True
                    return session
                # Cache miss → full-load. cls.load() itself populates the legacy
                # facts cache with a TOCTOU-guarded write (expected_sig), so we
                # do NOT re-cache here (an unguarded second write could stamp
                # stale facts under a replacement file's signature — Codex r5).
                return cls.load(sid)
            # Modern sidecars carry an accurate message_count, so it is the
            # source of truth and we skip the per-row _index.json read in the
            # common case. The sidebar index is only a cache (it can lag behind
            # external sidecar appends/backfills), so consult it solely as a
            # fallback when the sidecar has no count. When both are present we
            # still take the largest known count as a defensive measure.
            known_counts = [
                count for count in (index_message_count, sidecar_message_count)
                if count is not None
            ]
            session._metadata_message_count = max(known_counts) if known_counts else None
            # Mark this session as a metadata-only stub. save() refuses to write
            # such a session because doing so would atomically replace the
            # on-disk JSON with messages=[], wiping the conversation. Any
            # caller that needs to mutate persisted state on a metadata-only
            # session must reload it with metadata_only=False first.
            # See #1558 — v0.50.279 _clear_stale_stream_state() data-loss bug.
            session._loaded_metadata_only = True
            return session
        except Exception:
            # Corrupt prefix or decode error — fall back to full load
            return cls.load(sid)


class _SessionProjectionMixin:
    @staticmethod
    def _compute_user_message_count(messages) -> int:
        """perf(session-load-latency) Priority 1: bounded in-memory count.

        Returns the number of messages with role='user' in ``messages``.
        Pre-patch compact() did the same O(N) walk inline; the walk is
        extracted here so it can be measured and bounded independently.

        On the test corpus (a 2,400-message sidecar) this walk runs in
        tens of milliseconds on a Celeron N3350 with eMMC. Cost is
        proportional to the sidecar length the caller already loaded, not
        to anything new we read from disk.

        Critical: this walks ``messages`` (the sidecar) and NOT state.db.
        A previous version of this helper queried state.db for the same
        count, but the two sources can diverge by hundreds of messages
        during recovery / mid-flight writes / pending_user_message, and
        the sidebar's stale-row detection (see
        ``_looks_like_stale_zero_message_row`` and
        ``_row_may_need_sidecar_metadata_refresh``) consumes this field as
        if the sidecar were the source of truth. Mixing the two sources
        would silently flip the field's semantics.
        """
        if not isinstance(messages, list):
            return 0
        n = 0
        for m in messages:
            if isinstance(m, dict):
                # Inline role check to avoid the _message_role helper call
                # on every iteration. dict.get('role') with default '' is
                # materially faster than a function call for the hot loop.
                role = m.get('role')
                if isinstance(role, str) and role == 'user':
                    n += 1
        return n

    def compact(self, include_runtime=False, active_stream_ids=None) -> dict:
        active_stream_ids = active_stream_ids if active_stream_ids is not None else set()
        has_pending_user_message = bool(self.pending_user_message)
        message_count = (
            self._metadata_message_count
            if self._metadata_message_count is not None
            else len(self.messages)
        )
        if has_pending_user_message:
            message_count = max(message_count, 1)
        last_message_at = _last_message_timestamp(self.messages) or self.updated_at
        if has_pending_user_message and self.pending_started_at:
            last_message_at = self.pending_started_at
        return {
            'session_id': self.session_id,
            'title': self.title,
            'workspace': self.workspace,
            'model': self.model,
            'model_provider': self.model_provider,
            'message_count': message_count,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'last_message_at': last_message_at,
            'pinned': self.pinned,
            'archived': self.archived,
            'project_id': self.project_id,
            'profile': self.profile,
            'input_tokens': self.input_tokens,
            'output_tokens': self.output_tokens,
            'estimated_cost': self.estimated_cost,
            'cache_read_tokens': self.cache_read_tokens,
            'cache_write_tokens': self.cache_write_tokens,
            'cache_hit_percent': prompt_cache_hit_percent(self.cache_read_tokens, self.input_tokens),
            'personality': self.personality,
            'compression_anchor_visible_idx': self.compression_anchor_visible_idx,
            'compression_anchor_message_key': self.compression_anchor_message_key,
            'compression_anchor_summary': self.compression_anchor_summary,
            'pre_compression_snapshot': self.pre_compression_snapshot,
            'context_engine': self.context_engine,
            'compression_anchor_engine': self.compression_anchor_engine,
            'compression_anchor_mode': self.compression_anchor_mode,
            'compression_anchor_details': self.compression_anchor_details,
            'context_engine_state': self.context_engine_state,
            'context_length': self.context_length,
            'threshold_tokens': self.threshold_tokens,
            'last_prompt_tokens': self.last_prompt_tokens,
            'post_compression_context_tokens_estimate': self.post_compression_context_tokens_estimate,
            'compression_recovery': self.compression_recovery,
            'recommended_recovery_action': self.recommended_recovery_action,
            'gateway_routing': self.gateway_routing,
            'gateway_routing_history': self.gateway_routing_history,
            'manual_title': self.manual_title,
            # Only emit 'parent_session_id' when set (the /branch fork link, #1342).
            # Sessions without a fork must not leak None — see test_session_lineage_metadata_api.
            **({'parent_session_id': self.parent_session_id} if self.parent_session_id else {}),
            **({
                'compression_recovery_source_session_id': self.compression_recovery_source_session_id,
                'compression_recovery_action': self.compression_recovery_action,
            } if (self.compression_recovery_source_session_id or self.compression_recovery_action) else {}),
            **({
                'worktree_path': self.worktree_path,
                'worktree_branch': self.worktree_branch,
                'worktree_repo_root': self.worktree_repo_root,
                'worktree_created_at': self.worktree_created_at,
            } if self.worktree_path else {}),
            'user_message_count': Session._compute_user_message_count(self.messages),
            'active_stream_id': self.active_stream_id,
            'pending_user_message': self.pending_user_message,
            'has_pending_user_message': has_pending_user_message,
            'is_cli_session': self.is_cli_session,
            'source_tag': self.source_tag,
            'raw_source': self.raw_source,
            'session_source': self.session_source,
            'source_label': self.source_label,
            'read_only': self.read_only,
            'enabled_toolsets': self.enabled_toolsets,
            'composer_draft': self.composer_draft if isinstance(self.composer_draft, dict) else {},
            'process_wakeup_pause': self.process_wakeup_pause if isinstance(self.process_wakeup_pause, dict) else {},
            'share_token': self.share_token,
            'share_created_at': self.share_created_at,
            'is_streaming': _is_streaming_session(
                self.active_stream_id, active_stream_ids
            ) if include_runtime else False,
        }


class Session(_SessionPersistenceMixin, _SessionProjectionMixin):
    def __init__(self, session_id: str=None, title: str='Untitled',
                 workspace=str(DEFAULT_WORKSPACE), model=DEFAULT_MODEL,
                 model_provider=None,
                 messages=None, created_at=None, updated_at=None,
                 tool_calls=None, pinned: bool=False, archived: bool=False,
                 project_id: str=None, profile=None,
                 input_tokens: int=0, output_tokens: int=0, estimated_cost=None,
                 cache_read_tokens: int=0, cache_write_tokens: int=0,
                 personality=None,
                 active_stream_id: str=None,
                 pending_user_message: str=None,
                 pending_attachments=None,
                 pending_started_at=None,
                 pending_user_source: str=None,
                 context_messages=None,
                 compression_anchor_visible_idx=None,
                 compression_anchor_message_key=None,
                 compression_anchor_summary=None,
                 pre_compression_snapshot: bool=False,
                 context_engine=None,
                 compression_anchor_engine=None,
                 compression_anchor_mode=None,
                 compression_anchor_details=None,
                 context_engine_state=None,
                 context_length=None, threshold_tokens=None,
                 last_prompt_tokens=None,
                 post_compression_context_tokens_estimate=None,
                 compression_recovery=None,
                 recommended_recovery_action=None,
                 compression_recovery_source_session_id=None,
                 compression_recovery_action=None,
                 truncation_watermark=None,
                 truncation_boundary=None,
                 clear_generation=None,
                 gateway_routing=None, gateway_routing_history=None,
                 llm_title_generated: bool=False,
                 manual_title: bool=False,
                parent_session_id: str=None,
                worktree_path=None,
                worktree_branch=None,
                 worktree_repo_root=None,
                 worktree_created_at=None,
                 enabled_toolsets=None,
                 composer_draft=None,
                 anchor_activity_scenes=None,
                 process_wakeup_pause=None,
                 share_token=None,
                 share_created_at=None,
                 **kwargs):
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.title = title
        self.workspace = str(Path(workspace).expanduser().resolve())
        self.model = model
        self.model_provider = str(model_provider).strip().lower() if model_provider else None
        # #5979: signature of the model the user DELIBERATELY picked this session
        # (``"<model>\x1f<provider>"``), or None. Used by the streaming resolver
        # to preserve a custom-proxy vendor namespace on a COLD catalog ONLY when
        # the current routing context still matches what was picked. Storing a
        # SIGNATURE (not a bare bool) means any later model/provider change — via
        # /api/chat/start, /api/session/update, normalization, or provider repair
        # — automatically invalidates the pick (the signatures no longer match),
        # so a stale first-party leftover (#433) is never wrongly preserved.
        # Restored from persisted metadata on load (arrives via **kwargs).
        self.model_explicit_pick_signature = kwargs.get('model_explicit_pick_signature') or None
        self.messages = messages or []
        self.tool_calls = tool_calls or []
        self.created_at = created_at or time.time()
        self.updated_at = updated_at or time.time()
        self.pinned = bool(pinned)
        self.archived = bool(archived)
        self.project_id = project_id or None
        self.profile = profile
        self.input_tokens = input_tokens or 0
        self.output_tokens = output_tokens or 0
        self.estimated_cost = estimated_cost
        self.cache_read_tokens = cache_read_tokens or 0
        self.cache_write_tokens = cache_write_tokens or 0
        self.personality = personality
        self.active_stream_id = active_stream_id
        self.pending_user_message = pending_user_message
        self.pending_attachments = pending_attachments or []
        self.pending_started_at = pending_started_at
        self.pending_user_source = pending_user_source
        self.context_messages = context_messages if isinstance(context_messages, list) else []
        self.compression_anchor_visible_idx = compression_anchor_visible_idx
        self.compression_anchor_message_key = compression_anchor_message_key
        self.compression_anchor_summary = compression_anchor_summary
        self.pre_compression_snapshot = bool(pre_compression_snapshot)
        self.context_engine = context_engine
        self.compression_anchor_engine = compression_anchor_engine
        self.compression_anchor_mode = compression_anchor_mode
        self.compression_anchor_details = compression_anchor_details if isinstance(compression_anchor_details, dict) else {}
        self.context_engine_state = context_engine_state if isinstance(context_engine_state, dict) else {}
        self.context_length = context_length
        self.threshold_tokens = threshold_tokens
        self.last_prompt_tokens = last_prompt_tokens
        _post_compression_tokens = _parse_nonnegative_int(post_compression_context_tokens_estimate)
        self.post_compression_context_tokens_estimate = (
            _post_compression_tokens if _post_compression_tokens and _post_compression_tokens > 0 else None
        )
        self.compression_recovery = compression_recovery if isinstance(compression_recovery, dict) else {}
        self.recommended_recovery_action = recommended_recovery_action
        self.compression_recovery_source_session_id = (
            str(compression_recovery_source_session_id).strip()
            if compression_recovery_source_session_id
            else None
        )
        self.compression_recovery_action = (
            str(compression_recovery_action).strip()
            if compression_recovery_action
            else None
        )
        self.truncation_watermark = truncation_watermark
        self.truncation_boundary = truncation_boundary
        self.clear_generation = clear_generation
        self.gateway_routing = gateway_routing if isinstance(gateway_routing, dict) else None
        self.gateway_routing_history = gateway_routing_history if isinstance(gateway_routing_history, list) else []
        self.llm_title_generated = bool(llm_title_generated)
        self.manual_title = bool(manual_title)
        self.parent_session_id = parent_session_id
        self.worktree_path = str(Path(worktree_path).expanduser().resolve()) if worktree_path else None
        self.worktree_branch = str(worktree_branch) if worktree_branch else None
        self.worktree_repo_root = str(Path(worktree_repo_root).expanduser().resolve()) if worktree_repo_root else None
        self.worktree_created_at = worktree_created_at
        self.is_cli_session = bool(kwargs.get('is_cli_session', False))
        self.source_tag = kwargs.get('source_tag')
        self.raw_source = kwargs.get('raw_source')
        self.session_source = kwargs.get('session_source')
        self.source_label = kwargs.get('source_label')
        self.user_id = kwargs.get('user_id')
        self.chat_id = kwargs.get('chat_id')
        self.chat_type = kwargs.get('chat_type')
        self.thread_id = kwargs.get('thread_id')
        self.session_key = kwargs.get('session_key')
        self.platform = kwargs.get('platform')
        self.read_only = bool(kwargs.get('read_only', False))
        self.enabled_toolsets = enabled_toolsets  # List[str] or None — per-session toolset override
        self.composer_draft = composer_draft if isinstance(composer_draft, dict) else {}
        self.anchor_activity_scenes = anchor_activity_scenes if isinstance(anchor_activity_scenes, dict) else {}
        self.process_wakeup_pause = process_wakeup_pause if isinstance(process_wakeup_pause, dict) else {}
        self.share_token = str(share_token).strip() if share_token else None
        self.share_created_at = share_created_at
        # #5854: a compact fingerprint of anchor_activity_scenes ({scene_key:
        # updated_at}) persisted BEFORE the messages array so the sidebar-poll
        # freshness check can compare scene freshness without parsing the full
        # (often 250-480KB) scene bodies, which serialize AFTER messages. None
        # on legacy sidecars (scenes-before-messages, no fingerprint) — callers
        # fall back to reading keys/updated_at off anchor_activity_scenes.
        _raw_scene_index = kwargs.get('anchor_scene_index')
        self._anchor_scene_index = _raw_scene_index if isinstance(_raw_scene_index, dict) else None
        raw_message_count = kwargs.get('message_count')
        parsed_message_count = None
        if raw_message_count is not None:
            try:
                parsed_message_count = int(raw_message_count)
            except (TypeError, ValueError):
                parsed_message_count = None
        self._metadata_message_count = parsed_message_count if parsed_message_count is not None and parsed_message_count >= 0 else None


def _anchor_scene_index_from_records(records) -> dict:
    """Build the compact anchor-scene fingerprint {scene_key: updated_at} (#5854).

    This is the freshness signal the sidebar-poll comparison needs — scene keys
    plus each scene's ``updated_at`` — WITHOUT the 250-480KB bodies. Persisted in
    the metadata prefix (before ``messages``) so ``load_metadata_only`` and
    ``_persisted_session_meta_prefix`` stay cheap. Mirrors exactly what
    ``_anchor_scene_record_keys`` / ``_anchor_scene_records_updated_at`` read off
    the full records, so the fingerprint comparison is behavior-identical.
    """
    if not isinstance(records, dict):
        return {}
    index = {}
    for key, value in records.items():
        if not key or not isinstance(value, dict):
            continue
        try:
            updated_at = float(value.get('updated_at') or 0)
        except (TypeError, ValueError):
            updated_at = 0.0
        index[str(key)] = updated_at
    return index


def _disk_scene_fingerprint(disk_meta_prefix: dict):
    """Resolve the (scene_keys, max_updated_at) freshness signal from a parsed
    metadata prefix dict, preferring the modern ``anchor_scene_index`` and
    falling back to the full ``anchor_activity_scenes`` bodies for legacy files.

    Returns ``None`` when the prefix carries NEITHER field, so callers can tell
    "no scenes" (empty dict/index present) apart from "couldn't determine"
    (legacy file whose scenes serialize after ``messages`` and so aren't in the
    prefix) and fall through to the full metadata load instead of assuming zero.
    """
    if not isinstance(disk_meta_prefix, dict):
        return None
    if 'anchor_scene_index' in disk_meta_prefix:
        raw = disk_meta_prefix.get('anchor_scene_index')
        raw = raw if isinstance(raw, dict) else {}
        keys = {str(k) for k in raw}
        latest = 0.0
        for v in raw.values():
            try:
                fv = float(v or 0)
            except (TypeError, ValueError):
                fv = 0.0
            if fv > latest:
                latest = fv
        return keys, latest
    if 'anchor_activity_scenes' in disk_meta_prefix:
        records = disk_meta_prefix.get('anchor_activity_scenes')
        records = records if isinstance(records, dict) else {}
        keys = {str(k) for k, val in records.items() if k and isinstance(val, dict)}
        latest = 0.0
        for val in records.values():
            if not isinstance(val, dict):
                continue
            try:
                fv = float(val.get('updated_at') or 0)
            except (TypeError, ValueError):
                fv = 0.0
            if fv > latest:
                latest = fv
        return keys, latest
    return None


def _sidecar_stat_signature(path):
    """Stat signature for a sidecar path, or None if it can't be stat'd.

    Any edit (atomic-rename or in-place) changes at least one component, so a
    cached entry keyed by this signature is auto-invalidated on the next write.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    return (str(path), int(getattr(st, 'st_mtime_ns', int(st.st_mtime * 1_000_000_000))),
            int(st.st_size), int(getattr(st, 'st_ctime_ns', int(st.st_ctime * 1_000_000_000))))


def _legacy_sidecar_facts_get(sid):
    """Return cached authoritative facts for a LEGACY sidecar, or None (#5854).

    Only returns a hit when the file's current stat signature matches the cached
    one, so a stale entry can never be served after an edit.
    """
    if not is_safe_session_id(sid):
        return None
    sig = _sidecar_stat_signature(SESSION_DIR / f'{sid}.json')
    if sig is None:
        return None
    with _LEGACY_SIDECAR_FACTS_LOCK:
        hit = _LEGACY_SIDECAR_FACTS.get(sig)
        if hit is not None:
            _LEGACY_SIDECAR_FACTS.move_to_end(sig)
            return dict(hit)
    return None


def _legacy_sidecar_facts_put(sid, message_count, scene_index, *, expected_sig):
    """Cache authoritative facts for a legacy sidecar keyed by its stat signature.

    #5854 TOCTOU guard: ``expected_sig`` (MANDATORY) is the signature captured
    BEFORE the caller parsed the file. The facts were derived from that snapshot,
    so we only cache when the file's CURRENT signature still equals it —
    otherwise the file was atomically replaced during the parse and these facts
    describe the old content; caching them under the new signature would serve
    stale data. Pass ``None`` explicitly only if the caller genuinely has no
    snapshot (then this is a no-op, refusing to cache unverified facts).
    """
    if not is_safe_session_id(sid):
        return
    if expected_sig is None:
        return
    sig = _sidecar_stat_signature(SESSION_DIR / f'{sid}.json')
    if sig is None:
        return
    if sig != expected_sig:
        # File changed under us during the parse — do not cache stale facts.
        return
    entry = {"message_count": message_count,
             "scene_index": dict(scene_index) if isinstance(scene_index, dict) else {}}
    with _LEGACY_SIDECAR_FACTS_LOCK:
        _LEGACY_SIDECAR_FACTS[sig] = entry
        _LEGACY_SIDECAR_FACTS.move_to_end(sig)
        while len(_LEGACY_SIDECAR_FACTS) > _LEGACY_SIDECAR_FACTS_MAX:
            _LEGACY_SIDECAR_FACTS.popitem(last=False)


# Load-time normalization belongs to the serialized record owner.
def _normalize_journal_recovery_text(value) -> str:
    return " ".join(str(value or "").split())


def _message_matches_pending_checkpoint(message, pending_text, timestamp, source, attachments):
    if not isinstance(message, dict) or message.get('role') != 'user':
        return False
    try:
        message_timestamp = int(message.get('timestamp'))
        expected_timestamp = int(timestamp)
    except (TypeError, ValueError):
        return False
    return (
        _normalize_journal_recovery_text(message.get('content'))
        == _normalize_journal_recovery_text(pending_text)
        and message_timestamp == expected_timestamp
        and (message.get('_source') or 'webui') == (source or 'webui')
        and list(message.get('attachments') or []) == list(attachments or [])
    )


def _message_matches_pending_text(message, pending_text):
    if not isinstance(message, dict) or message.get('role') != 'user':
        return False
    return (
        _normalize_journal_recovery_text(message.get('content'))
        == _normalize_journal_recovery_text(pending_text)
    )


def _latest_user_matches_pending_text(messages, pending_text):
    if not isinstance(messages, list) or not pending_text:
        return False
    for message in reversed(messages):
        if isinstance(message, dict) and message.get('role') == 'user':
            return _message_matches_pending_text(message, pending_text)
    return False


def _partial_message_signature(message: dict) -> tuple:
    """Return a stable identity for partial assistant markers recovered on load."""
    if not isinstance(message, dict):
        return ('', '', ())
    tool_sig = []
    for tool_call in message.get('_partial_tool_calls') or []:
        if not isinstance(tool_call, dict):
            continue
        try:
            args_sig = json.dumps(
                tool_call.get('args') or {},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        except Exception:
            args_sig = str(tool_call.get('args') or '')
        tool_sig.append((
            str(tool_call.get('name') or ''),
            args_sig,
            bool(tool_call.get('done', False)),
            bool(tool_call.get('is_error', False)),
            str(tool_call.get('preview') or tool_call.get('snippet') or ''),
        ))
    return (
        str(message.get('content') or '').strip(),
        str(message.get('reasoning') or '').strip(),
        tuple(tool_sig),
    )


def _collapse_adjacent_duplicate_partials(messages) -> tuple[list, bool]:
    """Collapse repeated identical partial markers from the same failed turn."""
    if not isinstance(messages, list):
        return messages, False
    collapsed = []
    changed = False
    previous_partial_sig = None
    for message in messages:
        if isinstance(message, dict) and message.get('_partial'):
            sig = _partial_message_signature(message)
            if previous_partial_sig == sig:
                changed = True
                continue
            previous_partial_sig = sig
        else:
            previous_partial_sig = None
        collapsed.append(message)
    return collapsed, changed
