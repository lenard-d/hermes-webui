"""Chat turn startup, runtime recovery, and synchronous completion lifecycle."""

# Implementations are rebound to the canonical facade for compatibility.
# ruff: noqa: F821

from __future__ import annotations


def _handle_sessions_cleanup(handler, body, zero_only=False):
    result = cleanup_session_store(zero_only=zero_only)
    return j(
        handler,
        {
            "ok": True,
            "cleaned": result.cleaned,
            "skipped_active": result.skipped_active,
        },
    )


def _handle_btw(handler, body):
    """POST /api/btw — ephemeral side question using session context.

    Creates a temporary hidden session, streams the answer via SSE, then
    discards the session. The parent session is not modified.
    """
    try:
        require(body, "session_id")
        require(body, "question")
    except ValueError as e:
        return bad(handler, str(e))
    stale_response = _agent_runtime_barrier_response(runner_local_owned=False)
    if stale_response is not None:
        return j(handler, stale_response, status=409)
    if _session_is_subagent_view_only(str(body.get("session_id") or "")):
        return bad(handler, "Subagent sessions are view-only and cannot be used for /btw from WebUI", 400)
    try:
        s = get_session(body["session_id"])
    except KeyError:
        return bad(handler, "Session not found", 404)
    question = str(body["question"]).strip()
    if not question:
        return bad(handler, "question is required")
    # Duplicate-stream guard (same pattern as chat/start)
    current_stream_id = getattr(s, "active_stream_id", None)
    if current_stream_id:
        if runtime_stream_alive(current_stream_id):
            return j(handler, {"error": "session already has an active stream"}, status=409)
        s.active_stream_id = None
    # Create ephemeral hidden session inheriting context
    from api.sessions.store import new_session as _new_session
    model_provider = getattr(s, 'model_provider', None)
    ephemeral = _new_session(
        workspace=s.workspace,
        model=s.model,
        model_provider=model_provider,
        profile=getattr(s, 'profile', None),
    )
    # Copy conversation history for context (agent reads from messages)
    ephemeral.messages = list(s.messages or [])
    ephemeral.title = f"btw: {question[:60]}"
    ephemeral.save()
    stream_id = uuid.uuid4().hex
    ephemeral.active_stream_id = stream_id
    ephemeral.save()
    stream = create_stream_channel()
    register_runtime_stream(stream_id, ephemeral.session_id, stream)
    from api.runs.background import track_btw
    track_btw(body["session_id"], ephemeral.session_id, stream_id, question)
    thr = threading.Thread(
        target=_run_agent_streaming,
        args=(ephemeral.session_id, question, s.model, s.workspace, stream_id, None),
        kwargs={"ephemeral": True, "model_provider": model_provider},
        daemon=True,
    )
    thr.start()
    return j(handler, {"stream_id": stream_id, "session_id": ephemeral.session_id, "parent_session_id": body["session_id"]})


def _handle_background(handler, body):
    """POST /api/background — run prompt in parallel background agent.

    Creates a hidden session, starts streaming in a daemon thread.
    Frontend polls /api/background/status for completed results.
    """
    try:
        require(body, "session_id")
        require(body, "prompt")
    except ValueError as e:
        return bad(handler, str(e))
    stale_response = _agent_runtime_barrier_response(runner_local_owned=False)
    if stale_response is not None:
        return j(handler, stale_response, status=409)
    try:
        s = get_session(body["session_id"])
    except KeyError:
        return bad(handler, "Session not found", 404)
    prompt = str(body["prompt"]).strip()
    if not prompt:
        return bad(handler, "prompt is required")
    from api.sessions.store import new_session as _new_session
    model_provider = getattr(s, 'model_provider', None)
    bg = _new_session(
        workspace=s.workspace,
        model=s.model,
        model_provider=model_provider,
        profile=getattr(s, 'profile', None),
    )
    bg.title = f"bg: {prompt[:60]}"
    bg.save()
    stream_id = uuid.uuid4().hex
    bg.active_stream_id = stream_id
    bg.save()
    stream = create_stream_channel()
    register_runtime_stream(stream_id, bg.session_id, stream)
    task_id = uuid.uuid4().hex[:8]
    from api.runs.background import track_background, complete_background
    parent_sid = body["session_id"]
    bg_sid = bg.session_id
    track_background(parent_sid, bg_sid, stream_id, task_id, prompt)

    def _run_bg_and_notify():
        """Run the background agent, then mark the tracked task `done` with the
        last assistant reply so `/api/background/status` can surface it.  Without
        this, `complete_background()` is never called and the result is lost —
        `get_results()` would see a forever-`running` task and return nothing.
        """
        try:
            _run_agent_streaming(
                bg_sid,
                prompt,
                s.model,
                s.workspace,
                stream_id,
                None,
                model_provider=model_provider,
            )
            # Reload the bg session from disk and extract the final assistant reply.
            try:
                from api.sessions.store import Session as _Session
                reloaded = _Session.load(bg_sid)
                _answer = ""
                for _m in reversed((reloaded.messages if reloaded else None) or []):
                    if not isinstance(_m, dict) or _m.get("role") != "assistant":
                        continue
                    if _m.get("_error"):
                        continue
                    _content = str(_m.get("content") or "").strip()
                    if _content:
                        _answer = _content
                        break
                complete_background(parent_sid, task_id, _answer or "(no answer produced)")
            except Exception:
                complete_background(parent_sid, task_id, "(background task failed)")
            # Best-effort cleanup of the hidden bg session file so it doesn't
            # clutter the sidebar or SESSION_DIR. The index is pruned on the
            # next rebuild via _index_entry_exists().
            try:
                (SESSION_DIR / f"{bg_sid}.json").unlink(missing_ok=True)
            except Exception:
                pass
        except Exception:
            try:
                complete_background(parent_sid, task_id, "(background task failed)")
            except Exception:
                pass

    thr = threading.Thread(target=_run_bg_and_notify, daemon=True)
    thr.start()
    return j(handler, {"task_id": task_id, "stream_id": stream_id, "session_id": bg.session_id})


