"""Deleted and zero-message session tombstones.

Loaded behind :mod:`api.models`; use that compatibility facade in callers.
"""
# The facade seeds the original monolith namespace dynamically.  Pyflakes cannot
# see those names, while the copied implementation intentionally keeps its old
# global lookups so api.models monkeypatch seams remain effective.
# ruff: noqa: F401, F811, F821, F841, B007, B023, B904, B905
from api.models_parts._compat import seed_module_globals

seed_module_globals(globals())

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

__all__ = ['_webui_zero_message_orphan_tombstone_file', '_load_webui_zero_message_orphan_tombstone', '_save_webui_zero_message_orphan_tombstone', '_record_webui_zero_message_orphan_tombstone', '_clear_webui_zero_message_orphan_tombstone', '_webui_deleted_session_tombstone_file', '_load_webui_deleted_session_tombstone', '_save_webui_deleted_session_tombstone', '_record_webui_deleted_session_tombstone', '_clear_webui_deleted_session_tombstone']
