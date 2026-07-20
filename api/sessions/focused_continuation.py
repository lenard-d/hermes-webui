"""Atomic creation of focused continuations for exhausted sessions."""

from __future__ import annotations

import copy
import threading
import uuid

from .session_cache_eviction import _evict_sessions_over_cap
from .session_creation import find_compression_recovery_session
from .records import LOCK, SESSIONS, Session


_START_LOCK = threading.Lock()


def start_or_get_focused_continuation(
    source: Session,
    action: str,
    *,
    fallback_workspace,
) -> tuple[Session, bool]:
    """Atomically return the one durable focused continuation for ``source``."""
    source_id = str(getattr(source, "session_id", "") or "")
    source_profile = getattr(source, "profile", None)
    with _START_LOCK:
        continuation = find_compression_recovery_session(
            source_id,
            action,
            source_profile=source_profile,
        )
        if continuation is not None:
            return continuation, False

        title = str(getattr(source, "title", None) or "Untitled").strip() or "Untitled"
        if not title.endswith(" (focused continuation)"):
            title = f"{title} (focused continuation)"
        continuation = Session(
            session_id=uuid.uuid4().hex[:12],
            title=title,
            workspace=getattr(source, "workspace", fallback_workspace),
            model=getattr(source, "model", None),
            model_provider=getattr(source, "model_provider", None),
            messages=[],
            tool_calls=[],
            pinned=False,
            archived=False,
            project_id=getattr(source, "project_id", None),
            profile=source_profile,
            session_source="fork",
            personality=getattr(source, "personality", None),
            enabled_toolsets=copy.deepcopy(getattr(source, "enabled_toolsets", None)),
            context_length=getattr(source, "context_length", None),
            threshold_tokens=getattr(source, "threshold_tokens", None),
            gateway_routing=copy.deepcopy(getattr(source, "gateway_routing", None)),
            gateway_routing_history=copy.deepcopy(
                getattr(source, "gateway_routing_history", None) or []
            ),
            parent_session_id=source_id,
            worktree_path=getattr(source, "worktree_path", None),
            worktree_branch=getattr(source, "worktree_branch", None),
            worktree_repo_root=getattr(source, "worktree_repo_root", None),
            worktree_created_at=getattr(source, "worktree_created_at", None),
            compression_recovery_source_session_id=source_id,
            compression_recovery_action=action,
        )
        continuation.context_messages = []
        continuation.composer_draft = {"text": "", "files": []}
        continuation.save()
        with LOCK:
            SESSIONS[continuation.session_id] = continuation
            SESSIONS.move_to_end(continuation.session_id)
            _evict_sessions_over_cap()
        return continuation, True