def _active_run_stream_for_session(session_id: str | None) -> str | None:
    """Return a live worker stream for this session even if sidecar stream id is clear.

    cancel_stream() intentionally clears ``session.active_stream_id`` before the
    worker thread fully exits so Stop remains responsive. During that unwind
    window ACTIVE_RUNS is the worker-lifecycle truth; a successor chat/start for
    the same session must wait or it can reuse the cached agent while the old
    interrupt is still landing (#3808).

    Bounded: the post-cancel unwind is short (dominated by the worker finally's
    ``_ckpt_thread.join(timeout=15)``), and ``unregister_active_run`` runs in that
    finally, so a healthy worker leaves ACTIVE_RUNS within seconds. A detached /
    wedged worker that never reaches its finally (e.g. stuck in a provider call,
    or leaked by SIGKILL without restart) must NOT 409 the session forever — so an
    entry older than the unwind ceiling (180s) is treated as stale and ignored
    here. A legitimately long-running turn keeps ``active_stream_id`` SET
    and is handled by turn admission; this helper covers only the
    cleared-stream-id unwind window. (Codex brick-gate hardening, #3822.)
    """
    return blocking_runtime_stream(str(session_id or ""))


def _agent_runtime_barrier_response(
    *,
    runner_local_owned: bool = False,
    external_runtime_owned: bool | None = None,
) -> dict | None:
    """Return the typed stale-runtime response for local in-process turns."""
    if external_runtime_owned is True:
        return None
    if runner_local_owned and webui_gateway_chat_enabled(get_config()):
        return None
    from api.runs.adapter import runtime_adapter_runner_enabled

    if runner_local_owned and runtime_adapter_runner_enabled():
        return None
    try:
        ensure_agent_runtime_current()
    except AgentRuntimeChangedError as exc:
        return {
            "error": str(exc),
            "type": "agent_runtime_stale",
            "retryable": True,
        }
    return None


def _start_chat_stream_for_session(
    s,
    *,
    msg: str,
    attachments=None,
    workspace: str,
    model: str,
    model_provider=None,
    normalized_model: bool = False,
    diag=None,
    goal_related: bool = False,
    source: str = "webui",
    moa_config=None,
    external_runtime_owned: bool | None = None,
):
    """Select the execution owner and delegate local admission as one transition."""
    if external_runtime_owned is None:
        external_runtime_owned = webui_gateway_chat_enabled(get_config())
    backend_is_gateway = bool(external_runtime_owned)
    stale_response = _agent_runtime_barrier_response(
        external_runtime_owned=backend_is_gateway,
    )
    if stale_response is not None:
        stale_response["_status"] = 409
        return stale_response
    worker_target = _run_gateway_chat_streaming if backend_is_gateway else _run_agent_streaming
    return start_local_turn(
        s,
        LocalTurnRequest(
            message=msg,
            attachments=list(attachments or []),
            workspace=workspace,
            model=model,
            model_provider=model_provider,
            normalized_model=normalized_model,
            goal_related=goal_related,
            source=source,
            moa_config=moa_config if not backend_is_gateway else None,
        ),
        worker_target=worker_target,
        clear_stale_stream=_clear_stale_stream_state,
        diag=diag,
    )


def _runtime_runner_client_factory():
    """Return the configured runner-local client.

    `runner-local` remains default-off and bounded: without an explicit runner
    endpoint this factory preserves the existing "runner-local chat backend is
    not configured" 501 path. When
    `HERMES_WEBUI_RUNNER_BASE_URL` is set, the WebUI process only acts as a
    transport client; the runner endpoint owns execution, run ids, replay, and
    controls.
    """
    # Keep this literal here for route-level contract tests and readable 501 provenance:
    # "runner-local chat backend is not configured"
    from api.runner_client import HttpRunnerClient

    return HttpRunnerClient.from_env()


def _chat_start_response_from_run_start(result):
    """Expose only the legacy browser-facing chat-start response fields."""
    payload = dict(getattr(result, "payload", {}) or {})
    response = {}
    for key in (
        "stream_id",
        "session_id",
        "pending_started_at",
        "turn_id",
        "title",
        "effective_model",
        "effective_model_provider",
        "error",
        "active_stream_id",
        "_status",
    ):
        if key in payload:
            response[key] = payload[key]
    response.setdefault("stream_id", result.stream_id)
    response.setdefault("session_id", result.session_id)
    return response


def _runtime_adapter_goal_action(goal_args: str) -> str:
    """Return the bounded RuntimeAdapter goal action for WebUI /goal args."""
    action = str(goal_args or "").strip().lower()
    if not action or action == "status":
        return "status"
    if action in ("pause", "resume"):
        return action
    if action in ("clear", "stop", "done"):
        return "clear"
    return "set"


