"""Goal, compression-recovery, and background-control HTTP owners."""

# Implementations are rebound to the canonical facade for compatibility.
# ruff: noqa: F821

from __future__ import annotations

from api.sessions import foreign_session_access, start_or_get_focused_continuation


def _handle_bg_task_complete_ack(handler, body):
    """Acknowledge a bg_task_complete SSE event (diagnostic only).

    Option Z PIVOT: the agent wakeup is now started SERVER-SIDE by the drain
    thread (``api/background_process._process_one`` → ``start_session_turn``)
    with NO browser round-trip — the closed-tab case works (parity with
    CLI/Telegram). The frontend no longer re-POSTs ``wakeup_prompt`` to
    /api/chat/start; the per-session SSE channel is demoted to pure live-view.

    This endpoint is therefore a pure no-op for state — it exists so an open
    tab can confirm receipt of the live-view event and so a future follow-up
    (analytics, telemetry) has a stable hook. ``PENDING_BG_TASK_COMPLETIONS``
    is consumed by ``_start_chat_stream_for_session`` when the server-side
    wakeup turn (or the next human turn / PR #2279 next-turn drain) runs.
    """
    from api.helpers import j

    try:
        require(body, "session_id")
    except ValueError as e:
        return bad(handler, str(e))
    sid = str(body.get("session_id") or "").strip()
    try:
        s = get_session(sid)
    except KeyError:
        return bad(handler, "Session not found", 404)
    # process_id accepted as transitional alias; see Deprecation response header
    # + maintainer decision on removal milestone / future Sunset header. Only
    # flag Deprecation when the alias was ACTUALLY used (i.e. process_id present
    # and not empty), even if task_id is also present.
    _task_id_present = bool(str(body.get("task_id") or "").strip())
    _process_id_present = bool(str(body.get("process_id") or "").strip())
    legacy_process_id_used = _process_id_present
    pid = str(body.get("task_id") or body.get("process_id") or "").strip()
    # Post Option-Z pivot this endpoint owns no state: the server-side drain
    # thread starts the wakeup turn, the browser never re-POSTs /api/chat/start.
    # `noop` is returned so the diagnostic shape stays explicit about that and
    # matches the docstring ("pure no-op for state").
    return j(
        handler,
        {
            "ok": True,
            "session_id": s.session_id,
            "task_id": pid,
            "noop": True,
        },
        extra_headers={"Deprecation": "true"} if legacy_process_id_used else {},
    )


def _handle_session_compression_recovery_start(handler, body):
    try:
        require(body, "session_id")
    except ValueError as e:
        return bad(handler, str(e))
    sid = str(body.get("session_id") or "").strip()
    if not sid:
        return bad(handler, "session_id is required")
    if foreign_session_access.is_view_only(sid):
        return bad(handler, "Subagent sessions are view-only and cannot start compression recovery from WebUI", 400)
    try:
        source = get_session(sid)
    except KeyError:
        return bad(handler, "Session not found", 404)
    if not _session_visible_to_active_profile(getattr(source, "profile", None), handler):
        return bad(handler, "Session not found", 404)
    recovery = compression_recovery_payload_for_session(source)
    if not recovery:
        return bad(handler, "Session does not have a compression recovery action.", 409)
    action = str(recovery.get("recommended_action") or "")
    if action != COMPRESSION_RECOVERY_ACTION_START_FOCUSED:
        return bad(handler, "Unsupported compression recovery action.", 409)

    try:
        copied_session, created = start_or_get_focused_continuation(
            source,
            action,
            fallback_workspace=get_last_workspace(),
        )
    except Exception as e:
        logger.exception("failed to persist compression recovery session for %s", sid)
        return bad(
            handler,
            f"Failed to start compression recovery: {_sanitize_error(e)}",
            500,
        )
    if created:
        publish_session_list_changed(
            "session_compression_recovery",
            profile=getattr(copied_session, "profile", None),
            session_id=getattr(copied_session, "session_id", None),
        )
    session_payload = redact_session_data(copied_session.compact() | {"messages": copied_session.messages})
    return j(
        handler,
        {
            "ok": True,
            "session": session_payload,
            "source_session_id": sid,
            "recommended_recovery_action": action,
            "message": (
                "Started a focused continuation. Describe the next narrow task to continue."
                if created
                else "Opened the existing focused continuation for this exhausted session."
            ),
        },
    )


