"""Backup inspection and conservative sidecar restoration."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import threading
from pathlib import Path
from .recovery_deletion import _durable_tombstone_marks_deleted_webui_session

logger = logging.getLogger(__name__)


def _msg_count(p: Path) -> int:
    """Return the number of messages in a session JSON file, or -1 on read/parse error.

    Returns -1 for any non-session-shape file:
    - File can't be read (OSError)
    - Top-level isn't valid JSON or is invalid (JSONDecodeError, ValueError)
    - Top-level isn't a dict (AttributeError on .get) — e.g. ``_index.json``
      which is a top-level list of session metadata, not a session itself.
      The startup recovery scanner globs ``*.json`` and would otherwise
      crash on the first non-dict file it encounters.
    """
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, ValueError):
        return -1
    if not isinstance(data, dict):
        return -1
    msgs = data.get('messages')
    return len(msgs) if isinstance(msgs, list) else -1


def _rebuild_recovery_session_index(session_dir: Path) -> None:
    """Rebuild ``session_dir/_index.json`` from persisted sidecars only.

    Recovery repair/audit operates on a concrete sidecar directory. Unlike the
    live sidebar path, its rebuilt index must not include unrelated in-memory
    ``Session`` cache entries whose backing JSON files are absent from this
    directory; those would immediately audit as ``index_missing_file`` rows.
    """
    from api.sessions.records import _load_session_from_path

    entry_map: dict[str, dict] = {}
    for path in sorted(session_dir.glob('*.json')):
        if path.name.startswith('_'):
            continue
        session = _load_session_from_path(path)
        if not session:
            continue
        entry = session.compact()
        session_id = entry.get('session_id')
        if not session_id:
            continue
        existing = entry_map.get(session_id)
        if existing is None or entry.get('message_count', 0) > existing.get('message_count', 0):
            entry_map[session_id] = entry

    entries = sorted(
        entry_map.values(),
        key=lambda entry: entry.get('updated_at', 0),
        reverse=True,
    )
    index_path = session_dir / '_index.json'
    tmp = index_path.with_suffix(f'.tmp.recovery.{os.getpid()}.{threading.current_thread().ident}')
    try:
        with open(tmp, 'w', encoding='utf-8') as fh:
            fh.write(json.dumps(entries, ensure_ascii=False, indent=2))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, index_path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _session_records_intentional_compress_shrink(session_path: Path) -> bool:
    """Return True when the live sidecar records an intentional context shrink.

    Manual ``/compress`` keeps the visible transcript but replaces the
    model-facing ``context_messages`` with a smaller compacted prefix. That
    operation must not be treated as accidental data loss by the #1558
    ``.bak`` safeguard or startup recovery (#4836).

    NOTE: this only reports *whether* the live session was intentionally
    compressed. It must NOT, on its own, decide to skip ``.bak`` recovery —
    a session can be manually compressed and *then* later suffer a genuine
    #1558 ``messages``-array loss. The caller pairs this with a freshness
    check (the backup must predate the compression) so a real post-compress
    loss is still recovered. See ``inspect_session_recovery_status``.
    """
    try:
        data = json.loads(session_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False

    context_messages = data.get('context_messages')
    messages = data.get('messages')
    has_shorter_context = (
        isinstance(context_messages, list)
        and isinstance(messages, list)
        and len(context_messages) < len(messages)
    )
    anchor_summary = str(data.get('compression_anchor_summary') or '').strip()
    anchor_key = data.get('compression_anchor_message_key')
    mode = str(data.get('compression_anchor_mode') or '').strip().lower()
    watermark = data.get('truncation_watermark')

    if mode == 'manual':
        return True
    if has_shorter_context and anchor_summary and anchor_key is not None:
        return True
    if has_shorter_context and watermark is not None:
        return True
    return False


def _backup_predates_intentional_shrink(session_path: Path, bak_path: Path) -> bool:
    """True when the ``.bak`` captured the PRE-compression transcript.

    The intentional-compress guard (#4836) must only suppress recovery for a
    backup whose restore would *undo* the user's deliberate shrink — i.e. a
    backup taken before ``/compress`` ran, still carrying the large
    UNCOMPRESSED ``context_messages``. A backup written *after* the
    compression (a later save that lost data) carries the ALREADY-COMPRESSED
    context and MUST still be recoverable.

    Time can't tell these apart (the ``.bak`` and main file are written in the
    same ``save()`` milliseconds apart), so we use the same content-semantic
    signal reconciliation uses: the compaction marker. A post-compression
    backup carries the ``[context compaction…]`` marker in its
    ``context_messages`` (it persists across saves); a pre-compression backup
    does not. We suppress recovery ONLY when the backup is genuinely
    pre-compression — its context lacks the compaction marker AND is larger
    than the live compressed context (the shrink hasn't been applied to it).
    If the backup already carries the marker, it post-dates the compression →
    a real loss → recover. The length clause is a secondary guard for the rare
    compression that emits no marker; whenever in doubt we fail OPEN (return
    False, allow recovery) — for a data-loss safeguard, recovering real data
    is always the safer error. Fail OPEN on any read/parse error too.
    """
    try:
        live = json.loads(session_path.read_text(encoding='utf-8'))
        bak = json.loads(bak_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(live, dict) or not isinstance(bak, dict):
        return False

    bak_ctx = bak.get('context_messages')
    bak_ctx = bak_ctx if isinstance(bak_ctx, list) else []

    # If the backup's context already carries the compaction marker, the backup
    # post-dates the compression (the marker persists across saves) → it is a
    # recoverable post-compression snapshot, never a shrink-undoing one.
    try:
        from api.sessions.reconciliation_context import _context_messages_include_compression_marker
        if _context_messages_include_compression_marker(bak_ctx):
            return False
    except Exception:
        # If the marker check is unavailable, fall through to the length guard.
        logger.debug("compaction-marker check unavailable in recovery", exc_info=True)

    live_ctx = live.get('context_messages')
    live_ctx_len = len(live_ctx) if isinstance(live_ctx, list) else 0
    bak_ctx_len = len(bak_ctx)

    # Secondary guard (unmarked compression): a pre-compression backup's context
    # is larger than the live compressed context (the shrink hasn't been applied
    # to it). A backup whose context is already <= the live compressed context
    # post-dates the compression and represents recoverable post-compress data.
    return bak_ctx_len > live_ctx_len


def _session_records_clear_sentinel(session_path: Path, bak_path: Path) -> bool:
    """Return True when the live sidecar records a provenanced clear sentinel.

    The live sidecar must carry the explicit /api/session/clear marker, and
    the backup must not carry the same marker. Same-generation backups stay
    recoverable; unreadable or partial matches fail open.
    """
    try:
        data = json.loads(session_path.read_text(encoding='utf-8'))
        bak = json.loads(bak_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(data, dict) or not isinstance(bak, dict):
        return False
    clear_generation = data.get('clear_generation')
    if not isinstance(clear_generation, str) or not clear_generation:
        return False
    if bak.get('clear_generation') == clear_generation:
        return False
    expected = {
        'messages': [],
        'context_messages': [],
        'truncation_watermark': 0.0,
        'truncation_boundary': 0.0,
        'active_stream_id': None,
        'pending_user_message': None,
        'pending_attachments': [],
        'pending_started_at': None,
        'pending_user_source': None,
    }
    for key, value in expected.items():
        if key not in data or data.get(key) != value:
            return False
    return True


def _live_supersedes_backup_by_clear_generation(session_path: Path, bak_path: Path) -> bool:
    """Return True when the live sidecar provably post-dates the backup via a
    clear sentinel even though the user has since sent NEW messages.

    Scope: this handles ONLY the post-clear-message case that the exact-empty
    sentinel (_session_records_clear_sentinel) can't. After /api/session/clear
    stamps a unique ``clear_generation`` and resets the truncation boundary to
    0.0, a pre-clear ``.json.bak`` is stale — restoring it would resurrect
    cleared history on top of the post-clear message. We require: live carries a
    ``clear_generation`` the backup lacks, live has a NON-EMPTY transcript, and
    the live boundary still shows the clear reset (watermark == boundary == 0.0).
    Empty clear-shaped sidecars stay governed by the exact-empty sentinel and its
    existing recovery semantics. Same-generation backups stay recoverable;
    unreadable/partial reads fail open (return False -> normal recovery), so a
    genuine crash-loss is never suppressed.
    """
    try:
        data = json.loads(session_path.read_text(encoding='utf-8'))
        bak = json.loads(bak_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(data, dict) or not isinstance(bak, dict):
        return False
    clear_generation = data.get('clear_generation')
    if not isinstance(clear_generation, str) or not clear_generation:
        return False
    if bak.get('clear_generation') == clear_generation:
        return False
    live_messages = data.get('messages')
    if not isinstance(live_messages, list) or len(live_messages) == 0:
        return False
    if data.get('truncation_watermark') != 0.0 or data.get('truncation_boundary') != 0.0:
        return False
    return True


def inspect_session_recovery_status(session_path: Path) -> dict:
    """Return a status dict describing whether recovery is recommended.

    {
      "session_id": "...",
      "live_messages": int,    # -1 if live file unreadable
      "bak_messages": int,     # -1 if no .bak or unreadable
      "recommend": "restore" | "no_action" | "no_backup",
    }
    """
    bak_path = session_path.with_suffix('.json.bak')
    live_count = _msg_count(session_path)
    if not bak_path.exists():
        return {
            "session_id": session_path.stem,
            "live_messages": live_count,
            "bak_messages": -1,
            "recommend": "no_backup",
        }
    bak_count = _msg_count(bak_path)
    if bak_count > live_count:
        if (
            _session_records_clear_sentinel(session_path, bak_path)
            or _live_supersedes_backup_by_clear_generation(session_path, bak_path)
        ):
            return {
                "session_id": session_path.stem,
                "live_messages": live_count,
                "bak_messages": bak_count,
                "recommend": "no_action",
                "intentional_clear_truncate": True,
            }
        if (
            _session_records_intentional_compress_shrink(session_path)
            and _backup_predates_intentional_shrink(session_path, bak_path)
        ):
            return {
                "session_id": session_path.stem,
                "live_messages": live_count,
                "bak_messages": bak_count,
                "recommend": "no_action",
                "intentional_compress_shrink": True,
            }
        return {
            "session_id": session_path.stem,
            "live_messages": live_count,
            "bak_messages": bak_count,
            "recommend": "restore",
        }
    return {
        "session_id": session_path.stem,
        "live_messages": live_count,
        "bak_messages": bak_count,
        "recommend": "no_action",
    }


def recover_session(session_path: Path) -> dict:
    """Restore session_path from its .bak when the bak has more messages.

    Returns a status dict identical to ``inspect_session_recovery_status``
    plus a "restored" boolean.
    """
    status = inspect_session_recovery_status(session_path)
    if status["recommend"] != "restore":
        return {**status, "restored": False}
    bak_path = session_path.with_suffix('.json.bak')
    # Stage the recovery via a tmp copy + atomic replace so a crash mid-restore
    # cannot leave a half-written session.json.
    tmp_path = session_path.with_suffix('.json.recover.tmp')
    try:
        shutil.copyfile(bak_path, tmp_path)
        tmp_path.replace(session_path)
    except OSError as exc:
        logger.warning("recover_session: copy failed for %s: %s", session_path, exc)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return {**status, "restored": False, "error": str(exc)}
    logger.warning(
        "recover_session: restored %s from .bak (live=%d → bak=%d messages). "
        "See #1558 for the data-loss class this guards against.",
        session_path.name, status["live_messages"], status["bak_messages"],
    )
    return {**status, "restored": True}


def _state_db_has_session(session_id: str, state_db_path: Path | None) -> bool:
    """Return whether state.db still knows this session.

    The check is deliberately fail-open: recovery must not be prevented by a
    locked, absent, or older-schema state DB. When a DB is readable and has no
    row, treat the orphan backup as a tombstoned/deleted session and skip it.
    """
    if state_db_path is None or not state_db_path.exists():
        return True
    try:
        with sqlite3.connect(f"file:{state_db_path}?mode=ro", uri=True) as conn:
            cur = conn.execute(
                "select 1 from sqlite_master where type='table' and name='sessions'"
            )
            if cur.fetchone() is None:
                return True
            cur = conn.execute("select 1 from sessions where id = ? limit 1", (session_id,))
            return cur.fetchone() is not None
    except Exception as exc:
        logger.debug("state_db session tombstone check failed for %s: %s", session_id, exc)
        return True


def _orphaned_backup_live_paths(
    session_dir: Path,
    state_db_path: Path | None = None,
) -> list[Path]:
    """Return live ``<sid>.json`` paths whose ``<sid>.json.bak`` exists.

    ``Path.glob('*.json')`` does not see orphan backups because their suffix is
    ``.bak``. Existing startup recovery only handled shrunken live files; this
    helper covers the crash shape where the live sidecar is gone but the rescue
    copy remains.
    """
    paths: list[Path] = []
    for bak_path in sorted(session_dir.glob('*.json.bak')):
        live_path = bak_path.with_suffix('')
        if live_path.name.startswith('_') or live_path.exists():
            continue
        if _msg_count(bak_path) < 0:
            continue
        session_id = live_path.stem
        # A WebUI session the user deleted must not be resurrected from its
        # surviving .bak on the next boot (#5498). Use the DURABLE tombstone
        # only — not the _index.json heuristic — so a genuine crash that loses
        # the sidecar while its index entry survives is still restored (the
        # crash-recovery case this helper exists for).
        if _durable_tombstone_marks_deleted_webui_session(session_dir, session_id):
            logger.info(
                "recover_all_sessions_on_startup: skipped orphan backup %s; "
                "session is tombstoned as a deleted WebUI session",
                bak_path.name,
            )
            continue
        if not _state_db_has_session(session_id, state_db_path):
            logger.info(
                "recover_all_sessions_on_startup: skipped orphan backup %s; "
                "state.db has no live session row",
                bak_path.name,
            )
            continue
        paths.append(live_path)
    return paths