def _start_run(
    s,
    *,
    msg: str,
    attachments,
    workspace: str,
    model,
    model_provider,
    normalized_model,
    source: str,
    route: str,
    diag=None,
    moa_config=None,
):
    """Shared start-run helper for /api/chat/start and start_session_turn.

    Centralizes the runtime-adapter selection block (Q-2979-A2 / Copilot
    discussion_r3305864087/r3305864173) so both entrypoints honor
    ``runtime_adapter_enabled()`` / ``runtime_adapter_runner_enabled()`` the
    same way. Prior to this helper ``start_session_turn`` bypassed the
    adapter path entirely, so a process-wakeup turn skipped the adapter that
    a human-typed turn would have hit — a behavioral divergence.

    ``source`` is the StartRunRequest.source (``"webui"`` for browser POSTs,
    ``"process_wakeup"`` for the drain-thread wakeup). ``route`` is the
    metadata.route label that lands on the run record for observability.

    Returns a dict with ``_status`` plus the legacy chat-start response
    fields (``stream_id``, ``session_id``, etc.). Adapter selection that
    returns no adapter is surfaced as ``{"error": str(exc), "_status": 501}``
    so both call sites can map it onto their own HTTP shape.
    """
    from api.runs.adapter import (
        LegacyJournalRuntimeAdapter,
        StartRunRequest,
        build_runtime_adapter,
        runtime_adapter_enabled,
        runtime_adapter_runner_enabled,
    )

    if runtime_adapter_enabled() or runtime_adapter_runner_enabled():
        def _legacy_start_run(request: StartRunRequest) -> dict:
            return _start_chat_stream_for_session(
                s,
                msg=request.message,
                attachments=request.attachments,
                workspace=request.workspace or workspace,
                model=request.model or model,
                model_provider=request.provider or model_provider,
                normalized_model=normalized_model,
                diag=diag,
                source=request.source or source,
                moa_config=moa_config,
            )

        def _legacy_adapter_factory():
            return LegacyJournalRuntimeAdapter(start_run_delegate=_legacy_start_run)

        try:
            adapter = build_runtime_adapter(
                legacy_adapter_factory=_legacy_adapter_factory,
                runner_client_factory=_runtime_runner_client_factory,
            )
            if adapter is None:
                raise NotImplementedError("runtime adapter selection returned no adapter")
            result = adapter.start_run(
                StartRunRequest(
                    session_id=s.session_id,
                    message=msg,
                    attachments=attachments,
                    workspace=workspace,
                    profile=getattr(s, "profile", None),
                    provider=model_provider,
                    model=model,
                    source=source,
                    metadata={"route": route},
                )
            )
        except NotImplementedError as exc:
            return {"error": str(exc), "_status": 501}
        return _chat_start_response_from_run_start(result)

    return _start_chat_stream_for_session(
        s,
        msg=msg,
        attachments=attachments,
        workspace=workspace,
        model=model,
        model_provider=model_provider,
        normalized_model=normalized_model,
        diag=diag,
        source=source,
        moa_config=moa_config,
        external_runtime_owned=webui_gateway_chat_enabled(get_config()),
    )


def _process_wakeup_revalidation_provider(model, provider) -> str:
    """Return the canonical provider id used for wakeup credential revalidation."""
    try:
        _resolved_model, resolved_provider = canonical_model_provider_lane(model, provider)
    except Exception:
        logger.debug(
            "failed to canonicalize process_wakeup revalidation lane for model=%r provider=%r",
            model,
            provider,
            exc_info=True,
        )
        resolved_provider = None
    candidate = resolved_provider if resolved_provider else provider
    return str(candidate or "").strip()


def _process_wakeup_provider_has_recovery_credential(
    session,
    *,
    model,
    provider,
    provider_id: str | None = None,
) -> bool:
    """Check paused credential-pool recovery in the owning session profile."""
    provider_id = str(
        provider_id or _process_wakeup_revalidation_provider(model, provider) or ""
    ).strip()
    if not provider_id:
        return False
    profile_name = str(getattr(session, "profile", "") or "").strip()
    if profile_name and not _is_root_profile(profile_name):
        with profile_scope_for_detached_worker(
            profile_name,
            "process_wakeup credential revalidation",
            logger_override=logger,
        ):
            return provider_has_process_wakeup_recovery_credential(provider_id, refresh=True)
    return provider_has_process_wakeup_recovery_credential(provider_id, refresh=True)


def _refresh_process_wakeup_pause_credential_fingerprint(session) -> bool:
    """Refresh the stored credential fingerprint without clearing the pause."""
    pause = getattr(session, "process_wakeup_pause", None)
    if not isinstance(pause, dict) or not pause.get("paused"):
        return False
    updated = dict(pause)
    updated["credential_state_fingerprint"] = process_wakeup_credential_state_fingerprint(session)
    session.process_wakeup_pause = updated
    return True


