"""Compression rotation and continuation projection for a local run."""

from __future__ import annotations

import logging

from api.agent_cache import locked_agent_cache
from api.compression_anchor import visible_messages_for_anchor
from api.session_state import LOCK, SESSIONS, alias_session_agent_lock
from api.sessions.cache import _evict_sessions_over_cap

from .agent_cache import (
    _cached_agent_matches_session,
    _cached_agent_session_identity,
    _close_cached_agent_entry_at_session_boundary,
)
from .compression_anchors import (
    _compact_summary_text,
    _compression_anchor_message_key,
    _compression_summary_from_messages,
    _is_context_compression_marker,
)
from .compression_snapshot import _preserve_pre_compression_snapshot
from .post_compression_context import (
    _estimate_post_compression_context_tokens,
    _prune_context_tool_results_after_compression,
)


class LocalCompressionOwner:
    """Own session-id rotation and the durable compression projection."""

    def __init__(
        self,
        *,
        original_session_id: str,
        profile_name: str | None,
        agent,
        session_lock,
        logger: logging.Logger,
    ) -> None:
        self.original_session_id = original_session_id
        self.continuation_session_id: str | None = None
        self.profile_name = profile_name
        self.agent = agent
        self.session_lock = session_lock
        self.logger = logger
        self.compressed = False

    def rotate_if_needed(self, session) -> None:
        """Publish a provider-rotated continuation under one lock generation."""

        agent_session_id = getattr(self.agent, "session_id", None)
        if not agent_session_id or agent_session_id == self.original_session_id:
            return

        old_id = self.original_session_id
        new_id = agent_session_id
        self.continuation_session_id = new_id
        session.session_id = new_id
        if not getattr(session, "profile", None) and self.profile_name:
            session.profile = self.profile_name
            self.logger.info(
                "Stamped profile=%r on continuation session %s after compression",
                self.profile_name,
                new_id,
            )
        _preserve_pre_compression_snapshot(session, old_id)
        session.pre_compression_snapshot = False
        session.parent_session_id = old_id
        alias_session_agent_lock(old_id, new_id, self.session_lock)
        with LOCK:
            cached_old = SESSIONS.pop(old_id, None)
            if cached_old is not None and cached_old is not session:
                cached_id = str(getattr(cached_old, "session_id", "") or "")
                if cached_id == str(old_id):
                    SESSIONS[old_id] = cached_old
                else:
                    self.logger.warning(
                        "compression cache migration skipped stale object: "
                        "old_sid=%s new_sid=%s cached_session_id=%s",
                        old_id,
                        new_id,
                        cached_id or None,
                    )
            SESSIONS[new_id] = session
            SESSIONS.move_to_end(new_id)
            _evict_sessions_over_cap()
        self._migrate_agent_cache(old_id, new_id)
        self.compressed = True

    def detect_compressor_change(self, *, previous_count: int) -> None:
        if self.compressed:
            return
        compressor = getattr(self.agent, "context_compressor", None)
        current = int(getattr(compressor, "compression_count", 0) or 0)
        self.compressed = current > previous_count

    def project(
        self,
        session,
        *,
        previous_messages: list,
        system_message: str,
        publish,
        usage_snapshot,
    ) -> None:
        """Persist anchors/context estimates and emit one compression event."""

        if not self.compressed:
            return
        session.context_messages = _prune_context_tool_results_after_compression(
            self.agent,
            session.context_messages,
        )
        session.post_compression_context_tokens_estimate = (
            _estimate_post_compression_context_tokens(
                self.agent,
                session.context_messages,
                system_message,
            )
        )
        visible_after = visible_messages_for_anchor(
            session.messages,
            auto_compression=True,
        )
        marker_index = _last_compression_marker_index(session.messages)
        if marker_index is not None:
            before_marker = visible_messages_for_anchor(
                session.messages[:marker_index],
                auto_compression=True,
            )
            session.compression_anchor_visible_idx = max(0, len(before_marker) - 1)
            self.logger.info(
                "[ANCHOR-MARKER] session=%s marker_raw=%d vis_before=%d anchor=%d",
                getattr(session, "session_id", "?"),
                marker_index,
                len(before_marker),
                session.compression_anchor_visible_idx,
            )
        else:
            visible_before = visible_messages_for_anchor(
                previous_messages,
                auto_compression=True,
            )
            if visible_before:
                session.compression_anchor_visible_idx = len(visible_before) - 1
            elif visible_after:
                session.compression_anchor_visible_idx = 0
            else:
                session.compression_anchor_visible_idx = None

        anchor_index = session.compression_anchor_visible_idx
        anchor_message = (
            visible_after[anchor_index]
            if anchor_index is not None and anchor_index < len(visible_after)
            else (visible_after[-1] if visible_after else None)
        )
        session.compression_anchor_message_key = (
            _compression_anchor_message_key(anchor_message)
            if anchor_message
            else None
        )
        session.compression_anchor_summary = _compact_summary_text(
            _compression_summary_from_messages(session.messages)
            or _compression_summary_from_messages(session.context_messages)
        )
        continuation_id = self.continuation_session_id or session.session_id
        self.continuation_session_id = continuation_id
        publish(
            "compressed",
            {
                "session_id": self.original_session_id,
                "old_session_id": self.original_session_id,
                "new_session_id": continuation_id,
                "continuation_session_id": continuation_id,
                "message": "Compression finished",
                "usage": usage_snapshot(),
            },
        )

    def _migrate_agent_cache(self, old_id: str, new_id: str) -> None:
        skipped = None
        with locked_agent_cache() as session_agent_cache:
            entry = session_agent_cache.pop(old_id, None)
            if entry:
                cached_agent = entry[0]
                if _cached_agent_matches_session(cached_agent, new_id):
                    session_agent_cache[new_id] = entry
                else:
                    skipped = entry
                    self.logger.warning(
                        "Skipped cached agent migration with mismatched session "
                        "identity: old_sid=%s new_sid=%s agent_session_id=%s",
                        old_id,
                        new_id,
                        _cached_agent_session_identity(cached_agent),
                    )
        if skipped is not None:
            try:
                _close_cached_agent_entry_at_session_boundary(old_id, skipped)
            except Exception:
                self.logger.debug(
                    "Failed to close skipped compression-migration cached agent for %s",
                    old_id,
                    exc_info=True,
                )


def _last_compression_marker_index(messages: list) -> int | None:
    marker_index = None
    for index, message in enumerate(messages):
        if _is_context_compression_marker(message):
            marker_index = index
    return marker_index