def _handle_goal_command(handler, body):
    """Handle WebUI /goal command controls and optional kickoff stream."""
    try:
        require(body, "session_id")
    except ValueError as e:
        return bad(handler, str(e))
    if foreign_session_access.is_view_only(str(body.get("session_id") or "")):
        return bad(handler, "Subagent sessions are view-only and cannot run /goal from WebUI", 400)
    try:
        s = get_session(body["session_id"])
    except KeyError:
        return bad(handler, "Session not found", 404)

    requested_profile = str(body.get("profile") or "").strip()
    if requested_profile:
        try:
            from api.profiles import is_valid_profile_id

            if requested_profile != "default" and not is_valid_profile_id(requested_profile):
                return bad(handler, "invalid profile", 400)
        except ImportError:
            requested_profile = ""
    if requested_profile and not _profiles_match(getattr(s, "profile", None), requested_profile):
        has_persisted_turns = bool(
            getattr(s, "messages", None)
            or getattr(s, "context_messages", None)
            or getattr(s, "pending_user_message", None)
        )
        if not has_persisted_turns:
            s.profile = requested_profile

    current_stream_id = getattr(s, "active_stream_id", None)
    stream_running = False
    if current_stream_id:
        stream_running = runtime_stream_alive(current_stream_id)
        if not stream_running:
            _clear_stale_stream_state(s)

    try:
        from api.profiles import get_hermes_home_for_profile

        profile_home = get_hermes_home_for_profile(getattr(s, "profile", None))
    except Exception:
        profile_home = None

    from api.goals import goal_command_payload, goal_state_snapshot, restore_goal_state

    goal_args = str(body.get("args", "") or body.get("text", "") or "")
    goal_action = goal_args.strip().lower()
    will_kickoff = bool(
        goal_args.strip()
        and goal_action not in ("status", "pause", "resume", "clear", "stop", "done")
        and not stream_running
    )
    workspace = model = model_provider = normalized_model = None
    previous_goal_state = None
    if will_kickoff:
        try:
            workspace = str(resolve_trusted_workspace(body.get("workspace") or s.workspace))
        except ValueError as e:
            return bad(handler, str(e))
        requested_model = body.get("model") or s.model
        requested_provider = (
            body.get("model_provider")
            if "model_provider" in body
            else getattr(s, "model_provider", None)
        )
        _pp_provider, _pp_default, _pp_cfg = _read_profile_model_config(s, requested_provider)
        model, model_provider, normalized_model = _resolve_compatible_session_model_state(
            requested_model,
            requested_provider,
            profile_provider=_pp_provider,
            profile_default_model=_pp_default,
            profile_config=_pp_cfg,
        )
        previous_goal_state = goal_state_snapshot(s.session_id, profile_home=profile_home)

    from api.runs.adapter import LegacyJournalRuntimeAdapter, runtime_adapter_enabled

    def _legacy_goal_update(session_id: str, _action: str, text: str) -> dict:
        return goal_command_payload(
            session_id,
            text,
            stream_running=stream_running,
            profile_home=profile_home,
        )

    goal_adapter_action = _runtime_adapter_goal_action(goal_args)
    if runtime_adapter_enabled():
        adapter = LegacyJournalRuntimeAdapter(goal_delegate=_legacy_goal_update)
        control_result = adapter.update_goal(
            s.session_id,
            goal_adapter_action,
            goal_args,
        )
        # Slice 3c keeps the adapter as a structural seam only.  Preserve the
        # public /api/goal response by passing through the legacy payload rather
        # than deriving HTTP behavior from ControlResult.accepted/status.
        payload = dict(control_result.payload)
    else:
        payload = _legacy_goal_update(s.session_id, goal_adapter_action, goal_args)
    if not payload.get("ok", True):
        status = 409 if payload.get("error") == "agent_running" else 400
        return j(handler, payload, status=status)

    kickoff_prompt = str(payload.get("kickoff_prompt") or "").strip()
    if kickoff_prompt:
        if workspace is None:
            try:
                workspace = str(resolve_trusted_workspace(body.get("workspace") or s.workspace))
            except ValueError as e:
                return bad(handler, str(e))
        if model is None:
            requested_model = body.get("model") or s.model
            requested_provider = (
                body.get("model_provider")
                if "model_provider" in body
                else getattr(s, "model_provider", None)
            )
            _pp_provider, _pp_default, _pp_cfg = _read_profile_model_config(s, requested_provider)
            model, model_provider, normalized_model = _resolve_compatible_session_model_state(
                requested_model,
                requested_provider,
                profile_provider=_pp_provider,
                profile_default_model=_pp_default,
                profile_config=_pp_cfg,
            )
        try:
            stream_response = _start_chat_stream_for_session(
                s,
                msg=kickoff_prompt,
                attachments=[],
                workspace=workspace,
                model=model,
                model_provider=model_provider,
                normalized_model=normalized_model,
                goal_related=True,
                external_runtime_owned=webui_gateway_chat_enabled(get_config()),
            )
        except Exception:
            restore_goal_state(s.session_id, previous_goal_state, profile_home=profile_home)
            raise
        status = int(stream_response.pop("_status", 200) or 200)
        payload.update(stream_response)
        if status >= 400:
            restore_goal_state(s.session_id, previous_goal_state, profile_home=profile_home)
            payload["ok"] = False
            return j(handler, payload, status=status)

    return j(handler, payload)