def start_session_turn(
    session_id: str,
    message: str,
    *,
    source: str = "process_wakeup",
):
    """Start a server-side agent turn for ``session_id`` with ``message``.

    Option Z primary wakeup entrypoint. This is the minimal, HTTP-handler-free
    core that ``/api/chat/start`` already reaches via ``_handle_chat_start`` →
    ``_start_chat_stream_for_session``. The drain thread
    (``api/background_process._process_one``) calls this directly with a
    synthetic ``[IMPORTANT: …]`` wakeup_prompt so a background process can wake
    the agent server-side with NO browser round-trip — exactly how CLI /
    gateway self-wake from a ``notify_on_complete`` completion.

    Contract:
      - Resolves the session record (profile/workspace/model/model_provider are
        already persisted on it; no user auth needed — same trust level as
        gateway/cron starting a turn).
      - Resolves workspace + model/provider through the SAME helpers
        ``_handle_chat_start`` uses, so a process-wakeup turn is constructed
        identically to a human-typed turn. If the session record has no model
        persisted, ``_resolve_compatible_session_model_state`` falls back to the
        configured default model/provider (documented in the impl report §1).
      - Delegates to ``_start_chat_stream_for_session`` which spawns the agent
        on a daemon worker thread (the drain thread NEVER blocks) and serializes
        on the per-session agent lock + active-stream guard, so a concurrent
        human ``/api/chat/start`` cannot double-start (one wins, the other gets
        the existing 409 "session already has an active stream").

    Returns the same dict ``_start_chat_stream_for_session`` returns, including
    ``_status`` (200 on start, 409 when a turn is already active). On 409 the
    caller must leave the ``PENDING_BG_TASK_COMPLETIONS`` marker in place so the
    PR #2279 next-turn drain delivers the wakeup when the active turn ends.
    """
    msg = str(message or "").strip()
    if not msg:
        return {"error": "message is required", "_status": 400}
    stale_response = _agent_runtime_barrier_response(runner_local_owned=True)
    if stale_response is not None:
        stale_response["_status"] = 409
        return stale_response
    turn_source = str(source or "process_wakeup").strip() or "process_wakeup"
    try:
        s = get_session(session_id)
    except KeyError:
        return {"error": "Session not found", "_status": 404}

    try:
        workspace = _resolve_chat_workspace_with_recovery(s, None)
    except ValueError as e:
        return {"error": str(e), "_status": 400}

    requested_model = s.model
    requested_provider = getattr(s, "model_provider", None)
    # Server-initiated wakeup (Option Z): resolve persisted model via the
    # standard helper in cache-only mode so wakeups never trigger a cold
    # catalog rebuild. Thread the session's PROFILE model defaults through too
    # (mirrors _handle_chat_start) — a brand-new session that spawned a
    # background task before its first human turn has an empty s.model, and
    # without the profile defaults the resolver would fall back to the global
    # DEFAULT_MODEL instead of the profile's configured default (greptile flag).
    _pp_provider, _pp_default, _pp_cfg = _read_profile_model_config(s, requested_provider)
    model, model_provider, normalized_model = _resolve_compatible_session_model_state(
        requested_model,
        requested_provider,
        profile_provider=_pp_provider,
        profile_default_model=_pp_default,
        profile_config=_pp_cfg,
        prefer_cached_catalog=True,
    )
    _paused_wakeup_response = None
    with _get_session_agent_lock(s.session_id):
        try:
            s = get_session(session_id)
        except KeyError:
            return {"error": "Session not found", "_status": 404}
        if clear_process_wakeup_pause_if_model_changed(
            s,
            model=model,
            provider=model_provider,
        ):
            try:
                s.save(touch_updated_at=False)
            except Exception:
                logger.debug(
                    "failed to persist process_wakeup pause reset for session %s",
                    session_id,
                    exc_info=True,
                )
        if turn_source == "process_wakeup":
            _credential_state_changed = False
            try:
                _credential_state_changed = process_wakeup_pause_credential_state_changed(s)
            except Exception:
                logger.debug(
                    "failed to compare process_wakeup credential state for session %s",
                    session_id,
                    exc_info=True,
                )
            if process_wakeup_pause_matches(
                s,
                model=model,
                provider=model_provider,
                classification='credential_pool_empty',
            ):
                _credential_recovered = False
                _credential_revalidation_provider = _process_wakeup_revalidation_provider(
                    model,
                    model_provider,
                )
                try:
                    _credential_recovered = _process_wakeup_provider_has_recovery_credential(
                        s,
                        model=model,
                        provider=model_provider,
                        provider_id=_credential_revalidation_provider,
                    )
                except Exception:
                    logger.debug(
                        "failed to revalidate process_wakeup credential availability for session %s",
                        session_id,
                        exc_info=True,
                    )
                if _credential_recovered:
                    _recovery_reason = (
                        'credential_state_changed'
                        if _credential_state_changed
                        else 'credential_recovered'
                    )
                    if clear_process_wakeup_pause(s, reason=_recovery_reason):
                        try:
                            s.save(touch_updated_at=False)
                        except Exception:
                            logger.debug(
                                "failed to persist process_wakeup credential recovery reset for session %s",
                                session_id,
                                exc_info=True,
                            )
                elif _credential_state_changed:
                    if _refresh_process_wakeup_pause_credential_fingerprint(s):
                        try:
                            s.save(touch_updated_at=False)
                        except Exception:
                            logger.debug(
                                "failed to persist process_wakeup credential-state fingerprint refresh for session %s",
                                session_id,
                                exc_info=True,
                            )
            _paused_wakeup = suppress_process_wakeup_for_provider_pause(
                s,
                model=model,
                provider=model_provider,
                classification='credential_pool_empty',
            )
            if _paused_wakeup is not None:
                try:
                    PENDING_BG_TASK_COMPLETIONS.discard(s.session_id)
                except Exception:
                    logger.debug(
                        "failed to discard pending bg-task marker for paused wakeup %s",
                        session_id,
                        exc_info=True,
                    )
                try:
                    s.save(touch_updated_at=False)
                except Exception:
                    logger.debug(
                        "failed to persist process_wakeup suppression for session %s",
                        session_id,
                        exc_info=True,
                    )
                _paused_wakeup_response = {
                    "error": PROCESS_WAKEUP_PAUSE_ERROR,
                    "message": (
                        "Automatic process wakeups are paused for this session because "
                        "the provider credential pool is unavailable."
                    ),
                    "process_wakeup_pause": _paused_wakeup,
                    "_status": 409,
                }
    if _paused_wakeup_response is not None:
        return _paused_wakeup_response
    resp = _start_run(
        s,
        msg=msg,
        attachments=[],
        workspace=workspace,
        model=model,
        model_provider=model_provider,
        normalized_model=normalized_model,
        source=turn_source,
        route="start_session_turn",
    )

    # ── Defect B: live-view of server-initiated turns ──────────────────────
    # Option Z starts this turn server-side, so NO browser EventSource is
    # attached to the new STREAMS[stream_id] (the browser only opens
    # /api/chat/stream when IT POSTs /api/chat/start). An already-open tab
    # would therefore see nothing until a manual refresh re-reads persisted
    # state. Fix: fan a lightweight `server_turn_started` {stream_id} frame
    # onto the persistent per-session live-view channel. messages.js handles
    # it by attaching its EXISTING chat-stream renderer (attachLiveStream) to
    # that stream_id — no second renderer, no chat/start POST.
    #
    # Idempotent with the closed-tab path: get_session_channel() is the
    # NON-creating accessor, so when no tab is open this is a pure no-op and
    # the server-side wakeup (the Option Z headline) is completely unaffected.
    # If the user also has the per-turn chat-stream open, the frontend dedupes
    # by stream_id so there is no double-render.
    try:
        status = int((resp or {}).get("_status", 200) or 200)
        stream_id = (resp or {}).get("stream_id")
        if status < 400 and stream_id:
            from api.background_process import get_session_channel

            ch = get_session_channel(session_id)
            if ch is not None:
                ch.emit(
                    "server_turn_started",
                    {
                        "session_id": str(session_id),
                        "stream_id": str(stream_id),
                        "pending_started_at": (resp or {}).get("pending_started_at"),
                        "source": source,
                    },
                )
    except Exception:
        logger.debug(
            "server_turn_started fan-out failed for session %s", session_id, exc_info=True
        )
    return resp


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
    if _session_is_subagent_view_only(sid):
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

    created = False
    with _COMPRESSION_RECOVERY_START_LOCK:
        source_profile = getattr(source, "profile", None)
        copied_session = find_compression_recovery_session(sid, action, source_profile=source_profile)
        if copied_session is None:
            title = str(getattr(source, "title", None) or "Untitled").strip() or "Untitled"
            if not title.endswith(" (focused continuation)"):
                title = f"{title} (focused continuation)"
            copied_session = Session(
                session_id=uuid.uuid4().hex[:12],
                title=title,
                workspace=getattr(source, "workspace", get_last_workspace()),
                model=getattr(source, "model", None),
                model_provider=getattr(source, "model_provider", None),
                messages=[],
                tool_calls=[],
                pinned=False,
                archived=False,
                project_id=getattr(source, "project_id", None),
                profile=getattr(source, "profile", None),
                session_source="fork",
                personality=getattr(source, "personality", None),
                enabled_toolsets=copy.deepcopy(getattr(source, "enabled_toolsets", None)),
                context_length=getattr(source, "context_length", None),
                threshold_tokens=getattr(source, "threshold_tokens", None),
                gateway_routing=copy.deepcopy(getattr(source, "gateway_routing", None)),
                gateway_routing_history=copy.deepcopy(getattr(source, "gateway_routing_history", None) or []),
                parent_session_id=getattr(source, "session_id", sid),
                worktree_path=getattr(source, "worktree_path", None),
                worktree_branch=getattr(source, "worktree_branch", None),
                worktree_repo_root=getattr(source, "worktree_repo_root", None),
                worktree_created_at=getattr(source, "worktree_created_at", None),
                compression_recovery_source_session_id=sid,
                compression_recovery_action=action,
            )
            # Preserve the workspace/model/profile lane, but intentionally start with an
            # empty model-facing transcript so a focused follow-up does not replay the
            # exhausted state.db/context tail.
            copied_session.context_messages = []
            copied_session.composer_draft = {"text": "", "files": []}
            try:
                copied_session.save()
            except Exception as e:
                logger.exception("failed to persist compression recovery session for %s", sid)
                return bad(handler, f"Failed to start compression recovery: {_sanitize_error(e)}", 500)

            with LOCK:
                SESSIONS[copied_session.session_id] = copied_session
                SESSIONS.move_to_end(copied_session.session_id)
                _evict_sessions_over_cap()
            created = True
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
    if _session_is_subagent_view_only(str(body.get("session_id") or "")):
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


