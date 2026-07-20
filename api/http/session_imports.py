"""HTTP handlers for importing foreign and JSON-exported sessions.

The module owns profile authorization, append-only refresh semantics, read-only
subagent handling, and final WebUI materialization as one import lifecycle.
"""

from __future__ import annotations

from api.config import DEFAULT_MODEL, DEFAULT_WORKSPACE
from api.helpers import bad, j, require
from api.http.session_visibility import _session_visible_to_active_profile
from api.profiles import (
    _is_isolated_profile_mode,
    _profiles_match,
    get_active_profile_name,
    is_valid_profile_id,
)
from api.sessions import foreign_session_access
from api.sessions.events import publish_session_list_changed
from api.sessions.repository import edit_session
from api.sessions.store import (
    Session,
    _profile_has_user_projects,
    ensure_cron_project,
    get_cli_session_messages,
    import_cli_session,
    is_cron_session,
    title_from,
)
from api.sessions.title_publication import _queue_generated_title_for_imported_session
from api.workspace import get_last_workspace, resolve_trusted_workspace


def _normalize_message_for_import_refresh(message: object) -> object:
    """Normalize message payloads for import refresh prefix checks.

    The strict dict comparison previously failed when existing messages held
    integer timestamps while refreshed messages held floating-point timestamps.
    Strip timing keys before comparison so we can safely treat semantic
    prefixes as equivalent.
    """
    if not isinstance(message, dict):
        return message
    normalized = dict(message)
    normalized.pop("timestamp", None)
    normalized.pop("_ts", None)
    return normalized


def _message_has_cli_tool_metadata(message: object) -> bool:
    if not isinstance(message, dict):
        return False
    if message.get("role") == "assistant" and message.get("tool_calls"):
        return True
    if message.get("role") == "tool" and (message.get("tool_call_id") or message.get("tool_name") or message.get("name")):
        return True
    return False


def _strip_cli_tool_metadata_for_refresh(message: object) -> object:
    if not isinstance(message, dict):
        return _normalize_message_for_import_refresh(message)
    normalized = _normalize_message_for_import_refresh(message)
    if not isinstance(normalized, dict):
        return normalized
    for key in ("tool_calls", "tool_call_id", "tool_name", "name"):
        normalized.pop(key, None)
    return normalized


def _is_cli_tool_metadata_enrichment(existing_messages: list, fresh_messages: list) -> bool:
    """Return True when fresh messages only add CLI tool metadata.

    Older imports from get_cli_session_messages() persisted assistant/tool rows
    without tool_calls, tool_call_id, or tool_name. After #1772 the refreshed
    transcript can have the same length but richer metadata, so re-imports must
    rebuild the stored sidecar even without a new row.
    """
    if not isinstance(existing_messages, list) or not isinstance(fresh_messages, list):
        return False
    if len(existing_messages) != len(fresh_messages):
        return False
    if any(_message_has_cli_tool_metadata(m) for m in existing_messages):
        return False
    if not any(_message_has_cli_tool_metadata(m) for m in fresh_messages):
        return False
    for idx, existing_message in enumerate(existing_messages):
        if _strip_cli_tool_metadata_for_refresh(existing_message) != _strip_cli_tool_metadata_for_refresh(fresh_messages[idx]):
            return False
    return True


def _is_messages_refresh_prefix_match(existing_messages: list, fresh_messages: list) -> bool:
    """Return True when existing_messages is a prefix of fresh_messages by value.

    This is a semantic comparison intended for import refresh, not deep
    structural equality. It intentionally ignores timing fields that may differ
    in type/precision between storage layers.
    """
    if not isinstance(existing_messages, list) or not isinstance(fresh_messages, list):
        return False
    if len(existing_messages) > len(fresh_messages):
        return False
    for idx, existing_message in enumerate(existing_messages):
        fresh_message = fresh_messages[idx]
        if _normalize_message_for_import_refresh(existing_message) != _normalize_message_for_import_refresh(fresh_message):
            return False
    return True


def _request_wants_all_profiles_import(body) -> bool:
    """Return whether an import request explicitly allows cross-profile lookup."""
    if not isinstance(body, dict):
        return False
    if body.get("all_profiles") is True:
        return True
    scope = str(body.get("profile_scope") or "").strip().lower()
    return scope in {"all", "all_profiles"}


