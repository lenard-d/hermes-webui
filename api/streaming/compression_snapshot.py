"""Durable preservation of pre-compression session snapshots."""

from __future__ import annotations

import json
import logging

from api.sessions import Session


logger = logging.getLogger(__name__)


def _preserve_pre_compression_snapshot(s, old_sid: str) -> None:
    """Persist old_sid as a read-only pre-compression snapshot.

    Context compression rotates the active WebUI session id from old_sid to the
    agent's new continuation id. The old JSON must remain on disk for lineage
    traversal, but it should not continue to appear as an active sidebar row.
    """
    # Resolve through the live Session path owner so isolated state roots and
    # tests do not observe a stale package-level directory alias.
    old_path = s.path.parent / f'{old_sid}.json'
    if not old_path.exists():
        return
    try:
        existing_text = old_path.read_text(encoding='utf-8')
        try:
            existing = json.loads(existing_text)
            existing_msgs = len(existing.get('messages') or [])
        except (json.JSONDecodeError, ValueError):
            # Treat corrupt/malformed old JSON as missing history and rewrite it
            # from the in-memory pre-compression messages below. That is safer
            # than leaving an unreadable recovery snapshot behind.
            existing_msgs = -1
        if len(s.messages) > existing_msgs:
            # In-memory messages are newer than the file; save the full old
            # snapshot from the current session object while preserving its
            # pre-existing parent_session_id lineage.
            saved_sid = s.session_id
            saved_snapshot = bool(getattr(s, 'pre_compression_snapshot', False))
            saved_pinned = bool(getattr(s, 'pinned', False))
            s.session_id = old_sid
            s.pre_compression_snapshot = True
            s.pinned = False
            # Stage-359 / PR #2295: clear runtime stream-state fields on the
            # archived snapshot so the sidebar does not reopen the parent as
            # a permanently-running session while the child already holds the
            # completed answer. The continuation session's live state is
            # restored from saved_* locals in the finally block.
            saved_active_stream_id = getattr(s, 'active_stream_id', None)
            saved_pending_user_message = getattr(s, 'pending_user_message', None)
            saved_pending_attachments = list(getattr(s, 'pending_attachments', []) or [])
            saved_pending_started_at = getattr(s, 'pending_started_at', None)
            saved_pending_user_source = getattr(s, 'pending_user_source', None)
            s.active_stream_id = None
            s.pending_user_message = None
            s.pending_attachments = []
            s.pending_started_at = None
            s.pending_user_source = None
            try:
                # skip_index=False so the snapshot appears in _index.json with
                # the pre_compression_snapshot marker. The sidebar projection
                # (#2285) reads that marker to hide the snapshot from active
                # rows while keeping the JSON discoverable for lineage traversal.
                s.save(touch_updated_at=False, skip_index=False)
                logger.info(
                    "Preserved pre-compression session %s (%d messages) to disk",
                    old_sid, len(s.messages),
                )
            finally:
                s.session_id = saved_sid
                s.pre_compression_snapshot = saved_snapshot
                s.pinned = saved_pinned
                s.active_stream_id = saved_active_stream_id
                s.pending_user_message = saved_pending_user_message
                s.pending_attachments = saved_pending_attachments
                s.pending_started_at = saved_pending_started_at
                s.pending_user_source = saved_pending_user_source
            return
        # Existing file is already at least as complete as memory; stamp only
        # the snapshot marker so index/sidebar projection can hide it without
        # rewriting a shorter messages array over a fuller transcript.
        snapshot = Session.load(old_sid)
        if snapshot:
            snapshot.pre_compression_snapshot = True
            snapshot.pinned = False
            # Stage-359 Opus SHOULD-FIX: clear runtime fields on the loaded
            # snapshot too. If the disk snapshot was last persisted while the
            # parent was live, it could carry a stale active_stream_id /
            # pending_* over to disk. The sidebar projection filters snapshot
            # rows so this is latent today, but the contract should match the
            # primary branch above so future readers can trust snapshot files
            # to never contain live runtime state.
            snapshot.active_stream_id = None
            snapshot.pending_user_message = None
            snapshot.pending_attachments = []
            snapshot.pending_started_at = None
            snapshot.pending_user_source = None
            snapshot.save(touch_updated_at=False, skip_index=False)
            logger.info(
                "Marked pre-compression session %s as sidebar-hidden snapshot",
                old_sid,
            )
    except OSError:
        logger.debug("Could not read old session file before preservation")
    except Exception:
        logger.debug("Failed to preserve pre-compression session file", exc_info=True)