def _handle_chat_start(handler, body, diag=None):
    try:
        diag.stage("validate_session_id") if diag else None
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        # Reject a stale local Agent runtime before materialising, claiming, or
        # mutating any session state. Gateway-backed turns run in the gateway's
        # process and do not depend on this WebUI process's imported checkout.
        stale_response = _agent_runtime_barrier_response(runner_local_owned=True)
        if stale_response is not None:
            return j(handler, stale_response, status=409)
        diag.stage("get_session") if diag else None
        try:
            s = _get_or_materialize_session(body["session_id"], refresh_cli_messages=True)
        except KeyError:
            # No WebUI sidecar. If this is a foreign-origin session (CLI,
            # TUI, Desktop) with recoverable state.db messages, claim it by
            # materialising a WebUI-owned Session and persisting it as a
            # sidecar. This closes the GET-vs-POST asymmetry where a
            # TUI/Desktop session loads read-only via GET /api/session but
            # 404s on the first POST /api/chat/start, making the typed
            # message disappear into the empty state.
            synth, reason = _claim_or_synthesize_cli_session(body["session_id"])
            if synth is None:
                # 'was_webui' (deleted WebUI session, client should self-heal
                # via the existing 404 path), 'no_foreign_state' (sid has
                # no recoverable state anywhere), or 'invalid_sid' (path
                # safety violation). All collapse to 404 — the client only
                # knows the right thing to do for "this session is gone".
                return bad(handler, "Session not found", 404)
            if reason == "not_claimable":
                # Foreign store says this session is read-only / owned by
                # a non-WebUI process (messaging, claude_code,
                # external_agent, cron, gateway/unknown, or explicit
                # read_only flag). The session is real and viewable, but
                # the WebUI must not take write ownership of it — that
                # would be an ownership-boundary violation (#4911 review).
                # 403 (not 404) because 404 triggers the frontend's
                # empty-state self-heal handler which strips the URL and
                # clears localStorage; for a legitimately-listed read-only
                # session the user should keep their URL and see a refusal,
                # not have their session vanish.
                return bad(
                    handler,
                    "session is read-only in its foreign store; cannot be claimed writeable in WebUI",
                    403,
                )
            try:
                synth.save()
            except Exception as _save_err:
                # Persisting the sidecar failed: surface a generic 500 to
                # the client (paths sanitised, see _sanitize_error) and log
                # the full exception server-side. Returning the raw str(exc)
                # would leak /root/.hermes/webui/sessions/<sid>.json or any
                # other absolute filesystem path the OSError happened to
                # carry — #4911 review feedback.
                logger.exception(
                    "failed to persist materialised sidecar for foreign session %s",
                    body["session_id"],
                )
                return bad(
                    handler,
                    f"failed to claim session: {_sanitize_error(_save_err)}",
                    500,
                )
            s = synth
            try:
                with LOCK:
                    SESSIONS[s.session_id] = s
                    SESSIONS.move_to_end(s.session_id)
            except Exception:
                # If the in-memory LRU refuses the new session, fall through
                # with the just-persisted sidecar; _start_run will load it
                # from disk if needed.
                pass
        except PermissionError:
            return bad(handler, "Read-only imported sessions cannot be continued from WebUI", 403)
        diag.stage("validate_profile") if diag else None
        requested_profile = str(body.get("profile") or "").strip()
        active_profile = _get_active_profile_name()
        if requested_profile:
            try:
                from api.profiles import is_valid_profile_id

                if requested_profile != "default" and not is_valid_profile_id(requested_profile):
                    return bad(handler, "invalid profile", 400)
            except ImportError:
                requested_profile = ""
        session_profile = getattr(s, "profile", None)
        has_persisted_turns = bool(
            getattr(s, "messages", None)
            or getattr(s, "context_messages", None)
            or getattr(s, "pending_user_message", None)
        )
        if not _session_visible_to_active_profile(session_profile, handler):
            if (
                requested_profile
                and _profiles_match(requested_profile, active_profile)
                and not has_persisted_turns
            ):
                # Empty placeholders can still be retagged when the
                # requested profile matches the active request profile.
                s.profile = requested_profile
            else:
                return bad(handler, "Session not found", 404)
        diag.stage("normalize_message") if diag else None
        msg = str(body.get("message", "")).strip()
        if not msg:
            return bad(handler, "message is required")
        diag.stage("normalize_attachments") if diag else None
        attachments = _normalize_chat_attachments(body.get("attachments") or [])[:20]
        recovery = compression_recovery_payload_for_session(s)
        if recovery and not attachments and is_generic_continuation_intent(msg):
            return j(
                handler,
                {
                    "error": "This session exhausted context compression. Start a focused continuation, then describe the next narrow task.",
                    "type": "compression_recovery_required",
                    "recommended_recovery_action": recovery.get("recommended_action"),
                    "compression_recovery": recovery,
                    "session_id": getattr(s, "session_id", body["session_id"]),
                },
                status=409,
            )
        diag.stage("resolve_workspace") if diag else None
        try:
            workspace = _resolve_chat_workspace_with_recovery(s, body.get("workspace"))
        except ValueError as e:
            return bad(handler, str(e))
        requested_model = body.get("model") or s.model
        requested_provider = (
            body.get("model_provider")
            if "model_provider" in body
            else getattr(s, "model_provider", None)
        )
        _pp_provider, _pp_default, _pp_cfg = _read_profile_model_config(s, requested_provider)
        explicit_model_pick = bool(body.get("explicit_model_pick"))
        moa_config = None
        gateway_chat_enabled = webui_gateway_chat_enabled(get_config())
        if body.get("moa_config"):
            if gateway_chat_enabled:
                return bad(handler, "MoA override is unavailable on gateway-backed sessions", 409)
            from api.commands import resolve_moa_config

            try:
                moa_config = resolve_moa_config()
            except RuntimeError as e:
                return bad(handler, str(e), 503)
        diag.stage("resolve_model_provider") if diag else None
        model, model_provider, normalized_model = _resolve_compatible_session_model_state(
            requested_model,
            requested_provider,
            profile_provider=_pp_provider,
            profile_default_model=_pp_default,
            profile_config=_pp_cfg,
            explicit_model_pick=explicit_model_pick,
        )
        # #5979: record a SIGNATURE of the deliberately-picked model+provider so
        # the streaming resolver can preserve a custom-proxy vendor namespace on a
        # cold catalog — but ONLY while the routing context still matches. On a
        # fresh explicit pick, stamp the signature of the resolved model+provider;
        # otherwise leave any prior signature in place (it self-invalidates when
        # the model/provider changes, since the streaming side recomputes and
        # compares). This survives same-model follow-up sends (the onchange marker
        # is one-shot) yet can't outlive a real switch.
        try:
            if explicit_model_pick:
                from api.sessions.store import model_explicit_pick_signature as _mk_sig
                s.model_explicit_pick_signature = _mk_sig(model, model_provider)
        except Exception:
            pass
        catalog_profile_provider = _pp_provider
        if catalog_profile_provider is None and isinstance(_pp_cfg, dict):
            profile_model_config = _pp_cfg.get("model") or {}
            if isinstance(profile_model_config, dict):
                catalog_profile_provider = profile_model_config.get("provider")
        model_provider = _repair_foreign_session_model_provider(
            s,
            requested_model=requested_model,
            requested_provider=requested_provider,
            resolved_model=model,
            resolved_provider=model_provider,
            explicit_model_pick=explicit_model_pick,
            profile_provider=catalog_profile_provider,
        )
        if model_provider == "moa" and moa_config is None:
            if webui_gateway_chat_enabled(get_config()):
                return bad(handler, "MoA override is unavailable on gateway-backed sessions", 409)
            from api.commands import resolve_moa_config

            try:
                moa_config = resolve_moa_config(model)
            except RuntimeError as e:
                return bad(handler, str(e), 503)
        # NOTE: runtime-adapter selection is delegated to _start_run (shared
        # with start_session_turn so both entry points behave identically
        # under runtime_adapter_enabled() / runtime_adapter_runner_enabled()
        # — Q-2979-A2 / Copilot discussion_r3305864087/r3305864173).
        start_run_kwargs = {
            "msg": msg,
            "attachments": attachments,
            "workspace": workspace,
            "model": model,
            "model_provider": model_provider,
            "normalized_model": normalized_model,
            "source": "webui",
            "route": "/api/chat/start",
            "diag": diag,
        }
        if not gateway_chat_enabled and moa_config is not None:
            start_run_kwargs["moa_config"] = moa_config
        recovery_cleared_for_start = None
        def _restore_cleared_recovery():
            if recovery_cleared_for_start is None:
                return None
            s.compression_recovery = recovery_cleared_for_start
            s.recommended_recovery_action = recovery_cleared_for_start.get("recommended_action")
            try:
                s.save()
            except Exception as restore_err:
                logger.exception("failed to restore compression recovery after chat start rejection for %s", getattr(s, "session_id", None))
                return restore_err
            return None

        if recovery:
            recovery_cleared_for_start = copy.deepcopy(recovery)
            clear_compression_recovery(s)
        try:
            response = _start_run(
                s,
                **start_run_kwargs,
            )
        except Exception:
            _restore_cleared_recovery()
            raise
        # Map adapter-selection NotImplementedError (501) onto the legacy
        # bad-request response shape that this route exposed historically
        # before the helper extraction.
        if response.get("_status") == 501 and "error" in response:
            restore_err = _restore_cleared_recovery()
            if restore_err is not None:
                return bad(handler, f"failed to restore compression recovery: {_sanitize_error(restore_err)}", 500)
            return j(handler, {"error": response["error"]}, status=501)
        status = int(response.pop("_status", 200) or 200)
        if status >= 400 and recovery_cleared_for_start is not None:
            restore_err = _restore_cleared_recovery()
            if restore_err is not None:
                return bad(handler, f"failed to restore compression recovery: {_sanitize_error(restore_err)}", 500)
        diag.stage("response_write") if diag else None
        return j(handler, response, status=status)
    finally:
        if diag:
            diag.finish()