def _normalize_import_profile_value(value):
    """Return a validated profile id or None for an omitted profile."""
    if value is None:
        return None
    profile = str(value).strip()
    if not profile:
        return None
    if not is_valid_profile_id(profile):
        raise ValueError("Invalid profile")
    return profile


def _handle_session_import_cli(handler, body):
    """Import a single CLI session into the WebUI store."""
    try:
        require(body, "session_id")
    except ValueError as e:
        return bad(handler, str(e))

    sid = str(body["session_id"])
    requested_profile = _normalize_import_profile_value((body or {}).get("profile"))
    if requested_profile == "":
        return bad(handler, "invalid profile", 400)
    allow_all_profiles = _request_wants_all_profiles_import(body)
    if allow_all_profiles and _is_isolated_profile_mode():
        return bad(handler, "all_profiles import is not allowed in isolated profile mode", 403)
    if allow_all_profiles and not requested_profile:
        return bad(handler, "profile is required for all_profiles import", 400)

    # Check if already imported — refresh messages from CLI store if new ones arrived
    existing = Session.load(sid)
    if existing:
        # Cross-profile boundary: an unqualified (non-all-profiles) request must not
        # read or refresh a session that belongs to another profile, even though the
        # WebUI session store (SESSION_DIR) is a single global directory. This mirrors
        # the /api/session detail and /api/session/export profile-scoping gates.
        # An explicit all_profiles import is still allowed, but only when the request's
        # profile matches the stored session's profile.
        existing_profile = getattr(existing, "profile", None)
        if allow_all_profiles:
            if requested_profile and not _profiles_match(existing_profile, requested_profile):
                return bad(handler, "Session not found in CLI store", 404)
        elif not _session_visible_to_active_profile(existing_profile, handler):
            return bad(handler, "Session not found in CLI store", 404)
        refresh_profile = requested_profile or existing_profile
        cli_meta = foreign_session_access.resolve_import_metadata(
            sid,
            requested_profile=refresh_profile,
            allow_all_profiles=allow_all_profiles,
        )
        fresh_msgs = get_cli_session_messages(
            sid,
            profile=(cli_meta or {}).get("profile") or refresh_profile,
        )
        changed = False
        try:
            with edit_session(
                sid,
                session=existing,
                touch_updated_at=False,
                save_when=lambda _session: changed,
            ) as current:
                # Authorization was checked before reading the foreign store so an
                # unauthorized request cannot use refresh as a metadata oracle. Check
                # the repository-current record again before applying that data.
                current_profile = getattr(current, "profile", None)
                if allow_all_profiles:
                    if requested_profile and not _profiles_match(
                        current_profile, requested_profile
                    ):
                        return bad(handler, "Session not found in CLI store", 404)
                elif not _session_visible_to_active_profile(current_profile, handler):
                    return bad(handler, "Session not found in CLI store", 404)

                existing = current
                if fresh_msgs and len(fresh_msgs) > len(existing.messages):
                    # Prefix-equality guard: only extend if existing messages are a prefix of
                    # the fresh CLI messages. Prevents silently dropping WebUI-added messages
                    # on hybrid sessions (user sent messages via WebUI while CLI continued).
                    if _is_messages_refresh_prefix_match(existing.messages, fresh_msgs):
                        existing.messages = fresh_msgs
                        changed = True
                elif fresh_msgs and _is_cli_tool_metadata_enrichment(existing.messages, fresh_msgs):
                    # Same row count, richer payload: rebuild sidecars imported before
                    # CLI tool metadata was preserved (#1772).
                    existing.messages = fresh_msgs
                    changed = True
                if cli_meta:
                    # A subagent child must never be flipped to CLI-classified /
                    # writable on an existing-session refresh either (#5307).
                    _existing_is_sa = (
                        (existing.source_tag or existing.raw_source or "").strip().lower() == "subagent"
                        or (cli_meta.get("source_tag") or cli_meta.get("raw_source") or "").strip().lower() == "subagent"
                        or foreign_session_access.is_subagent_child(sid)
                    )
                    updates = {
                        "is_cli_session": (False if _existing_is_sa else True),
                        "source_tag": existing.source_tag or cli_meta.get("source_tag"),
                        "raw_source": existing.raw_source or cli_meta.get("raw_source") or cli_meta.get("source_tag"),
                        "session_source": existing.session_source or cli_meta.get("session_source"),
                        "source_label": existing.source_label or cli_meta.get("source_label"),
                        "parent_session_id": existing.parent_session_id or cli_meta.get("parent_session_id"),
                    }
                    # A subagent child is view-only: also coerce read_only=True on the
                    # persisted sidecar so a stale writable (pre-fix) sidecar can't be
                    # used to start a WebUI turn (#5307).
                    if _existing_is_sa:
                        updates["read_only"] = True
                    for attr, value in updates.items():
                        if getattr(existing, attr, None) != value:
                            setattr(existing, attr, value)
                            changed = True
                else:
                    _existing_is_sa = (
                        (existing.source_tag or existing.raw_source or "").strip().lower() == "subagent"
                        or foreign_session_access.is_subagent_child(sid)
                    )
        except KeyError:
            return bad(handler, "Session not found in CLI store", 404)
        if changed:
            publish_session_list_changed(
                "session_import_cli",
                profile=getattr(existing, "profile", None),
            )
        return j(
            handler,
            {
                "session": existing.compact()
                | {
                    "messages": existing.messages,
                    "is_cli_session": (False if _existing_is_sa else True),
                    # Greptile #4911 follow-up: read read_only from
                    # the persisted Session, NOT from cli_meta.  This
                    # refresh path is for an already-WebUI-owned
                    # session; the WebUI's persisted view is the
                    # source of truth for the response, not the
                    # foreign store's current value.  (Mirrors the
                    # GET /api/session fix.)
                    "read_only": bool(getattr(existing, "read_only", False)),
                },
                "imported": False,
            },
        )

    # Fetch messages from CLI store
    cli_meta = foreign_session_access.resolve_import_metadata(
        sid,
        requested_profile=requested_profile,
        allow_all_profiles=allow_all_profiles,
    )
    profile = cli_meta.get("profile") if cli_meta else (requested_profile if allow_all_profiles else None)
    msgs = get_cli_session_messages(sid, profile=profile)
    if not msgs:
        return bad(handler, "Session not found in CLI store", 404)

    # Get profile, model, timestamps, and title from CLI session metadata
    created_at = cli_meta.get("created_at") if cli_meta else None
    updated_at = cli_meta.get("updated_at") if cli_meta else None
    cli_title = cli_meta.get("title") if cli_meta else None
    cli_source_tag = cli_meta.get("source_tag") if cli_meta else None
    model = cli_meta.get("model", "unknown") if cli_meta else "unknown"
    cli_raw_source = cli_meta.get("raw_source") if cli_meta else None
    cli_session_source = cli_meta.get("session_source") if cli_meta else None
    cli_source_label = cli_meta.get("source_label") if cli_meta else None
    cli_user_id = cli_meta.get("user_id") if cli_meta else None
    cli_chat_id = cli_meta.get("chat_id") if cli_meta else None
    cli_chat_type = cli_meta.get("chat_type") if cli_meta else None
    cli_thread_id = cli_meta.get("thread_id") if cli_meta else None
    cli_session_key = cli_meta.get("session_key") if cli_meta else None
    cli_platform = cli_meta.get("platform") if cli_meta else None
    cli_parent_session_id = cli_meta.get("parent_session_id") if cli_meta else None
    cli_read_only = bool((cli_meta or {}).get("read_only"))
    # Delegated subagent children (#5307) are recovered VIEW-ONLY: they must
    # never be materialized as a writable WebUI sidecar via this endpoint, or a
    # subsequent chat-start/composer write would take ownership of a session
    # that belongs to the delegate runner. Treat them like an explicitly
    # read-only source (return the read-only stub payload, do not import), and
    # keep them out of the _isExternalSession frontend gates (is_cli_session=False).
    _sa_child = foreign_session_access.is_subagent_child(sid)
    # Also treat a resolved-metadata subagent source as view-only: with
    # all_profiles=true, cli_meta is resolved from the requested (possibly
    # non-active) profile, so the active-profile state.db check (_sa_child)
    # can miss it (#5307 cross-profile edge).
    _cli_sa = (cli_source_tag or cli_raw_source or "").strip().lower() == "subagent"
    _sa_child = _sa_child or _cli_sa
    _read_only_view = cli_read_only or _sa_child

    # Use the CLI session title if available (e.g., cron job name), otherwise derive from messages
    title = cli_title or title_from(msgs, "CLI Session")

    # Auto-assign cron sessions to the dedicated "Cron Jobs" project (#1079),
    # gated on whether this profile has opted into project organization (#5379)
    cron_project_id = None
    if is_cron_session(sid, cli_source_tag):
        cron_project_id = ensure_cron_project(create=_profile_has_user_projects())

    if _read_only_view:
        session_payload = {
            "session_id": sid,
            "title": title,
            "workspace": str(get_last_workspace()),
            "model": model,
            "message_count": len(msgs),
            "created_at": created_at,
            "updated_at": updated_at,
            "last_message_at": updated_at or created_at,
            "pinned": False,
            "archived": False,
            "project_id": None,
            "profile": profile,
            # Subagent children (#5307) are recovered view-only and must NOT be
            # CLI-classified (keeps them out of the frontend _isExternalSession
            # gates); other explicitly-read-only sources keep is_cli_session=True.
            "is_cli_session": (False if _sa_child else True),
            "source_tag": cli_source_tag,
            "raw_source": cli_raw_source or cli_source_tag,
            "session_source": cli_session_source,
            "source_label": cli_source_label,
            "parent_session_id": cli_parent_session_id,
            "read_only": True,
            "messages": msgs,
            "tool_calls": [],
        }
        return j(handler, {"session": session_payload, "imported": False})

    s = import_cli_session(
        sid,
        title,
        msgs,
        model,
        profile=profile,
        created_at=created_at,
        updated_at=updated_at,
        parent_session_id=cli_parent_session_id,
        source_metadata={
            "project_id": cron_project_id,
            "is_cli_session": True,
            "source_tag": cli_source_tag,
            "raw_source": cli_raw_source or cli_source_tag,
            "session_source": cli_session_source,
            "source_label": cli_source_label,
            "user_id": cli_user_id,
            "chat_id": cli_chat_id,
            "chat_type": cli_chat_type,
            "thread_id": cli_thread_id,
            "session_key": cli_session_key,
            "platform": cli_platform,
            "model_provider": (cli_meta or {}).get("model_provider"),
        },
    )
    publish_session_list_changed(
        "session_import_cli",
        profile=getattr(s, "profile", None),
    )
    _queue_generated_title_for_imported_session(
        s,
        {
            "title": cli_title,
            "source_tag": cli_source_tag,
            "raw_source": cli_raw_source,
            "session_source": cli_session_source,
            "source_label": cli_source_label,
            "read_only": cli_read_only,
        },
    )
    return j(
        handler,
        {
            "session": s.compact()
            | {
                "messages": msgs,
                "is_cli_session": True,
            },
            "imported": True,
        },
    )


