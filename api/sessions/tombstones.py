"""Durable tombstones that prevent deleted or pruned sessions from resurfacing."""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from .atomic_io import safe_replace

logger = logging.getLogger(__name__)
_WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_LOCK = threading.Lock()
_WEBUI_DELETED_SESSION_TOMBSTONE_LOCK = threading.Lock()

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


def zero_message_orphan_tombstone_file(session_dir: Path) -> Path:
    """Return the current tombstone file path.

    Resolved at call time (not module load) so tests that monkeypatch
    ``SESSION_DIR`` (e.g. ``_real_pipeline``) get a per-test path without
    having to also rewrite the module-level constant. Mirrors how
    ``SESSION_INDEX_FILE`` is computed but resolved at call time so the
    real path tracks the live ``SESSION_DIR``.
    """
    return session_dir / "_pruned_webui_orphans.json"


def load_zero_message_orphan_ids(session_dir: Path) -> frozenset[str]:
    """Return sids we've explicitly pruned as webui zero-message orphans.

    Degrades to ``frozenset()`` on any read error, missing file, version
    mismatch, or schema mismatch so the recovery path never accidentally
    admits a row that should stay tombstoned.
    """
    p = zero_message_orphan_tombstone_file(session_dir)
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


def save_zero_message_orphan_ids(session_dir: Path, ids) -> None:
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
    p = zero_message_orphan_tombstone_file(session_dir)
    _tmp = None
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        _tmp = p.with_suffix(
            f'.tmp.{os.getpid()}.{threading.current_thread().ident}'
        )
        with open(_tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        safe_replace(_tmp, p)
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


def record_zero_message_orphan(session_dir: Path, sid: str) -> None:
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
        current = set(load_zero_message_orphan_ids(session_dir))
        if sid in current:
            return
        current.add(sid)
        save_zero_message_orphan_ids(session_dir, current)


def clear_zero_message_orphan(session_dir: Path, sid: str) -> None:
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
        current = set(load_zero_message_orphan_ids(session_dir))
        if sid not in current:
            return
        current.discard(sid)
        if current:
            save_zero_message_orphan_ids(session_dir, current)
            return
        try:
            zero_message_orphan_tombstone_file(session_dir).unlink(missing_ok=True)
        except Exception:
            logger.debug(
                "Failed to remove empty webui zero-message orphan tombstone",
                exc_info=True,
            )


def deleted_session_tombstone_file(session_dir: Path) -> Path:
    return session_dir / "_deleted_webui_sessions.json"


def load_deleted_session_ids(session_dir: Path) -> frozenset[str]:
    p = deleted_session_tombstone_file(session_dir)
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


def save_deleted_session_ids(session_dir: Path, ids) -> None:
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
    p = deleted_session_tombstone_file(session_dir)
    _tmp = None
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        _tmp = p.with_suffix(
            f'.tmp.{os.getpid()}.{threading.current_thread().ident}'
        )
        with open(_tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        safe_replace(_tmp, p)
    except Exception:
        logger.debug("Failed to save webui deleted-session tombstone", exc_info=True)
        if _tmp is not None:
            try:
                _tmp.unlink(missing_ok=True)
            except Exception:
                pass


def record_deleted_session(session_dir: Path, sid: str) -> None:
    sid = str(sid or "").strip()
    if not sid:
        return
    with _WEBUI_DELETED_SESSION_TOMBSTONE_LOCK:
        current = set(load_deleted_session_ids(session_dir))
        if sid in current:
            return
        current.add(sid)
        save_deleted_session_ids(session_dir, current)


def clear_deleted_session(session_dir: Path, sid: str) -> None:
    sid = str(sid or "").strip()
    if not sid:
        return
    with _WEBUI_DELETED_SESSION_TOMBSTONE_LOCK:
        current = set(load_deleted_session_ids(session_dir))
        if sid not in current:
            return
        current.discard(sid)
        if current:
            save_deleted_session_ids(session_dir, current)
            return
        try:
            deleted_session_tombstone_file(session_dir).unlink(missing_ok=True)
        except Exception:
            logger.debug("Failed to remove empty webui deleted-session tombstone", exc_info=True)