def _resolve_chat_workspace_with_recovery(s, requested_workspace) -> str:
    """Recover stale implicit session workspaces without hiding explicit errors."""
    explicit = requested_workspace not in (None, "")
    candidate = requested_workspace if explicit else getattr(s, "workspace", None)
    try:
        return str(resolve_trusted_workspace(candidate))
    except ValueError:
        if explicit:
            raise
    fallback = str(resolve_trusted_workspace(get_last_workspace()))
    s.workspace = fallback
    try:
        s.save()
    except Exception:
        pass
    return fallback


def _normalize_chat_attachments(raw_attachments):
    """Normalize attachment payloads from the browser.

    Older clients send a list of filenames. Newer clients send upload result
    objects containing name/path/mime/size so image attachments can be supplied
    to Hermes as native multimodal inputs for the current turn.
    """
    normalized = []
    if not isinstance(raw_attachments, list):
        return normalized
    for item in raw_attachments:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("filename") or "").strip()
            path = str(item.get("path") or "").strip()
            mime = str(item.get("mime") or "").strip()
            att = {"name": name or path, "path": path, "mime": mime}
            size = item.get("size")
            if isinstance(size, int):
                att["size"] = size
            is_image = item.get("is_image")
            if isinstance(is_image, bool):
                att["is_image"] = is_image
            normalized.append(att)
        else:
            value = str(item).strip()
            if value:
                normalized.append({"name": value, "path": "", "mime": ""})
    return normalized