def _handle_session_import(handler, body):
    """Import a session from a JSON export. Creates a new session with a new ID."""
    if not body or not isinstance(body, dict):
        return bad(handler, "Request body must be a JSON object")
    messages = body.get("messages")
    if not isinstance(messages, list):
        return bad(handler, 'JSON must contain a "messages" array')
    title = body.get("title", "Imported session")
    try:
        workspace = str(resolve_trusted_workspace(body.get("workspace", str(DEFAULT_WORKSPACE))))
    except (TypeError, ValueError) as e:
        return bad(handler, str(e))
    model = body.get("model", DEFAULT_MODEL)
    s = Session(
        title=title,
        workspace=workspace,
        model=model,
        messages=messages,
        tool_calls=body.get("tool_calls", []),
        profile=get_active_profile_name(),
    )
    s.pinned = body.get("pinned", False)
    foreign_session_access.publish(s, persist=True)
    publish_session_list_changed("session_import")
    return j(handler, {"ok": True, "session": s.compact() | {"messages": s.messages}})

__routes_exports__ = (
    "_normalize_message_for_import_refresh",
    "_message_has_cli_tool_metadata",
    "_strip_cli_tool_metadata_for_refresh",
    "_is_cli_tool_metadata_enrichment",
    "_is_messages_refresh_prefix_match",
    "_request_wants_all_profiles_import",
    "_normalize_import_profile_value",
    "_handle_session_import_cli",
    "_handle_session_import",
)
