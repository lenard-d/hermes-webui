"""HTTP adapter for persisting assistant Anchor scenes."""

from __future__ import annotations

from api.sessions import (
    AnchorSceneMessageNotFound,
    persist_anchor_activity_scene,
)


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _message_index_from_request(body):
    """Resolve the full-transcript index from a possibly windowed request."""
    if not isinstance(body, dict):
        return None
    message_index = _int_or_none(body.get("message_index"))
    message_offset = _int_or_none(body.get("message_offset"))
    window_index = _int_or_none(body.get("message_window_index"))
    if (
        window_index is not None
        and message_offset is not None
        and message_offset > 0
        and (message_index is None or message_index == window_index)
    ):
        return window_index + message_offset
    return message_index


def _handle_session_anchor_scene(
    handler,
    body,
    *,
    get_or_materialize_session,
    session_visible_to_active_profile,
    require_fields,
    bad_response,
    json_response,
):
    """Translate one anchor-scene request into the session-domain operation."""
    try:
        require_fields(body, "session_id", "scene")
    except ValueError as exc:
        return bad_response(handler, str(exc))
    sid = str(body.get("session_id") or "").strip()
    if not sid:
        return bad_response(handler, "session_id is required", 400)
    try:
        session = get_or_materialize_session(sid)
    except KeyError:
        return bad_response(handler, "Session not found", 404)
    except PermissionError:
        return bad_response(
            handler,
            "Read-only imported sessions cannot persist anchor scenes",
            403,
        )
    if not session_visible_to_active_profile(
        getattr(session, "profile", None) or None,
        handler,
    ):
        return bad_response(handler, "Session not found", 404)
    try:
        result = persist_anchor_activity_scene(
            session,
            scene=body.get("scene"),
            message_index=_message_index_from_request(body),
            message_ref=str(body.get("message_ref") or ""),
            stream_id=str(body.get("stream_id") or ""),
        )
    except ValueError as exc:
        return bad_response(handler, str(exc), 400)
    except AnchorSceneMessageNotFound as exc:
        return bad_response(handler, str(exc), 404)
    return json_response(handler, {"ok": True, **result})