def _handle_chat_sync(handler, body):
    """Fallback synchronous chat endpoint (POST /api/chat). Not used by frontend."""
    stale_response = _agent_runtime_barrier_response(runner_local_owned=False)
    if stale_response is not None:
        return j(handler, stale_response, status=409)
    if _session_is_subagent_view_only(str(body.get("session_id") or "")):
        return bad(handler, "Subagent sessions are view-only and cannot be written from WebUI", 400)
    s = get_session(body["session_id"])
    msg = str(body.get("message", "")).strip()
    if not msg:
        return j(handler, {"error": "empty message"}, status=400)
    try:
        workspace = str(resolve_trusted_workspace(body.get("workspace") or s.workspace))
    except ValueError as e:
        return bad(handler, str(e))
    with _get_session_agent_lock(s.session_id):
        s.workspace = workspace
        _sync_requested_provider = (
            body.get("model_provider") if "model_provider" in body else getattr(s, "model_provider", None)
        )
        _pp_provider, _pp_default, _pp_cfg = _read_profile_model_config(s, _sync_requested_provider)
        model, model_provider = _resolve_compatible_session_model_state(
            body.get("model") or s.model,
            _sync_requested_provider,
            profile_provider=_pp_provider,
            profile_default_model=_pp_default,
            profile_config=_pp_cfg,
        )[:2]
        s.model = model
        s.model_provider = model_provider
    from api.streaming import _ENV_LOCK

    with _ENV_LOCK:
        old_cwd = os.environ.get("TERMINAL_CWD")
        os.environ["TERMINAL_CWD"] = str(workspace)
        old_exec_ask = os.environ.get("HERMES_EXEC_ASK")
        old_session_key = os.environ.get("HERMES_SESSION_KEY")
        os.environ["HERMES_EXEC_ASK"] = "1"
        os.environ["HERMES_SESSION_KEY"] = s.session_id
    try:
        AIAgent = require_ai_agent_class()

        with CHAT_LOCK:
            from api.config import (
                resolve_model_provider,
                resolve_custom_provider_connection,
            )

            _model, _provider, _base_url = resolve_model_provider(
                model_with_provider_context(s.model, getattr(s, "model_provider", None))
            )
            # Resolve API key via Hermes runtime provider (matches gateway behaviour)
            _api_key = None
            try:
                from api.auth import resolve_runtime_provider_with_anthropic_env_lock
                from hermes_cli.runtime_provider import resolve_runtime_provider

                _rt = resolve_runtime_provider_with_anthropic_env_lock(
                    resolve_runtime_provider,
                    requested=_provider,
                )
                _api_key = _rt.get("api_key")
                # Also use runtime provider/base_url if the webui config didn't resolve them
                if not _provider:
                    _provider = _rt.get("provider")
                if not _base_url:
                    _base_url = _rt.get("base_url")
            except Exception as _e:
                print(
                    f"[webui] WARNING: resolve_runtime_provider failed: {_e}",
                    flush=True,
                )
            if isinstance(_provider, str) and _provider.startswith("custom:"):
                _cp_key, _cp_base = resolve_custom_provider_connection(_provider)
                if not _api_key and _cp_key:
                    _api_key = _cp_key
                if not _base_url and _cp_base:
                    _base_url = _cp_base
            agent = AIAgent(
                model=_model,
                provider=_provider,
                base_url=_base_url,
                api_key=_api_key,
                # Identify browser-originated sessions as WebUI so Hermes Agent
                # does not inject CLI-specific terminal/output guidance.
                platform="webui",
                quiet_mode=True,
                enabled_toolsets=_resolve_cli_toolsets(),
                session_id=s.session_id,
            )
            from api.streaming import (
                _WEBUI_PROGRESS_PROMPT,
                _assign_stable_message_ids,
                _dedupe_replayed_context_messages,
                _merge_display_messages_after_agent_result,
                _restore_display_reasoning_metadata,
                _restore_reasoning_metadata,
                _sanitize_messages_for_api,
                _context_messages_for_new_turn,
                _workspace_context_prefix,
            )
            workspace_ctx = _workspace_context_prefix(str(s.workspace))
            workspace_system_msg = (
                f"Active workspace at session start: {s.workspace}\n"
                "Every user message is prefixed with [Workspace::v1: /absolute/path] indicating the "
                "workspace the user has selected in the web UI at the time they sent that message. "
                "This tag is the single authoritative source of the active workspace and updates "
                "with every message. It overrides any prior workspace mentioned in this system "
                "prompt, memory, or conversation history. Always use the value from the most recent "
                "[Workspace::v1: ...] tag as your default working directory for ALL file operations: "
                "write_file, read_file, search_files, terminal workdir, and patch. "
                "Never fall back to a hardcoded path when this tag is present.\n\n"
                f"{_WEBUI_PROGRESS_PROMPT}\n\n"
                "WebUI external-notes/durable-memory policy: Do not copy or dump this browser transcript "
                "into external notes or durable memory by default. Write or update durable "
                "notes only for explicit captures, durable preferences, decisions, blockers/open "
                "issues, runbook-worthy workflows, or other clearly reusable signals; otherwise "
                "leave external notes and durable memory unchanged. When you do write or update a durable note, briefly tell "
                "the user what note or section changed so the write is reviewable."
            )

            _previous_messages = list(s.messages or [])
            _previous_context_messages = list(_context_messages_for_new_turn(s, msg))

            result = agent.run_conversation(
                user_message=workspace_ctx + msg,
                system_message=workspace_system_msg,
                conversation_history=_sanitize_messages_for_api(
                    _previous_context_messages,
                    cfg=get_config(),
                    effective_model=_model,
                    effective_provider=_provider,
                    effective_base_url=_base_url,
                ),
                task_id=s.session_id,
                persist_user_message=msg,
            )
    finally:
        with _ENV_LOCK:
            if old_cwd is None:
                os.environ.pop("TERMINAL_CWD", None)
            else:
                os.environ["TERMINAL_CWD"] = old_cwd
            if old_exec_ask is None:
                os.environ.pop("HERMES_EXEC_ASK", None)
            else:
                os.environ["HERMES_EXEC_ASK"] = old_exec_ask
            if old_session_key is None:
                os.environ.pop("HERMES_SESSION_KEY", None)
            else:
                os.environ["HERMES_SESSION_KEY"] = old_session_key
    with _get_session_agent_lock(s.session_id):
        _result_messages = result.get("messages") or _previous_context_messages
        _next_context_messages = _restore_reasoning_metadata(
            _previous_context_messages,
            _result_messages,
        )
        # Mint ids on the shared result rows BEFORE dedupe deep-copies any
        # stale-user boundary row, so both arrays share the id (#5564).
        _assign_stable_message_ids(
            _result_messages, _previous_messages, _previous_context_messages
        )
        _next_context_messages = _dedupe_replayed_context_messages(
            _previous_context_messages,
            _next_context_messages,
            msg,
        )
        s.context_messages = _next_context_messages
        s.messages = _merge_display_messages_after_agent_result(
            _previous_messages,
            _previous_context_messages,
            _restore_display_reasoning_metadata(_previous_messages, _result_messages),
            msg,
            source=getattr(s, "pending_user_source", None) or "webui",
        )
        # Only auto-generate title when still default; preserves user renames
        if s.title == "Untitled":
            s.title = title_from(s.messages, s.title)
        s.save()
    # Sync to state.db for /insights (opt-in setting)
    try:
        if load_settings().get("sync_to_insights"):
            from api.state_sync import sync_session_usage

            sync_session_usage(
                session_id=s.session_id,
                input_tokens=s.input_tokens or 0,
                output_tokens=s.output_tokens or 0,
                estimated_cost=s.estimated_cost,
                model=s.model,
                title=s.title,
                message_count=len(s.messages),
                cache_read_tokens=s.cache_read_tokens or 0,
                cache_write_tokens=s.cache_write_tokens or 0,
                # #2762 / #2827 parity with api/streaming.py:5078: pass the
                # session's profile explicitly so a future refactor that
                # backgrounds this handler doesn't silently leak writes to
                # the wrong profile's state.db. HTTP thread today, but
                # defense-in-depth. Opus pre-release advisor MUST-FIX.
                profile=getattr(s, 'profile', None),
            )
    except Exception:
        logger.debug("Failed to update session cost tracking")
    return j(
        handler,
        {
            "answer": result.get("final_response") or "",
            "status": "done" if result.get("completed", True) else "partial",
            "session": s.compact() | {"messages": s.messages},
            "result": {k: v for k, v in result.items() if k != "messages"},
        },
    )


__routes_exports__ = (
    "_handle_sessions_cleanup",
    "_handle_btw",
    "_handle_background",
    "_active_run_stream_for_session",
    "_agent_runtime_barrier_response",
    "_start_chat_stream_for_session",
    "_runtime_runner_client_factory",
    "_chat_start_response_from_run_start",
    "_runtime_adapter_goal_action",
    "_start_run",
    "_process_wakeup_revalidation_provider",
    "_process_wakeup_provider_has_recovery_credential",
    "_refresh_process_wakeup_pause_credential_fingerprint",
    "start_session_turn",
    "_handle_bg_task_complete_ack",
    "_handle_session_compression_recovery_start",
    "_handle_goal_command",
    "_handle_chat_start",
    "_resolve_chat_workspace_with_recovery",
    "_normalize_chat_attachments",
    "_handle_chat_sync",
)
