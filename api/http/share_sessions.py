"""Resolve public-share snapshots without crossing session ownership seams.

External transcripts may live in state.db while share metadata must be stored
in a WebUI sidecar.  This module resolves those two roles together and applies
request-profile visibility before a public snapshot is built.
"""

from __future__ import annotations

import copy

from api.agent_ops import is_cli_session_row
from api.http.session_visibility import _session_visible_to_active_profile
from api.sessions import (
    foreign_session_access,
    is_messaging_session_record,
    requires_external_metadata_lookup,
    session_detail_projection,
)
from api.sessions.repository import get_full_session
from api.sessions.store import Session, get_session, get_cli_session_messages, title_from
from api.workspace import get_last_workspace

def _share_snapshot_messages_for_session(session, *, cli_meta: dict | None = None) -> list:
    """Return the visible transcript that a public share should snapshot.

    External sessions (Telegram/Discord/Slack/CLI/etc.) may have no WebUI sidecar
    or may persist only local metadata in the sidecar while the transcript lives
    in state.db. Public sharing should snapshot the same visible conversation the
    session page renders, not the bare local sidecar payload.
    """
    sid = str(getattr(session, "session_id", "") or "").strip()
    current_messages = list(getattr(session, "messages", None) or [])
    if not sid:
        return current_messages
    profile = getattr(session, "profile", None)
    is_messaging = (
        is_messaging_session_record(session)
        or is_messaging_session_record(cli_meta)
    )
    if is_messaging or not current_messages:
        cli_messages = get_cli_session_messages(sid, profile=profile)
        if cli_messages:
            if is_messaging:
                return session_detail_projection.merge_session_messages(
                    session,
                    cli_messages,
                )
            return list(cli_messages)
    return current_messages


def _build_share_metadata_sidecar(
    sid: str,
    snapshot_session,
    *,
    cli_meta: dict | None = None,
):
    """Create a minimal WebUI sidecar for share metadata on external sessions."""
    cli_meta = dict(cli_meta or {})
    workspace = (
        cli_meta.get("workspace")
        or cli_meta.get("cwd")
        or getattr(snapshot_session, "workspace", None)
    )
    if not workspace:
        workspace = get_last_workspace()
    session = Session(
        session_id=sid,
        title=(
            cli_meta.get("title")
            or getattr(snapshot_session, "title", None)
            or title_from(getattr(snapshot_session, "messages", None) or [], "CLI Session")
        ),
        workspace=workspace,
        messages=[],
        model=cli_meta.get("model") or getattr(snapshot_session, "model", None) or "unknown",
        model_provider=(
            cli_meta.get("model_provider")
            or getattr(snapshot_session, "model_provider", None)
        ),
        created_at=cli_meta.get("created_at") or getattr(snapshot_session, "created_at", None),
        updated_at=cli_meta.get("updated_at") or getattr(snapshot_session, "updated_at", None),
        profile=cli_meta.get("profile") or getattr(snapshot_session, "profile", None),
    )
    session.is_cli_session = bool(
        getattr(snapshot_session, "is_cli_session", False)
        or is_cli_session_row(cli_meta)
    )
    session.source_tag = cli_meta.get("source_tag") or getattr(snapshot_session, "source_tag", None)
    session.raw_source = (
        cli_meta.get("raw_source")
        or getattr(snapshot_session, "raw_source", None)
        or session.source_tag
    )
    session.session_source = (
        cli_meta.get("session_source")
        or getattr(snapshot_session, "session_source", None)
    )
    session.source_label = (
        cli_meta.get("source_label")
        or getattr(snapshot_session, "source_label", None)
    )
    session.read_only = bool(
        cli_meta.get("read_only") or getattr(snapshot_session, "read_only", False)
    )
    for attr in (
        "user_id",
        "chat_id",
        "chat_type",
        "thread_id",
        "session_key",
        "platform",
        "origin_chat_id",
        "origin_user_id",
        "parent_session_id",
    ):
        value = cli_meta.get(attr)
        if value is None:
            value = getattr(snapshot_session, attr, None)
        if value is not None:
            setattr(session, attr, value)
    return session


def _resolve_share_session_pair(sid: str, handler):
    """Resolve a shareable session plus the sidecar that stores share metadata.

    Returns ``(snapshot_session, stored_session_or_none, cli_meta)``. The
    snapshot session always carries the transcript that should become the public
    share payload. ``stored_session`` is the WebUI-owned sidecar to mutate for
    share_token/share_created_at persistence; it may be absent for pure external
    sessions that have not yet created local metadata.
    """
    try:
        stored_session = get_session(sid)
        cli_meta = (
            foreign_session_access.metadata(sid)
            if requires_external_metadata_lookup(stored_session)
            else {}
        )
        effective_profile = (
            (cli_meta or {}).get("profile")
            or getattr(stored_session, "profile", None)
            or None
        )
        if not _session_visible_to_active_profile(effective_profile, handler):
            raise KeyError(sid)
        stored_session = get_full_session(sid, session=stored_session)
        snapshot_session = copy.copy(stored_session)
        snapshot_session.messages = _share_snapshot_messages_for_session(
            stored_session,
            cli_meta=cli_meta,
        )
        return snapshot_session, stored_session, cli_meta or {}
    except KeyError:
        cli_meta = foreign_session_access.metadata(sid) or {}
        effective_profile = cli_meta.get("profile") or None
        if not _session_visible_to_active_profile(effective_profile, handler):
            raise KeyError(sid) from None
        synth, reason = foreign_session_access.claim(sid, cli_meta)
        if reason == "was_webui" or synth is None:
            raise KeyError(sid) from None
        return synth, None, cli_meta


__routes_exports__ = (
    "_share_snapshot_messages_for_session",
    "_build_share_metadata_sidecar",
    "_resolve_share_session_pair",
)
