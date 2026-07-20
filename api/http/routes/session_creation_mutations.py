"""Cohesive HTTP route group used by the transport composition root."""

from __future__ import annotations

from api.http.context import RouteContext, UNHANDLED
from api.sessions import foreign_session_access


def handle_post(handler, parsed, body, diag, ctx: RouteContext):
    Session = ctx["Session"]
    _handle_session_compression_recovery_start = ctx[
        "_handle_session_compression_recovery_start"
    ]
    _session_id_visible_to_request_profile = ctx[
        "_session_id_visible_to_request_profile"
    ]
    _session_model_state_from_request = ctx["_session_model_state_from_request"]
    _validate_session_toolsets_shape = ctx["_validate_session_toolsets_shape"]
    _worktree_default_from_config = ctx["_worktree_default_from_config"]
    bad = ctx["bad"]
    copy = ctx["copy"]
    get_last_workspace = ctx["get_last_workspace"]
    j = ctx["j"]
    logger = ctx["logger"]
    new_session = ctx["new_session"]
    publish_session_list_changed = ctx["publish_session_list_changed"]
    resolve_trusted_workspace = ctx["resolve_trusted_workspace"]
    threading = ctx["threading"]
    time = ctx["time"]
    uuid = ctx["uuid"]

    if parsed.path == "/api/session/new":
        try:
            workspace = (
                str(resolve_trusted_workspace(body.get("workspace")))
                if body.get("workspace")
                else None
            )
        except (TypeError, ValueError) as e:
            return bad(handler, str(e))
        worktree_info = None
        worktree_skipped = None
        # Three-value worktree model (#6022): an explicit body value always
        # wins; an ABSENT key falls back to the agent's config-level
        # ``worktree:`` default so WebUI sessions and CLI sessions agree on
        # isolation for the same repo.  Clients that must never create a
        # worktree (e.g. the boot-time auto-bind) send ``worktree: false``
        # explicitly.
        raw_worktree = body.get("worktree")
        # Presence-based, not truthiness-based: a client that sends the key at
        # all (even ``worktree: null``) has spoken explicitly and never falls
        # through to the config default.  ``null`` parses as non-true below,
        # i.e. an explicit opt-out — only a genuinely ABSENT key inherits.
        worktree_explicit = "worktree" in body
        if worktree_explicit:
            worktree_requested = raw_worktree is True or str(
                raw_worktree
            ).strip().lower() in {"1", "true", "yes", "on"}
        else:
            worktree_requested = _worktree_default_from_config(
                body.get("profile") or None
            )
        if worktree_requested:
            try:
                from api.worktrees import create_worktree_for_workspace

                base_workspace = workspace
                if not base_workspace:
                    base_workspace = str(
                        resolve_trusted_workspace(get_last_workspace())
                    )
                worktree_info = create_worktree_for_workspace(base_workspace)
                workspace = worktree_info["path"]
            except (TypeError, ValueError) as e:
                # Explicit requests keep the hard failure.  A config-default
                # request on a non-git workspace (create_worktree_for_workspace
                # raises ValueError) degrades to a plain session instead —
                # otherwise `worktree: true` in config.yaml would 400 every
                # session in every non-git directory.
                if worktree_explicit:
                    return bad(handler, str(e), status=400)
                worktree_info = None
                worktree_skipped = str(e)
            except Exception as e:
                logger.exception("failed to create worktree-backed session")
                return bad(handler, f"Failed to create worktree: {e}", status=500)
        model, model_provider = _session_model_state_from_request(
            body.get("model"),
            body.get("model_provider"),
        )
        try:
            enabled_toolsets = _validate_session_toolsets_shape(
                body.get("enabled_toolsets")
            )
        except ValueError as e:
            return bad(handler, str(e), status=400)
        # Use the profile sent by the client tab (if any) so that two tabs on
        # different profiles never clobber each other via the process-level global.
        # ── Memory lifecycle: commit the previous session before starting a new one ──
        prev_session_id = body.get("prev_session_id")
        if prev_session_id:
            if not _session_id_visible_to_request_profile(
                handler, prev_session_id, emit_error=False
            ):
                # Cross-profile hand-off after a profile switch: skip memory
                # commit for the previous profile's session, but still create
                # the new session (#5420).
                prev_session_id = None
            if prev_session_id:
                # Fire-and-forget: commit_memory_session() can take 1-5+ seconds
                # (extraction call to the memory provider), and blocking the
                # response here made "+ New Chat" feel slow/unresponsive.
                # commit_session_memory() already serialises overlapping commits
                # for a session via its own in-flight guard, so running it off
                # the request thread is safe.
                def _commit_prev_session_memory(_sid=prev_session_id):
                    try:
                        from api.agent_cache import locked_agent_cache
                        from api.sessions import commit_session_memory

                        prev_agent = None
                        with locked_agent_cache() as session_agent_cache:
                            _cached = session_agent_cache.get(_sid)
                            if _cached:
                                prev_agent = _cached[0]
                        commit_session_memory(_sid, agent=prev_agent)
                    except Exception:
                        logger.warning(
                            "Lifecycle commit for prev_session %s failed",
                            _sid,
                            exc_info=True,
                        )
                    finally:
                        # Self-unregister so the background-commit registry does
                        # not leak completed threads; drain only tracks live ones.
                        try:
                            from api.sessions import (
                                unregister_background_commit_thread,
                            )

                            unregister_background_commit_thread(
                                threading.current_thread()
                            )
                        except Exception:
                            pass

                t = threading.Thread(
                    target=_commit_prev_session_memory,
                    daemon=True,
                    name=f"commit-memory-{prev_session_id}",
                )
                from api.sessions import register_background_commit_thread

                # Refused only if shutdown draining has already begun; in that
                # window the inline drain commits the pending generation instead,
                # so skipping the worker start is safe (avoids a late daemon
                # thread the drain snapshot already missed).
                if register_background_commit_thread(t):
                    t.start()
        s = new_session(
            workspace=workspace,
            model=model,
            model_provider=model_provider,
            profile=body.get("profile") or None,
            project_id=body.get("project_id") or None,
            worktree_info=worktree_info,
            enabled_toolsets=enabled_toolsets,
        )
        if worktree_info:
            publish_session_list_changed(
                "session_new",
                profile=getattr(s, "profile", None),
                session_id=getattr(s, "session_id", None),
            )
        payload = {"session": s.compact() | {"messages": s.messages}}
        if worktree_skipped:
            # Config-default worktree was skipped (non-git workspace); tell the
            # client the session is plain so the UI doesn't assume isolation.
            payload["worktree_skipped"] = worktree_skipped
        return j(handler, payload)

    if parsed.path == "/api/session/compression-recovery/start":
        return _handle_session_compression_recovery_start(handler, body)

    if parsed.path == "/api/session/duplicate":
        try:
            sid = body.get("session_id")
            if not sid:
                return bad(handler, "session_id is required")
            if foreign_session_access.is_view_only(sid):
                return bad(
                    handler,
                    "Subagent sessions are view-only and cannot be duplicated from WebUI",
                    400,
                )

            session = Session.load(sid)
            if not session:
                # 404, not 400 — missing resource, not a malformed request.
                return bad(handler, "Session not found", status=404)

            # Deep-copy mutable lists so the duplicate is *actually* independent.
            # `Session.__init__` does `self.messages = messages or []` — plain
            # assignment, no copy. Without deepcopy, both sessions share the same
            # list object in memory; appending to one mutates the other.
            # Items inside `messages` are dicts with mutable values (tool_calls,
            # content arrays), so a shallow `list(...)` is not enough.
            copied_session = Session(
                session_id=uuid.uuid4().hex[:12],
                # Defensive: legacy sessions may have title=None on disk; fall back to 'Untitled'
                # so `+ " (copy)"` doesn't TypeError.
                title=(session.title or "Untitled") + " (copy)",
                workspace=session.workspace,
                model=session.model,
                model_provider=session.model_provider,
                messages=copy.deepcopy(session.messages),
                tool_calls=copy.deepcopy(session.tool_calls),
                # Reset ephemeral / per-session-instance flags. Duplicating an
                # archived conversation should produce a visible (un-archived)
                # copy; pinned status doesn't transfer either.
                pinned=False,
                archived=False,
                project_id=session.project_id,
                profile=session.profile,
                input_tokens=session.input_tokens,
                output_tokens=session.output_tokens,
                estimated_cost=session.estimated_cost,
                cache_read_tokens=getattr(session, "cache_read_tokens", 0),
                cache_write_tokens=getattr(session, "cache_write_tokens", 0),
                # Per-session settings the user may have customized — carry them over
                # so the duplicate behaves identically until further edits. Compression
                # anchor + last_prompt_tokens are intentionally NOT carried — those
                # re-derive on the next turn.
                personality=session.personality,
                enabled_toolsets=getattr(session, "enabled_toolsets", None),
                context_length=getattr(session, "context_length", None),
                threshold_tokens=getattr(session, "threshold_tokens", None),
                truncation_watermark=getattr(session, "truncation_watermark", None),
                truncation_boundary=getattr(session, "truncation_boundary", None),
                # context_messages is the authoritative model-facing prefix — must be
                # deepcopied so the duplicate has its own independent context that won't
                # be mutated when the original session's context changes (#2914).
                context_messages=copy.deepcopy(
                    getattr(session, "context_messages", None) or []
                ),
                # Gateway routing — if the user customized routing for this session,
                # the duplicate should behave identically.
                gateway_routing=copy.deepcopy(
                    getattr(session, "gateway_routing", None)
                ),
                gateway_routing_history=copy.deepcopy(
                    getattr(session, "gateway_routing_history", None) or []
                ),
                # Preserve LLM-generated title flag so we don't regenerate title on duplicate.
                llm_title_generated=getattr(session, "llm_title_generated", False),
                manual_title=getattr(session, "manual_title", False),
                # Composer draft — preserve per-session draft state.
                composer_draft=copy.deepcopy(
                    getattr(session, "composer_draft", None) or {}
                ),
                # Context engine state — preserve so the duplicate's context engine
                # starts from the same point as the original.
                context_engine=getattr(session, "context_engine", None),
                context_engine_state=copy.deepcopy(
                    getattr(session, "context_engine_state", None) or {}
                ),
                created_at=time.time(),
                updated_at=time.time(),
            )

            # Persist immediately. The pre-PR flow (/api/session/new + /api/session/rename)
            # accidentally avoided this because `/api/session/rename` calls `s.save()`.
            # Without this explicit save, the duplicate is in-memory only — if the user
            # refreshes before sending a turn, the duplicate vanishes.
            foreign_session_access.publish(copied_session, persist=True)
            publish_session_list_changed(
                "session_duplicate",
                profile=getattr(copied_session, "profile", None),
                session_id=getattr(copied_session, "session_id", None),
            )

            return j(
                handler,
                {
                    "session": copied_session.compact()
                    | {"messages": copied_session.messages}
                },
            )
        except Exception as e:
            return bad(handler, str(e))
    return UNHANDLED
