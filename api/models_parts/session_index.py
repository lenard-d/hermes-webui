"""Session index snapshots, rebuilds, and pruning.

Loaded behind :mod:`api.models`; use that compatibility facade in callers.
"""
# The facade seeds the original monolith namespace dynamically.  Pyflakes cannot
# see those names, while the copied implementation intentionally keeps its old
# global lookups so api.models monkeypatch seams remain effective.
# ruff: noqa: F401, F811, F821, F841, B007, B023, B904, B905
from api.models_parts._compat import seed_module_globals

seed_module_globals(globals())



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
    _publish_models_global(
        globals(), "_PERSISTED_SESSION_IDS_CACHE", _PERSISTED_SESSION_IDS_CACHE
    )
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
                _publish_models_global(globals(), "_SESSION_INDEX_REBUILD_THREAD", None)
                _publish_models_global(globals(), "_SESSION_INDEX_REBUILD_THREAD_TARGET", None)


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
        _publish_models_global(
            globals(), "_SESSION_INDEX_REBUILD_THREAD_TARGET", _SESSION_INDEX_REBUILD_THREAD_TARGET
        )
        _publish_models_global(
            globals(), "_SESSION_INDEX_REBUILD_THREAD", _SESSION_INDEX_REBUILD_THREAD
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

__all__ = ['_cleanup_stale_tmp_files', '_PERSISTED_SESSION_IDS_CACHE', '_persisted_session_ids_snapshot', '_session_dir_has_persisted_session_files', '_rebuild_session_index_background', '_start_session_index_rebuild_thread', '_index_entry_exists', '_write_session_index', 'prune_session_from_index', 'WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_CAP', 'WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_VERSION', 'WEBUI_DELETED_SESSION_TOMBSTONE_CAP', 'WEBUI_DELETED_SESSION_TOMBSTONE_VERSION']
