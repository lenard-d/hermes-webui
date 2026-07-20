"""HTTP adapter and compatibility exports for assistant anchor scenes."""

from __future__ import annotations

# Explicit imports are the static compatibility surface consumed by
# ``api.routes``; most are intentionally re-exported without local use.
# ruff: noqa: F401

from api.sessions.anchor_scene import (
    AnchorSceneMessageNotFound,
    _ANCHOR_ACTIVITY_SCENE_MAX_BYTES,
    _ANCHOR_ACTIVITY_SCENE_MAX_ROWS,
    _ANCHOR_SCENE_SETTLED_SNIPPET_CAP,
    _RUN_JOURNAL_TOOL_ID_KEYS,
    _anchor_scene_candidate_matches_scene,
    _anchor_scene_clean_text,
    _anchor_scene_content_rows,
    _anchor_scene_content_text,
    _anchor_scene_content_tool,
    _anchor_scene_content_visible_text,
    _anchor_scene_final_answer_text,
    _anchor_scene_int_or_none,
    _anchor_scene_is_bounded_tool_body_preview,
    _anchor_scene_matching_content_tool_row_index,
    _anchor_scene_message_has_content_tool_use,
    _anchor_scene_message_index_from_request,
    _anchor_scene_message_reasoning_text,
    _anchor_scene_message_ref_digest,
    _anchor_scene_message_text,
    _anchor_scene_message_turn_duration,
    _anchor_scene_object_contains_subset,
    _anchor_scene_prose_row,
    _anchor_scene_records,
    _anchor_scene_row_base,
    _anchor_scene_row_has_live_identity,
    _anchor_scene_row_is_stale_token_answer,
    _anchor_scene_row_key,
    _anchor_scene_row_looks_like_final_answer,
    _anchor_scene_settle_live_running_row,
    _anchor_scene_string_payload,
    _anchor_scene_text_has_long_overlap,
    _anchor_scene_text_key,
    _anchor_scene_thinking_row,
    _anchor_scene_tool_args,
    _anchor_scene_tool_id,
    _anchor_scene_tool_name,
    _anchor_scene_tool_row,
    _anchor_scene_tool_row_args,
    _anchor_scene_tool_row_body_text,
    _anchor_scene_tool_row_has_invocation_evidence,
    _anchor_scene_tool_row_id,
    _anchor_scene_tool_row_name,
    _anchor_scene_tool_row_started_at,
    _anchor_scene_tool_rows_can_name_match,
    _anchor_scene_tool_rows_have_compatible_body,
    _anchor_scene_tool_rows_have_compatible_invocation,
    _anchor_scene_tool_rows_have_compatible_names,
    _anchor_scene_tool_rows_have_different_explicit_ids,
    _anchor_scene_tool_rows_have_same_started_at,
    _assistant_anchor_scene_message_ref,
    _assistant_anchor_scene_message_ref_payload,
    _complete_hydrated_anchor_scene,
    _find_anchor_scene_message,
    _hydrate_anchor_activity_scenes,
    _normalize_anchor_scene_message_ref,
    _run_journal_live_snapshot,
    _run_journal_snapshot_arg_detail_score,
    _run_journal_snapshot_merge_args,
    _run_journal_snapshot_recovery_args,
    _run_journal_snapshot_tool_id,
    _run_journal_status_payload,
    _sanitize_anchor_activity_scene,
    _truncate_journal_snapshot_value,
    persist_anchor_activity_scene,
)


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
            message_index=_anchor_scene_message_index_from_request(body),
            message_ref=str(body.get("message_ref") or ""),
            stream_id=str(body.get("stream_id") or ""),
        )
    except ValueError as exc:
        return bad_response(handler, str(exc), 400)
    except AnchorSceneMessageNotFound as exc:
        return bad_response(handler, str(exc), 404)
    return json_response(handler, {"ok": True, **result})


# ``api.routes`` imports these names explicitly. No compatibility rebinding is
# needed: domain functions retain the globals of their real session owner.
__routes_exports__ = ()
