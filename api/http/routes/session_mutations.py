"""Cohesive HTTP route group used by the transport composition root."""

from __future__ import annotations

from api.http.context import RouteContext, UNHANDLED


def handle_post(handler, parsed, body, diag, ctx: RouteContext):
    Session = ctx["Session"]
    SessionActiveError = ctx["SessionActiveError"]
    _get_or_materialize_session = ctx["_get_or_materialize_session"]
    _handle_background = ctx["_handle_background"]
    _handle_bg_task_complete_ack = ctx["_handle_bg_task_complete_ack"]
    _handle_btw = ctx["_handle_btw"]
    _handle_chat_start = ctx["_handle_chat_start"]
    _handle_chat_sync = ctx["_handle_chat_sync"]
    _handle_conversation_rounds = ctx["_handle_conversation_rounds"]
    _handle_goal_command = ctx["_handle_goal_command"]
    _handle_handoff_summary = ctx["_handle_handoff_summary"]
    _handle_session_anchor_scene = ctx["_handle_session_anchor_scene"]
    _handle_session_compress = ctx["_handle_session_compress"]
    _handle_session_compress_start = ctx["_handle_session_compress_start"]
    _handle_terminal_close = ctx["_handle_terminal_close"]
    _handle_terminal_input = ctx["_handle_terminal_input"]
    _handle_terminal_resize = ctx["_handle_terminal_resize"]
    _handle_terminal_start = ctx["_handle_terminal_start"]
    _is_messaging_session_id = ctx["_is_messaging_session_id"]
    _is_messaging_session_record = ctx["_is_messaging_session_record"]
    _load_branch_source_or_refuse = ctx["_load_branch_source_or_refuse"]
    _lookup_cli_session_metadata = ctx["_lookup_cli_session_metadata"]
    _merged_session_messages_for_display = ctx["_merged_session_messages_for_display"]
    _persist_generated_session_title = ctx["_persist_generated_session_title"]
    _publish_materialized_session = ctx["_publish_materialized_session"]
    _publish_session_list_changed = ctx["_publish_session_list_changed"]
    _resolve_context_length_for_session_model = ctx[
        "_resolve_context_length_for_session_model"
    ]
    _sanitize_error = ctx["_sanitize_error"]
    _session_is_subagent_view_only = ctx["_session_is_subagent_view_only"]
    _session_visible_to_active_profile = ctx["_session_visible_to_active_profile"]
    _session_model_state_from_request = ctx["_session_model_state_from_request"]
    _session_requires_cli_metadata_lookup = ctx["_session_requires_cli_metadata_lookup"]
    _sync_session_title_to_insights = ctx["_sync_session_title_to_insights"]
    _validate_session_toolsets_shape = ctx["_validate_session_toolsets_shape"]
    _worktree_retained_payload_for_session_id = ctx[
        "_worktree_retained_payload_for_session_id"
    ]
    bad = ctx["bad"]
    copy = ctx["copy"]
    delete_session_state = ctx["delete_session_state"]
    disable_session_yolo = ctx["disable_session_yolo"]
    edit_session = ctx["edit_session"]
    enable_session_yolo = ctx["enable_session_yolo"]
    generate_session_title_for_session = ctx["generate_session_title_for_session"]
    get_cli_session_messages = ctx["get_cli_session_messages"]
    get_session = ctx["get_session"]
    is_safe_session_id = ctx["is_safe_session_id"]
    j = ctx["j"]
    json = ctx["json"]
    logger = ctx["logger"]
    publish_session_list_changed = ctx["publish_session_list_changed"]
    require = ctx["require"]
    resolve_gateway_approval = ctx["resolve_gateway_approval"]
    resolve_trusted_workspace = ctx["resolve_trusted_workspace"]
    set_last_workspace = ctx["set_last_workspace"]
    uuid = ctx["uuid"]

    if parsed.path == "/api/session/anchor-scene":
        return _handle_session_anchor_scene(
            handler,
            body,
            get_or_materialize_session=_get_or_materialize_session,
            session_visible_to_active_profile=_session_visible_to_active_profile,
            require_fields=require,
            bad_response=bad,
            json_response=j,
        )

    if parsed.path == "/api/session/rename":
        try:
            require(body, "session_id", "title")
        except ValueError as e:
            return bad(handler, str(e))
        try:
            s = _get_or_materialize_session(body["session_id"])
        except KeyError:
            return bad(handler, "Session not found", 404)
        except PermissionError:
            return bad(
                handler, "Read-only imported sessions cannot be renamed from WebUI", 403
            )
        with edit_session(body["session_id"], session=s) as s:
            from api.sessions import apply_session_title_rename

            apply_session_title_rename(s, body["title"])
        _sync_session_title_to_insights(s)
        publish_session_list_changed(
            "session_rename",
            profile=getattr(s, "profile", None),
            session_id=getattr(s, "session_id", body["session_id"]),
        )
        return j(handler, {"session": s.compact()})

    if parsed.path == "/api/session/title/regenerate":
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        sid = body["session_id"]
        prefer_latest = bool(body.get("prefer_latest", False))
        try:
            s = _get_or_materialize_session(sid)
        except KeyError:
            return bad(handler, "Session not found", 404)
        except PermissionError:
            return bad(
                handler, "Read-only imported sessions cannot regenerate titles", 403
            )
        next_title, reason, raw_preview = generate_session_title_for_session(
            s, prefer_latest=prefer_latest
        )
        if not next_title:
            return bad(
                handler, f"Could not generate a better title ({reason or 'empty'})", 422
            )
        _persist_generated_session_title(
            s, next_title, event_reason="session_title_regenerate"
        )
        return j(
            handler,
            {
                "session": s.compact(),
                "title": s.title,
                "status": reason,
                "raw_preview": (raw_preview or "")[:240],
            },
        )

    if parsed.path == "/api/personality/set":
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        if "name" not in body:
            return bad(handler, "Missing required field: name")
        sid = body["session_id"]
        if _session_is_subagent_view_only(sid):
            return bad(
                handler,
                "Subagent sessions are view-only and cannot be modified from WebUI",
                400,
            )
        name = body["name"].strip()
        try:
            s = get_session(sid)
        except KeyError:
            return bad(handler, "Session not found", 404)
        # Resolve personality from config.yaml agent.personalities section
        # (matches hermes-agent CLI behavior)
        prompt = ""
        if name:
            from api.config import reload_config as _reload_cfg2

            _reload_cfg2()  # pick up config changes without restart
            from api.config import get_config as _get_cfg2

            _cfg2 = _get_cfg2()
            agent_cfg = _cfg2.get("agent", {})
            raw_personalities = agent_cfg.get("personalities", {})
            if not isinstance(raw_personalities, dict) or name not in raw_personalities:
                return bad(
                    handler, f'Personality "{name}" not found in config.yaml', 404
                )
            value = raw_personalities[name]
            # Resolve prompt using the same logic as hermes-agent cli.py
            if isinstance(value, dict):
                parts = [value.get("system_prompt", "") or value.get("prompt", "")]
                if value.get("tone"):
                    parts.append(f"Tone: {value['tone']}")
                if value.get("style"):
                    parts.append(f"Style: {value['style']}")
                prompt = "\n".join(p for p in parts if p)
            else:
                prompt = str(value)
        with edit_session(sid, session=s) as s:
            s.personality = name if name else None
        return j(handler, {"ok": True, "personality": s.personality, "prompt": prompt})

    if parsed.path == "/api/session/toolsets":
        """Set or clear per-session toolset override (#493).

        POST body: { session_id, toolsets: [...] | null }
        - toolsets: list of toolset names to restrict the session to, or null to clear.
        """
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        sid = body["session_id"]
        if _session_is_subagent_view_only(sid):
            return bad(
                handler,
                "Subagent sessions are view-only and cannot be modified from WebUI",
                400,
            )
        toolsets = body.get("toolsets")
        try:
            toolsets = _validate_session_toolsets_shape(toolsets)
        except ValueError as e:
            return bad(handler, str(e), status=400)
        try:
            s = get_session(sid)
        except KeyError:
            return bad(handler, "Session not found", 404)
        with edit_session(sid, session=s) as s:
            s.enabled_toolsets = toolsets
        return j(handler, {"ok": True, "enabled_toolsets": s.enabled_toolsets})

    if parsed.path == "/api/session/draft":
        # POST body → save draft { session_id, text?, files? }. Current draft
        # state is returned as part of GET /api/session.
        import time as _draft_time

        _draft_t0 = _draft_time.monotonic()
        _draft_stages = []

        def _draft_mark(name):
            _draft_stages.append((name, _draft_time.monotonic()))

        _draft_mark("enter")
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        sid = body["session_id"]
        if _session_is_subagent_view_only(sid):
            return bad(
                handler,
                "Subagent sessions are view-only and cannot store a draft from WebUI",
                400,
            )
        text = body.get("text")
        files = body.get("files")
        # Stage-326 hardening (per Opus advisor): size + type validation on
        # the draft inputs. Without this, a misbehaving or malicious client
        # can persist multi-MB strings into the session JSON on every keystroke
        # via the 400ms debounced auto-save.
        _MAX_DRAFT_TEXT = 50_000  # 50 KB cap on textarea content
        _MAX_DRAFT_FILES = 50  # max number of attached file references
        if text is not None and not isinstance(text, str):
            text = ""
        if isinstance(text, str) and len(text) > _MAX_DRAFT_TEXT:
            text = text[:_MAX_DRAFT_TEXT]
        if files is not None and not isinstance(files, list):
            files = []
        if isinstance(files, list) and len(files) > _MAX_DRAFT_FILES:
            files = files[:_MAX_DRAFT_FILES]
        try:
            s = get_session(sid)
        except KeyError:
            return bad(handler, "Session not found", 404)
        _draft_mark("after_get_session")
        unchanged = False
        with edit_session(
            sid,
            session=s,
            touch_updated_at=False,
            skip_index=True,
            save_when=lambda _session: not unchanged,
        ) as s:
            _draft_mark("acquired_lock")
            current_draft = dict(getattr(s, "composer_draft", {}) or {})
            next_draft = dict(current_draft)
            if text is not None:
                next_draft["text"] = text
            if files is not None:
                next_draft["files"] = files
            if next_draft == current_draft:
                unchanged = True
                saved_draft = current_draft
            else:
                s.composer_draft = next_draft
                # Draft persistence is not conversation activity. Touching updated_at
                # here makes the active-session external-refresh poll force-reload the
                # current chat every few seconds while the user is typing, and that
                # delayed reload can restore an older draft over newer local input.
                _draft_mark("before_save")
                saved_draft = s.composer_draft
        _draft_mark("released_lock")
        payload = {"ok": True, "draft": saved_draft}
        if unchanged:
            payload["unchanged"] = True
        _draft_mark("before_json")
        j(handler, payload)
        _draft_mark("after_json")
        _draft_stages.append(("end", _draft_time.monotonic()))
        if _draft_stages[-1][1] - _draft_t0 > 0.2:
            parts = " ".join(
                f"{n}={((t - prev[1]) * 1000):.1f}ms"
                for (n, t), prev in zip(
                    _draft_stages[1:], _draft_stages[:-1], strict=True
                )
            )
            handler._safe_webui_print(
                "[SLOW] /api/session/draft total=%.1fms stages: %s"
                % (
                    (_draft_stages[-1][1] - _draft_t0) * 1000,
                    parts,
                )
            )
        return True

    if parsed.path == "/api/session/update":
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        try:
            s = _get_or_materialize_session(body["session_id"])
        except KeyError:
            return bad(handler, "Session not found", 404)
        except PermissionError:
            return bad(
                handler, "Read-only imported sessions cannot be updated from WebUI", 403
            )
        old_ws = getattr(s, "workspace", "")
        old_model = getattr(s, "model", None)
        old_provider = getattr(s, "model_provider", None)
        try:
            new_ws = str(resolve_trusted_workspace(body.get("workspace", s.workspace)))
        except ValueError as e:
            return bad(handler, str(e))
        with edit_session(body["session_id"], session=s) as s:
            s.workspace = new_ws
            if "model" in body or "model_provider" in body:
                model, provider = _session_model_state_from_request(
                    body.get("model", s.model),
                    body.get("model_provider") if "model_provider" in body else None,
                    getattr(s, "model_provider", None),
                )
                if model is not None:
                    s.model = model
                s.model_provider = provider
                if str(old_model or "") != str(getattr(s, "model", "") or "") or str(
                    old_provider or ""
                ) != str(getattr(s, "model_provider", "") or ""):
                    s.context_length = _resolve_context_length_for_session_model(
                        getattr(s, "model", None),
                        getattr(s, "model_provider", None),
                    )
                    s.threshold_tokens = 0
                    s.last_prompt_tokens = 0
                    from api.config import _evict_session_agent

                    _evict_session_agent(body["session_id"])
        if str(old_ws or "") != str(new_ws or ""):
            try:
                from api.terminal import close_terminal

                close_terminal(body["session_id"])
            except Exception:
                logger.debug(
                    "Failed to close workspace terminal after workspace update"
                )
        set_last_workspace(new_ws)
        return j(handler, {"session": s.compact() | {"messages": s.messages}})
    if parsed.path == "/api/session/worktree/remove":
        sid = body.get("session_id", "")
        if not sid or not isinstance(sid, str) or not sid.strip():
            return bad(handler, "session_id must be a non-empty string", status=400)
        sid = sid.strip()
        if not is_safe_session_id(sid):
            return bad(handler, "Invalid session_id", 400)
        try:
            s = get_session(sid, metadata_only=True)
        except KeyError:
            return bad(handler, "Session not found", status=404)
        force = bool(body.get("force", False))
        try:
            from api.worktrees import remove_worktree_for_session

            result = remove_worktree_for_session(s, force=force)
            return j(handler, result)
        except ValueError as exc:
            return bad(handler, str(exc), status=400)
        except Exception as exc:
            logger.exception("failed to remove worktree for session %s", sid)
            return bad(handler, _sanitize_error(exc), status=500)

    if parsed.path == "/api/session/delete":
        sid = body.get("session_id", "")
        if not sid:
            return bad(handler, "session_id is required")
        if not is_safe_session_id(sid):
            return bad(handler, "Invalid session_id", 400)
        cli_meta_for_delete = _lookup_cli_session_metadata(sid)
        if cli_meta_for_delete.get("read_only"):
            return bad(
                handler, "Read-only imported sessions cannot be deleted from WebUI", 400
            )
        # A delegated subagent child (#5307) is view-only and owned by the
        # delegate runner. Deleting it here would call delete_cli_session() and
        # erase the child's state.db transcript — refuse it.
        if _session_is_subagent_view_only(sid):
            return bad(
                handler,
                "Subagent sessions are view-only and cannot be deleted from WebUI",
                400,
            )
        is_messaging_session = _is_messaging_session_id(sid)
        worktree_retained = _worktree_retained_payload_for_session_id(sid)
        try:
            event_profile = getattr(
                get_session(sid, metadata_only=True), "profile", None
            )
        except KeyError:
            event_profile = None
        except Exception:
            logger.debug(
                "Failed to resolve profile for deleted session %s", sid, exc_info=True
            )
            event_profile = None
        try:
            deletion = delete_session_state(sid, messaging=is_messaging_session)
        except SessionActiveError:
            return bad(
                handler,
                "Stop the active response before deleting this session",
                409,
            )
        except ValueError:
            return bad(handler, "Invalid session_id", 400)
        except Exception:
            logger.exception("Failed to delete session state for %s", sid)
            return bad(handler, "Failed to delete session", 500)
        _publish_session_list_changed("session_delete", profile=event_profile)
        return j(
            handler,
            {
                "ok": True,
                "state_db_cleanup_failed": deletion.state_db_cleanup_failed,
                **worktree_retained,
            },
        )

    if parsed.path == "/api/session/clear":
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        if _session_is_subagent_view_only(body["session_id"]):
            return bad(
                handler,
                "Subagent sessions are view-only and cannot be modified from WebUI",
                400,
            )
        try:
            s = get_session(body["session_id"])
        except KeyError:
            return bad(handler, "Session not found", 404)
        sid = body["session_id"]
        with edit_session(sid, session=s) as s:
            had_sidecar_messages = bool(s.messages or [])
            # Clear is a full truncate-to-empty: route through the SAME helper the
            # /api/session/truncate handler uses (single source of truth) so the
            # display + context arrays are emptied AND the truncation watermark is
            # set via _truncation_watermark_for([]) == 0.0 — the #2914
            # truncate-to-empty sentinel that blocks state.db replay. Before this,
            # /clear wiped s.messages but left the watermark unset, so the
            # append-only state.db merge treated it as "keep everything" and the
            # cleared history resurrected on the next /api/session read (#5532).
            from api.sessions import truncate_session_at_keep

            truncate_session_at_keep(s, 0)
            s.tool_calls = []
            # A compressed-continuation child keeps its archived transcript in a
            # parent sidecar marked pre_compression_snapshot;
            # _webui_sidecar_lineage_messages_for_display() stitches that parent
            # back with truncation_watermark=None, so the 0.0 sentinel on the
            # CHILD does NOT stop the parent from resurrecting the cleared history
            # on refresh. Detach the compression lineage (#5532/#5553) — but ONLY
            # when the parent is actually a pre_compression_snapshot; a genuine
            # fork parent (session_source="fork" from /api/session/branch) must
            # keep its link so the child still nests + shows "Forked from"
            # (sessions.js:5720/5964/7105). Dropping every parent broke that
            # (#5532 Codex gate).
            _parent_sid = getattr(s, "parent_session_id", None)
            if _parent_sid:
                _parent_is_compression_snapshot = False
                try:
                    _parent = get_session(_parent_sid, metadata_only=True)
                    _parent_is_compression_snapshot = bool(
                        getattr(_parent, "pre_compression_snapshot", False)
                    )
                except Exception:
                    _parent_is_compression_snapshot = False
                if _parent_is_compression_snapshot:
                    s.parent_session_id = None
                    s.compression_anchor_visible_idx = None
                    s.compression_anchor_message_key = None
            s.active_stream_id = None
            s.pending_user_message = None
            s.pending_attachments = []
            s.pending_started_at = None
            s.pending_user_source = None
            s.clear_generation = uuid.uuid4().hex if had_sidecar_messages else None
            # Reset the title via the rename helper so clearing a manually-named
            # session also clears manual_title/llm_title_generated — otherwise the
            # reused session keeps its manual-title protection and never auto-names
            # again (#3542 lifecycle gap).
            from api.sessions import apply_session_title_rename

            apply_session_title_rename(s, "Untitled")
        persisted_clear = False
        try:
            persisted = json.loads(s.path.read_text(encoding="utf-8"))
            persisted_clear = (
                persisted.get("messages") == []
                and persisted.get("context_messages") == []
                and persisted.get("truncation_watermark") == 0.0
                and persisted.get("truncation_boundary") == 0.0
                and persisted.get("active_stream_id") is None
                and persisted.get("pending_user_message") is None
                and persisted.get("pending_attachments") == []
                and persisted.get("pending_started_at") is None
                and persisted.get("pending_user_source") is None
                and persisted.get("clear_generation") == s.clear_generation
            )
        except (OSError, json.JSONDecodeError, ValueError):
            logger.warning(
                "session clear could not verify persisted empty state for %s",
                sid,
                exc_info=True,
            )
        if had_sidecar_messages and persisted_clear:
            try:
                s.path.with_suffix(".json.bak").unlink(missing_ok=True)
            except OSError:
                logger.warning(
                    "session clear could not remove stale backup for %s",
                    sid,
                    exc_info=True,
                )
        # Evict cached agent outside the per-session lock.  Eviction may run a
        # boundary memory commit for batch-extraction providers, and provider
        # I/O must not hold the session mutation lock.
        from api.config import _evict_session_agent

        _evict_session_agent(sid)
        return j(handler, {"ok": True, "session": s.compact()})

    if parsed.path == "/api/session/truncate":
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        if _session_is_subagent_view_only(body["session_id"]):
            return bad(
                handler,
                "Subagent sessions are view-only and cannot be modified from WebUI",
                400,
            )
        if body.get("keep_count") is None:
            return bad(handler, "Missing required field(s): keep_count")
        try:
            s = get_session(body["session_id"])
        except KeyError:
            return bad(handler, "Session not found", 404)
        # Validate keep_count before it reaches the destructive `messages[:keep]`
        # slice. A non-numeric value would raise ValueError and surface as a
        # confusing 500; a NEGATIVE value slices as `messages[:-N]`, which
        # silently DELETES the most recent N messages (e.g. keep_count=-5 on a
        # 3-message session wipes the whole transcript) and then persists it via
        # save(). Mirror the explicit guard the /api/session/branch handler
        # already applies to its own keep_count. (Opus pre-release follow-up.)
        try:
            keep = int(body["keep_count"])
        except (ValueError, TypeError):
            return bad(handler, "keep_count must be an integer")
        if keep < 0:
            return bad(handler, "keep_count must be non-negative")
        with edit_session(body["session_id"], session=s) as s:
            from api.sessions import truncate_session_at_keep

            old_msg_count, old_ctx_count = truncate_session_at_keep(s, keep)
            logger.info(
                "truncate %s: messages %d→%d, context_messages %d→%d, watermark=%.2f",
                body["session_id"],
                old_msg_count,
                len(s.messages or []),
                old_ctx_count,
                len(getattr(s, "context_messages", None) or []),
                s.truncation_watermark or 0,
            )
        from api.config import _evict_session_agent

        _evict_session_agent(body["session_id"])
        return j(
            handler, {"ok": True, "session": s.compact() | {"messages": s.messages}}
        )

    if parsed.path == "/api/session/branch":
        # Fork a conversation from any message point (#465).
        # Accepts: {session_id, keep_count?, title?}
        #   keep_count: number of messages to copy (0=empty, undefined=full history)
        #   title: custom title (defaults to "<original title> (fork)")
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        # Reject non-string session_id explicitly so the failure surfaces as a
        # 400 instead of a generic 500 from get_session() raising TypeError.
        # (Opus pre-release follow-up.)
        if not isinstance(body["session_id"], str):
            return bad(handler, "session_id must be a string")
        source = _load_branch_source_or_refuse(handler, body["session_id"])
        if source is None:
            return True

        keep_count = body.get("keep_count")
        if keep_count is not None:
            try:
                keep_count = int(keep_count)
            except (ValueError, TypeError):
                return bad(handler, "keep_count must be an integer")
            # Negative slice (`messages[:-N]`) returns "all but last N", which
            # is a confusing fork semantic. Reject explicitly so the user
            # doesn't accidentally fork a session with the tail truncated when
            # they meant to copy the prefix. (Opus pre-release follow-up.)
            if keep_count < 0:
                return bad(handler, "keep_count must be non-negative")

        custom_title = body.get("title")
        if custom_title:
            custom_title = str(custom_title).strip()[:80] or None

        # Build messages slice in the same coordinate space exposed by GET
        # /api/session so frontend keep_count values from merged messaging
        # transcripts do not silently become full sidecar copies.
        try:
            if not getattr(source, "_branch_source_readonly", False):
                source.save()
        except Exception:
            pass
        cli_meta = (
            _lookup_cli_session_metadata(source.session_id)
            if _session_requires_cli_metadata_lookup(source)
            else {}
        )
        is_messaging_session = _is_messaging_session_record(
            source
        ) or _is_messaging_session_record(cli_meta)
        cli_messages = (
            get_cli_session_messages(source.session_id) if is_messaging_session else []
        )
        source_messages = (
            _merged_session_messages_for_display(source, cli_messages)
            if is_messaging_session and cli_messages
            else list(source.messages or [])
        )
        if keep_count is not None:
            forked_messages = source_messages[:keep_count]
        else:
            forked_messages = list(source_messages)

        # Derive title
        if custom_title:
            branch_title = custom_title
        else:
            source_title = source.title or "Untitled"
            branch_title = f"{source_title} (fork)"

        # Create new session inheriting workspace/model/profile
        from api.sessions import truncate_context_for_display_keep

        fork_keep = keep_count if keep_count is not None else len(source_messages)
        forked_context = truncate_context_for_display_keep(
            getattr(source, "context_messages", None),
            source_messages,
            fork_keep,
        )
        branch = Session(
            workspace=source.workspace,
            model=source.model,
            model_provider=getattr(source, "model_provider", None),
            profile=getattr(source, "profile", None),
            title=branch_title,
            messages=forked_messages,
            project_id=getattr(source, "project_id", None),
            personality=getattr(source, "personality", None),
            enabled_toolsets=getattr(source, "enabled_toolsets", None),
            context_length=getattr(source, "context_length", None),
            threshold_tokens=getattr(source, "threshold_tokens", None),
            # context_messages — truncated to fork prefix (not full parent copy)
            context_messages=copy.deepcopy(forked_context),
            # Gateway routing — inherit from source
            gateway_routing=copy.deepcopy(getattr(source, "gateway_routing", None)),
            # Context engine — inherit state so branch's context engine starts correctly
            context_engine=getattr(source, "context_engine", None),
            context_engine_state=copy.deepcopy(
                getattr(source, "context_engine_state", None) or {}
            ),
            parent_session_id=source.session_id,
            session_source="fork",
        )
        # Empty branches intentionally match new_session's memory-only contract;
        # non-empty branches must persist before becoming cache-visible.
        _publish_materialized_session(branch, persist=bool(forked_messages))
        if forked_messages:
            publish_session_list_changed(
                "session_branch",
                profile=getattr(branch, "profile", None),
                session_id=getattr(branch, "session_id", None),
            )

        return j(
            handler,
            {
                "session_id": branch.session_id,
                "title": branch_title,
                "parent_session_id": source.session_id,
            },
        )

    if parsed.path == "/api/session/compress/start":
        return _handle_session_compress_start(handler, body)

    if parsed.path == "/api/session/compress":
        return _handle_session_compress(handler, body)

    if parsed.path == "/api/session/conversation-rounds":
        return _handle_conversation_rounds(handler, body)

    if parsed.path == "/api/session/handoff-summary":
        return _handle_handoff_summary(handler, body)

    if parsed.path == "/api/session/retry":
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        if _session_is_subagent_view_only(body["session_id"]):
            return bad(
                handler,
                "Subagent sessions are view-only and cannot be modified from WebUI",
                400,
            )
        try:
            from api.sessions import retry_last

            result = retry_last(body["session_id"])
            return j(handler, {"ok": True, **result})
        except KeyError:
            return bad(handler, "Session not found", 404)
        except ValueError as e:
            return j(handler, {"error": str(e)})

    if parsed.path == "/api/session/undo":
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        if _session_is_subagent_view_only(body["session_id"]):
            return bad(
                handler,
                "Subagent sessions are view-only and cannot be modified from WebUI",
                400,
            )
        try:
            from api.sessions import undo_last

            result = undo_last(body["session_id"])
            return j(handler, {"ok": True, **result})
        except KeyError:
            return bad(handler, "Session not found", 404)
        except ValueError as e:
            return j(handler, {"error": str(e)})

    # ── YOLO mode toggle (POST) ──
    # Session-scoped only — stored in-memory on the server side.
    # Important lifecycle notes:
    #   • Page reload: state PERSISTS (frontend re-fetches via GET endpoint)
    #   • Cross-tab: state is SHARED (same server-side flag per session)
    #   • Server restart: state is LOST (in-memory only)
    #   • Cross-session: isolated (each session has its own flag)
    # Fixes #467
    if parsed.path == "/api/session/yolo":
        try:
            require(body, "session_id")
        except ValueError as e:
            return bad(handler, str(e))
        sid = body["session_id"]
        enabled = bool(body.get("enabled", True))
        if enabled:
            enable_session_yolo(sid)
            # Also resolve any pending approvals for this session so the
            # agent doesn't stay stuck waiting on an already-dismissed card.
            try:
                from tools.approval import _pending as _p, _lock as _l

                with _l:
                    _p.pop(sid, None)
            except Exception:
                pass
            resolve_gateway_approval(sid, "once", resolve_all=True)
        else:
            disable_session_yolo(sid)
        return j(handler, {"ok": True, "yolo_enabled": enabled})

    if parsed.path == "/api/btw":
        return _handle_btw(handler, body)

    if parsed.path == "/api/background":
        return _handle_background(handler, body)

    if parsed.path == "/api/goal":
        return _handle_goal_command(handler, body)

    if parsed.path == "/api/bg-task-complete-ack":
        return _handle_bg_task_complete_ack(handler, body)

    if parsed.path == "/api/chat/start":
        return _handle_chat_start(handler, body, diag=diag)

    if parsed.path == "/api/chat":
        return _handle_chat_sync(handler, body)

    if parsed.path == "/api/chat/steer":
        from api.streaming import _handle_chat_steer

        return _handle_chat_steer(handler, body)

    if parsed.path == "/api/terminal/start":
        return _handle_terminal_start(handler, body)

    if parsed.path == "/api/terminal/input":
        return _handle_terminal_input(handler, body)

    if parsed.path == "/api/terminal/resize":
        return _handle_terminal_resize(handler, body)

    if parsed.path == "/api/terminal/close":
        return _handle_terminal_close(handler, body)
    return UNHANDLED
