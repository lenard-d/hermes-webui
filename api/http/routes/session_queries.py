"""Cohesive HTTP route group used by the transport composition root."""

from __future__ import annotations

from api.http.context import RouteContext, UNHANDLED


def handle_get(handler, parsed, ctx: RouteContext):
    RequestDiagnostics = ctx["RequestDiagnostics"]
    SESSION_DIR = ctx["SESSION_DIR"]
    _MAX_MSG_LIMIT = ctx["_MAX_MSG_LIMIT"]
    _active_state_db_path = ctx["_active_state_db_path"]
    _active_stream_ids = ctx["_active_stream_ids"]
    _all_profiles_enabled = ctx["_all_profiles_enabled"]
    _build_session_list_cache_payload = ctx["_build_session_list_cache_payload"]
    _claim_or_synthesize_cli_session = ctx["_claim_or_synthesize_cli_session"]
    _clear_stale_stream_state = ctx["_clear_stale_stream_state"]
    _get_cached_session_list_payload = ctx["_get_cached_session_list_payload"]
    _handle_session_compress_status = ctx["_handle_session_compress_status"]
    _hydrate_anchor_activity_scenes = ctx["_hydrate_anchor_activity_scenes"]
    _is_isolated_profile_mode = ctx["_is_isolated_profile_mode"]
    _is_messaging_session_record = ctx["_is_messaging_session_record"]
    _is_subagent_child_session_id = ctx["_is_subagent_child_session_id"]
    _limited_webui_messages_for_display_with_sidecar = ctx[
        "_limited_webui_messages_for_display_with_sidecar"
    ]
    _lookup_cli_session_metadata = ctx["_lookup_cli_session_metadata"]
    _merge_cli_sidebar_metadata = ctx["_merge_cli_sidebar_metadata"]
    _merged_session_messages_for_display = ctx["_merged_session_messages_for_display"]
    _merged_webui_lineage_messages_for_display = ctx[
        "_merged_webui_lineage_messages_for_display"
    ]
    _message_summary = ctx["_message_summary"]
    _message_window_for_display = ctx["_message_window_for_display"]
    _messages_for_limited_payload = ctx["_messages_for_limited_payload"]
    _metadata_only_message_summary = ctx["_metadata_only_message_summary"]
    _parse_msg_limit = ctx["_parse_msg_limit"]
    _pre_compression_continuation_session_id = ctx[
        "_pre_compression_continuation_session_id"
    ]
    _profiles_match = ctx["_profiles_match"]
    _query_flag = ctx["_query_flag"]
    _query_positive_int = ctx["_query_positive_int"]
    _reconcile_session_detail_source_flags = ctx[
        "_reconcile_session_detail_source_flags"
    ]
    _rescale_threshold_tokens_for_context_window = ctx[
        "_rescale_threshold_tokens_for_context_window"
    ]
    _resolve_context_length_for_session_model = ctx[
        "_resolve_context_length_for_session_model"
    ]
    _resolve_effective_session_model_for_display = ctx[
        "_resolve_effective_session_model_for_display"
    ]
    _resolve_effective_session_model_provider_for_display = ctx[
        "_resolve_effective_session_model_provider_for_display"
    ]
    _run_journal_live_snapshot = ctx["_run_journal_live_snapshot"]
    _run_journal_status_payload = ctx["_run_journal_status_payload"]
    _session_context_length_lookup_state = ctx["_session_context_length_lookup_state"]
    _session_detail_tail_cache_eligible = ctx["_session_detail_tail_cache_eligible"]
    _session_detail_tail_cache_get = ctx["_session_detail_tail_cache_get"]
    _session_detail_tail_cache_key = ctx["_session_detail_tail_cache_key"]
    _session_detail_tail_cache_set = ctx["_session_detail_tail_cache_set"]
    _session_list_cache_key = ctx["_session_list_cache_key"]
    _session_list_payload_to_response = ctx["_session_list_payload_to_response"]
    _session_model_identity_matches = ctx["_session_model_identity_matches"]
    _session_requires_cli_metadata_lookup = ctx["_session_requires_cli_metadata_lookup"]
    _session_source_is_webui = ctx["_session_source_is_webui"]
    _session_visible_to_active_profile = ctx["_session_visible_to_active_profile"]
    _should_accept_session_context_length_refresh = ctx[
        "_should_accept_session_context_length_refresh"
    ]
    _state_db_backstop_limit_for_display = ctx["_state_db_backstop_limit_for_display"]
    _state_db_since_timestamp_for_limited_display = ctx[
        "_state_db_since_timestamp_for_limited_display"
    ]
    _tool_calls_for_message_window = ctx["_tool_calls_for_message_window"]
    _webui_sidecar_lineage_messages_for_display = ctx[
        "_webui_sidecar_lineage_messages_for_display"
    ]
    attach_todo_state = ctx["attach_todo_state"]
    bad = ctx["bad"]
    find_run_summary = ctx["find_run_summary"]
    get_cli_session_messages = ctx["get_cli_session_messages"]
    get_session = ctx["get_session"]
    get_state_db_session_messages = ctx["get_state_db_session_messages"]
    is_session_yolo_enabled = ctx["is_session_yolo_enabled"]
    j = ctx["j"]
    load_projects = ctx["load_projects"]
    load_settings = ctx["load_settings"]
    logger = ctx["logger"]
    merge_session_messages_append_only = ctx["merge_session_messages_append_only"]
    os = ctx["os"]
    parse_qs = ctx["parse_qs"]
    read_session_lineage_report = ctx["read_session_lineage_report"]
    redact_session_data = ctx["redact_session_data"]

    if parsed.path == "/api/session/compress/status":
        query = parse_qs(parsed.query)
        _handle_session_compress_status(handler, query.get("session_id", [""])[0])
        return True

    if parsed.path == "/api/session":
        import time as _time

        _t0 = _time.monotonic()
        _debug_slow = os.environ.get("HERMES_DEBUG_SLOW", "")
        # perf(webui/session-load-latency) tier2c: per-stage breakdown via
        # RequestDiagnostics. maybe_start() returns None for paths not in
        # the allowlist, in which case the existing _tN-driven [SLOW] log
        # is the only signal — same as before.
        _diag = RequestDiagnostics.maybe_start(
            "GET",
            parsed.path,
            logger=logger,
            print_fn=getattr(handler, "_safe_webui_print", None),
        )
        # perf(webui/session-load-latency) tier2c-followup: every early-return
        # in this handler calls `_diag.finish()` before returning so the
        # watchdog's _watchdog_pending dict stays bounded to in-flight requests.
        # Greptile flagged this in PR review — finish() unregisters the
        # pending watchdog entry; without it the entry stays for the full
        # 5s slow-request timeout and emits a spurious "Slow WebUI request
        # still running" log. Idempotent — finish() no-ops if already called.
        query = parse_qs(parsed.query)
        sid = query.get("session_id", [""])[0]
        if not sid:
            if _diag:
                _diag.finish()
            return j(handler, {"error": "session_id is required"}, status=400)
        # ?messages=0 skips the message payload for fast session switching.
        # The frontend uses this when switching conversations in the sidebar
        # (only needs metadata). The full message array is loaded lazily
        # via ?messages=1 when the message panel opens.
        load_messages = query.get("messages", ["1"])[0] != "0"
        resolve_model_default = "1" if load_messages else "0"
        resolve_model = query.get("resolve_model", [resolve_model_default])[0] != "0"
        # ?msg_limit=N returns a tail window containing the last N visible
        # transcript rows. Hidden tool-result rows do not consume the budget;
        # they are included only when they sit inside the selected window and
        # are bounded before serialization. Older rows load on-demand.
        # Clamp to _MAX_MSG_LIMIT so an oversized request (e.g. msg_limit=9999
        # from an outline jump, or a hostile value) can't force an unbounded
        # payload; the existing _messages_truncated signal covers the clamped
        # case (the client sees there are more rows than returned). Parsing +
        # clamping live in _parse_msg_limit so the expression has direct test
        # coverage; None means the bare no-msg_limit path (full transcript).
        msg_limit = _parse_msg_limit(query.get("msg_limit", [None])[0])
        # ?msg_before=N — 0-based index into the full message array.
        # Returns messages before this index (for scroll-to-top lazy loading).
        # Combined with msg_limit for paging.
        _msg_before = query.get("msg_before", [None])[0]
        try:
            msg_before = int(_msg_before) if _msg_before else None
        except (ValueError, TypeError):
            msg_before = None
        # ?expand_renderable=1 is retained for compatibility with older
        # frontends. msg_limit now counts visible transcript rows by default, so
        # the flag no longer changes the server-side pagination semantics.
        _expand_renderable = query.get("expand_renderable", [None])[0]
        expand_renderable = str(_expand_renderable).strip() in ("1", "true", "True")
        try:
            _t1 = _time.monotonic()
            if _diag:
                _diag.stage("t1_after_get_session_check")
            _detail_cache_candidate = (
                load_messages
                and msg_limit is not None
                and msg_before is None
                and not resolve_model
            )
            _detail_cache_key = None
            s = get_session(
                sid,
                metadata_only=(not load_messages) or _detail_cache_candidate,
            )
            _session_profile = getattr(s, "profile", None) or None
            if not _session_visible_to_active_profile(_session_profile, handler):
                if _session_profile:
                    # Valid session owned by a KNOWN other profile: 409 so the
                    # client can offer to switch to it (#5419).
                    if _diag:
                        _diag.finish()
                    return j(
                        handler,
                        {
                            "error": "Session belongs to a different profile",
                            "code": "session_profile_mismatch",
                            "session_id": sid,
                            "profile": _session_profile,
                        },
                        status=409,
                    )
                # Unknown/legacy None-profile sidecar: keep the original 404 so
                # the frontend's self-heal (clear stale URL + localStorage) still
                # fires. _profiles_match coerces None->'default', so a truly
                # missing/legacy session under a non-default active profile would
                # otherwise emit a useless 409 with profile=null.
                if _diag:
                    _diag.finish()
                return bad(handler, "Session not found", 404)
            original_stream_id = getattr(s, "active_stream_id", None)
            _clear_stale_stream_state(s)
            if _detail_cache_candidate:
                _detail_cache_key = _session_detail_tail_cache_key(
                    s,
                    msg_limit=msg_limit,
                    expand_renderable=expand_renderable,
                )
                _cached_detail_payload = _session_detail_tail_cache_get(
                    _detail_cache_key
                )
                if _cached_detail_payload is not None:
                    if _diag:
                        _diag.stage("session_detail_tail_cache_hit")
                        _diag.finish()
                    return j(handler, _cached_detail_payload)
                if _diag:
                    _diag.stage("session_detail_tail_cache_miss")
                if getattr(s, "_loaded_metadata_only", False):
                    s = get_session(sid, metadata_only=False)
                    _session_profile = getattr(s, "profile", None) or None
                # A full cached object may have changed while the metadata-only
                # read was in flight. Re-evaluate eligibility before storing.
                if not _session_detail_tail_cache_eligible(s):
                    _detail_cache_key = None
            cli_meta = (
                _lookup_cli_session_metadata(sid)
                if _session_requires_cli_metadata_lookup(s)
                else {}
            )
            is_messaging_session = _is_messaging_session_record(
                s
            ) or _is_messaging_session_record(cli_meta)
            cli_messages = []
            state_db_messages = []
            metadata_summary = None
            limited_sidecar_messages = None
            state_db_since_timestamp = None
            if is_messaging_session:
                cli_messages = get_cli_session_messages(sid)
            elif load_messages:
                if msg_limit is not None:
                    (
                        state_db_since_timestamp,
                        limited_sidecar_messages,
                    ) = _state_db_since_timestamp_for_limited_display(
                        s,
                        msg_limit,
                        msg_before=msg_before,
                    )
                _state_db_reader_kwargs = {"profile": _session_profile}
                if state_db_since_timestamp is not None:
                    _state_db_reader_kwargs["since_timestamp"] = (
                        state_db_since_timestamp
                    )
                # Apply the display-path row backstop ONLY on provably-safe
                # reads where no truncation_boundary prefix is required for the
                # merge — see _state_db_backstop_limit_for_display. Compressed
                # sessions and msg_before paging need their full prefix rows for
                # correct reconciliation, so those stay uncapped.
                _backstop = _state_db_backstop_limit_for_display(s, msg_before)
                if _backstop is not None:
                    _state_db_reader_kwargs["limit"] = _backstop
                state_db_messages = get_state_db_session_messages(
                    sid,
                    **_state_db_reader_kwargs,
                )
            elif not is_messaging_session:
                # Metadata-only callers still need the same append-only
                # reconciliation contract as full loads so stale/replayed
                # state.db rows do not make sidebar polling think the
                # transcript is always newer. Helper threads profile= to
                # honor #2827's TLS-vs-thread fix.
                metadata_summary = _metadata_only_message_summary(
                    sid, profile=_session_profile
                )
            _t2 = _time.monotonic()
            if _diag:
                _diag.stage("t2_after_state_db_load")
            effective_model = (
                _resolve_effective_session_model_for_display(s)
                if resolve_model
                else None
            )
            effective_provider = (
                _resolve_effective_session_model_provider_for_display(s)
                if resolve_model
                else None
            )
            _t3 = _time.monotonic()
            if _diag:
                _diag.stage("t3_after_model_resolve")
            if load_messages:
                if is_messaging_session and cli_messages:
                    # Recovery/aggregate sidecars can intentionally contain a
                    # longer visible conversation than the single state.db
                    # segment for this messaging session id. Prefer the longer
                    # sidecar so repaired WebUI history is not hidden behind the
                    # canonical per-segment transcript. When both sources carry
                    # different slices of the same stitched conversation, merge
                    # them chronologically and dedupe exact repeats.
                    _all_msgs = _merged_session_messages_for_display(s, cli_messages)
                elif msg_limit is not None:
                    _all_msgs = _limited_webui_messages_for_display_with_sidecar(
                        s,
                        limited_sidecar_messages,
                        state_db_messages,
                    )
                else:
                    _all_msgs = merge_session_messages_append_only(
                        _webui_sidecar_lineage_messages_for_display(s),
                        state_db_messages,
                        truncation_watermark=getattr(s, "truncation_watermark", None),
                        truncation_boundary=getattr(s, "truncation_boundary", None),
                    )
                    _all_msgs = _merged_webui_lineage_messages_for_display(s, _all_msgs)
            else:
                if is_messaging_session and cli_messages:
                    _all_msgs = _merged_session_messages_for_display(s, cli_messages)
                else:
                    if metadata_summary is None:
                        metadata_summary = _message_summary(
                            getattr(s, "messages", []) or []
                        )
                    _summary_message_count = metadata_summary["message_count"]
                    _summary_last_message_at = metadata_summary["last_message_at"]
                    _all_msgs = []
            if not load_messages:
                if metadata_summary is None:
                    metadata_summary = _message_summary(_all_msgs)
                    _summary_message_count = metadata_summary["message_count"]
                    _summary_last_message_at = metadata_summary["last_message_at"]
                if _summary_message_count == 0:
                    # Legacy session with no loaded sidecar and no state.db summary —
                    # fall back to the persisted metadata count from session JSON.
                    # See PR #2605 (LumenYoung): without this, the metadata poll
                    # returns 0 and the active-session external-refresh signal
                    # never trips on legacy sessions.
                    try:
                        metadata_count = getattr(s, "_metadata_message_count", None)
                        if metadata_count is not None:
                            _summary_message_count = max(0, int(metadata_count))
                    except (TypeError, ValueError):
                        pass
            else:
                _summary_message_count = None
                _summary_last_message_at = None
            if load_messages:
                _truncated_msgs, _messages_offset = _message_window_for_display(
                    _all_msgs,
                    msg_limit=msg_limit,
                    msg_before=msg_before,
                    expand_renderable=expand_renderable,
                )
                if msg_limit is not None:
                    _truncated_msgs = _messages_for_limited_payload(_truncated_msgs)
                _truncated_msgs = _hydrate_anchor_activity_scenes(
                    _truncated_msgs,
                    getattr(s, "anchor_activity_scenes", None),
                    message_offset=_messages_offset,
                    tool_calls=getattr(s, "tool_calls", None),
                )
            else:
                _truncated_msgs = []
                _messages_offset = 0
            # Index of the first returned message in the full message array.
            # Frontend uses this as cursor for scroll-to-top paging.
            _windowed_messages = (
                load_messages
                and msg_limit is not None
                and (msg_before is not None or len(_truncated_msgs) < len(_all_msgs))
            )
            # Resolve effective context_length with model-metadata fallback so
            # older sessions (pre-#1318) that have context_length=0 persisted
            # still render a meaningful indicator on load.  Mirrors the
            # SSE-path fallback in api/streaming.py:2333-2342.  Fixes #1436.
            #
            # #1896: pass config_context_length, provider, and custom_providers
            # so explicit config overrides win over the 256K default fallback.
            # Without these, an old session loaded after a user upgraded to a
            # 1M-context model with `model.context_length: 1048576` in
            # config.yaml gets a 256K window in the initial UI indicator and
            # /api/session/get response — the same wrong-window display this
            # fix addresses on the streaming side.
            _persisted_cl = getattr(s, "context_length", 0) or 0
            _threshold_tokens = getattr(s, "threshold_tokens", 0) or 0
            if (not _persisted_cl) or resolve_model:
                _stored_model_for_lookup = getattr(s, "model", "") or ""
                _stored_provider_for_lookup = getattr(s, "model_provider", None) or ""
                _model_for_lookup = (
                    effective_model or _stored_model_for_lookup
                ).strip()
                (
                    _model_for_lookup,
                    _provider_for_lookup,
                    _base_url_for_lookup,
                    _api_key_for_lookup,
                ) = _session_context_length_lookup_state(
                    _model_for_lookup,
                    effective_provider or getattr(s, "model_provider", None) or "",
                )
                _fb_cl = _resolve_context_length_for_session_model(
                    _model_for_lookup,
                    _provider_for_lookup,
                    base_url=_base_url_for_lookup,
                    api_key=_api_key_for_lookup,
                )
                _model_changed_for_context = not _session_model_identity_matches(
                    _stored_model_for_lookup,
                    _stored_provider_for_lookup,
                    _model_for_lookup,
                    _provider_for_lookup,
                )
                if _should_accept_session_context_length_refresh(
                    _persisted_cl,
                    _fb_cl,
                    model_changed=_model_changed_for_context,
                ):
                    if _persisted_cl and _fb_cl != _persisted_cl:
                        # The old threshold belongs to the old window. Hiding it
                        # is less useful than keeping the same compression ratio
                        # against the freshly resolved context length.
                        _threshold_tokens = (
                            _rescale_threshold_tokens_for_context_window(
                                _threshold_tokens,
                                _persisted_cl,
                                _fb_cl,
                            )
                        )
                    _persisted_cl = _fb_cl
            _session_tool_calls = getattr(s, "tool_calls", []) if load_messages else []
            # Always include session-level tool_calls so the browser can merge
            # them with per-message tool_calls for messages that lack the
            # per-message variant (older messages whose tool_calls live only
            # in the session-level list).  The browser-side
            # _syncToolCallsForLoadedMessages handles deduplication by tid.
            if _windowed_messages:
                _session_tool_calls = _tool_calls_for_message_window(
                    _session_tool_calls,
                    _messages_offset,
                    len(_truncated_msgs),
                )
            _merged_message_count = (
                _summary_message_count
                if _summary_message_count is not None
                else len(_all_msgs)
            )
            _merged_last_message_at = (
                _summary_last_message_at if _summary_last_message_at is not None else 0
            )
            if _summary_last_message_at is None and _all_msgs:
                try:
                    _merged_last_message_at = max(
                        float((m or {}).get("timestamp") or 0)
                        for m in _all_msgs
                        if isinstance(m, dict)
                    )
                except (TypeError, ValueError):
                    _merged_last_message_at = 0
            active_stream_ids = _active_stream_ids()
            try:
                compact_session = s.compact(
                    include_runtime=True,
                    active_stream_ids=active_stream_ids,
                )
            except TypeError:
                compact_session = s.compact()
            raw = compact_session | {
                "messages": _truncated_msgs,
                "message_count": _merged_message_count,
                "tool_calls": _session_tool_calls,
                "active_stream_id": getattr(s, "active_stream_id", None),
                "pending_user_message": getattr(s, "pending_user_message", None),
                "pending_attachments": getattr(s, "pending_attachments", [])
                if load_messages
                else [],
                "pending_started_at": getattr(s, "pending_started_at", None),
                "pending_user_source": getattr(s, "pending_user_source", None),
                "context_length": _persisted_cl,
                "threshold_tokens": _threshold_tokens,
                "last_prompt_tokens": getattr(s, "last_prompt_tokens", 0) or 0,
            }
            if original_stream_id:
                try:
                    journal = find_run_summary(original_stream_id)
                except Exception:
                    journal = None
                if journal:
                    journal_active = bool(original_stream_id in active_stream_ids)
                    raw["runtime_journal"] = _run_journal_status_payload(
                        journal,
                        active=journal_active,
                    )
                    if journal_active:
                        try:
                            snapshot = _run_journal_live_snapshot(
                                original_stream_id, handler=handler
                            )
                        except Exception:
                            logger.debug(
                                "Failed to build runtime journal snapshot for %s",
                                original_stream_id,
                                exc_info=True,
                            )
                            snapshot = None
                        if snapshot:
                            raw["runtime_journal_snapshot"] = snapshot
                            raw["pending_attachments"] = (
                                getattr(s, "pending_attachments", []) or []
                            )
            # Cold-load: derive the latest settled todo snapshot from the full
            # merged transcript, not the truncated display window. This keeps
            # the Todos panel correct after refresh even when the latest todo
            # tool result is outside msg_limit, and treats an explicit empty
            # todo list as the current state instead of falling through to an
            # older non-empty write.
            if load_messages and _all_msgs:
                attach_todo_state(raw, _all_msgs)
            if _merged_last_message_at:
                raw["last_message_at"] = max(
                    float(raw.get("last_message_at") or 0),
                    _merged_last_message_at,
                )
                raw["updated_at"] = max(
                    float(raw.get("updated_at") or 0),
                    _merged_last_message_at,
                )
            # #2980: surface the visible continuation for a hidden pre-compression
            # snapshot so a mobile reload mid-compression can recover to it.
            continuation_sid = _pre_compression_continuation_session_id(s)
            if continuation_sid:
                raw["continuation_session_id"] = continuation_sid
            if cli_meta and _session_source_is_webui(cli_meta):
                raw = _reconcile_session_detail_source_flags(raw, cli_meta)
            elif cli_meta and _is_messaging_session_record(cli_meta):
                raw = _merge_cli_sidebar_metadata(raw, cli_meta)
                # ``message_count`` in /api/session is the display coordinate
                # space used for pagination and the header badge. Messaging
                # state.db metadata can include raw duplicate transport rows that
                # _merged_session_messages_for_display() intentionally dedupes;
                # keep the raw count available as ``actual_message_count`` but
                # do not let it make the frontend expect phantom messages.
                raw["message_count"] = _merged_message_count
            # Signal to the frontend that older messages were omitted. The
            # message window cursor already reflects visible-row pagination and
            # avoids false positives when raw hidden tool rows exceed msg_limit.
            _truncated = (
                load_messages and msg_limit is not None and _messages_offset > 0
            )
            raw["_messages_truncated"] = _truncated
            raw["_messages_offset"] = _messages_offset
            raw["_msg_limit_max"] = _MAX_MSG_LIMIT
            _t4 = _time.monotonic()
            if _diag:
                _diag.stage("t4_after_compact_and_merge")
            if effective_model:
                raw["model"] = effective_model
            if effective_provider:
                raw["model_provider"] = effective_provider
            # A subagent child (#5307) is view-only regardless of what a stale
            # sidecar stored: coerce the serialized flags so the browser never
            # treats an existing subagent sidecar as writable / CLI-classified.
            if (
                str(
                    raw.get("source_tag")
                    or raw.get("raw_source")
                    or raw.get("session_source")
                    or ""
                )
                .strip()
                .lower()
                == "subagent"
            ) or _is_subagent_child_session_id(sid):
                raw["is_cli_session"] = False
                raw["read_only"] = True
            redact = redact_session_data(raw)
            _t5 = _time.monotonic()
            if _diag:
                _diag.stage("t5_after_redact")
            _response_payload = {"session": redact}
            if _detail_cache_key is not None:
                _fresh_detail_cache_key = _session_detail_tail_cache_key(
                    s,
                    msg_limit=msg_limit,
                    expand_renderable=expand_renderable,
                )
                # The response was derived between two independent filesystem
                # snapshots. Store it only when no authoritative input changed
                # during reconciliation/redaction (TOCTOU stale-cache guard).
                if _fresh_detail_cache_key == _detail_cache_key:
                    _session_detail_tail_cache_set(_detail_cache_key, _response_payload)
            resp = j(handler, _response_payload)
            _t6 = _time.monotonic()
            if _diag:
                _diag.stage("t6_after_json_write")
            _total_ms = (_t6 - _t0) * 1000
            # Always log when slow (>2s) so we don't need HERMES_DEBUG_SLOW env var
            # to diagnose latency regressions. Opt-in env var still forces
            # logging on every request for development.
            if _debug_slow or _total_ms >= 2000:
                # perf(webui/session-load-latency) tier2c: route the [SLOW] line
                # through handler._safe_webui_print() rather than logger.warning().
                # The WebUI process starts the root logger without any handler, so
                # logger.warning() calls are silently dropped (the [SLOW] line
                # previously worked only on PIDs that happened to have a logger
                # handler set up by an earlier run; today the line is invisible).
                # _safe_webui_print writes to the systemd journal socket directly,
                # same as the per-request ms line — which is why THAT line keeps
                # working.
                handler._safe_webui_print(
                    "[SLOW] session_id=%s get_session=%.1fms model_resolve=%.1fms "
                    "compact=%.1fms redact=%.1fms json_write=%.1fms total=%.1fms"
                    % (
                        sid,
                        (_t2 - _t1) * 1000,
                        (_t3 - _t2) * 1000,
                        (_t4 - _t3) * 1000,
                        (_t5 - _t4) * 1000,
                        (_t6 - _t5) * 1000,
                        _total_ms,
                    )
                )
            if _diag:
                _diag.finish()
            return resp
        except KeyError:
            # perf(webui/session-load-latency) tier2c-followup: fire
            # _diag.finish() in the exception branch too. Greptile flagged
            # this in PR review — finish() unregisters the pending watchdog
            # entry; without it the entry stays for the full 5s slow-request
            # timeout and emits a spurious "Slow WebUI request still
            # running" log. Idempotent — finish() no-ops if already called.
            if _diag:
                _diag.finish()
            # No WebUI sidecar. Delegate to the shared foreign-session
            # synthesizer so GET and POST have symmetric writeable/read-only
            # behaviour for CLI/TUI/Desktop sessions. The helper enforces the
            # #2782 deleted-WebUI-session 404 contract (via
            # _session_index_marks_was_webui) and the #4911 source ownership
            # gate (via _is_claimable_cli_source) so the two endpoints can't
            # drift on foreign-session semantics.
            cli_meta = _lookup_cli_session_metadata(sid)
            _session_profile = (cli_meta or {}).get("profile") or None
            if not _session_visible_to_active_profile(_session_profile, handler):
                if _session_profile:
                    # Valid CLI/foreign session owned by a KNOWN other profile:
                    # 409 so the client can offer to switch to it (#5419).
                    return j(
                        handler,
                        {
                            "error": "Session belongs to a different profile",
                            "code": "session_profile_mismatch",
                            "session_id": sid,
                            "profile": _session_profile,
                        },
                        status=409,
                    )
                # Missing session (cli_meta={} -> profile=None): keep the 404
                # self-heal path. _profiles_match coerces None->'default', so a
                # truly-missing session under a non-default active profile would
                # otherwise emit a useless 409 with profile=null and skip the
                # frontend self-heal + spin the SSE reconnect against a dead sid.
                return bad(handler, "Session not found", 404)
            synth, reason = _claim_or_synthesize_cli_session(
                sid, cli_meta=cli_meta or {}
            )
            if reason == "was_webui":
                # Deleted WebUI session: 404 so the client self-heals
                # (clears stale /session/<id> URL and localStorage, #2782).
                return bad(handler, "Session not found", 404)
            if synth is None:
                # 'no_foreign_state' / 'invalid_sid' — nothing to render.
                return bad(handler, "Session not found", 404)
            # Build the legacy dict response from the synthesized Session so
            # the wire shape stays byte-equivalent to the previous inline
            # synthesis (the frontend has been reading these exact keys).
            msgs = list(synth.messages or [])
            sess = {
                "session_id": synth.session_id,
                "title": synth.title,
                "workspace": synth.workspace,
                "model": synth.model,
                "message_count": len(msgs),
                "created_at": synth.created_at,
                "updated_at": synth.updated_at,
                "last_message_at": (
                    (cli_meta or {}).get("last_message_at")
                    or (cli_meta or {}).get("updated_at", 0)
                    or ((msgs or [{}])[-1].get("timestamp", 0))
                ),
                "pinned": bool(getattr(synth, "pinned", False)),
                "archived": bool(getattr(synth, "archived", False)),
                "project_id": getattr(synth, "project_id", None),
                "profile": synth.profile,
                # Read is_cli_session from the synthesized Session, not a
                # hardcoded True: delegated subagent children (#5307) are
                # recovered read-only with is_cli_session=False so they don't
                # pass the frontend _isExternalSession poll-skip / active-refresh
                # gates (#3603). Every other synthesized foreign session keeps
                # is_cli_session=True so its source badge renders.
                "is_cli_session": bool(getattr(synth, "is_cli_session", False)),
                "source_tag": synth.source_tag,
                "raw_source": synth.raw_source,
                "session_source": synth.session_source,
                "source_label": synth.source_label,
                # Greptile #4911 follow-up: read read_only from the
                # synthesized Session, NOT from cli_meta directly.
                # The helper sets synth.read_only=True for BOTH
                # explicit read_only=True cli_meta AND source-refused
                # sessions (messaging / claude_code / external_agent).
                # cli_meta.get("read_only") is only populated for the
                # explicit case, so reading it from there causes the
                # frontend to render the composer for source-refused
                # sessions and the user only discovers the block at
                # POST time with a confusing 403.
                "read_only": bool(getattr(synth, "read_only", False)),
                "messages": msgs,
                "tool_calls": [],
            }
            attach_todo_state(sess, msgs)
            sess = _merge_cli_sidebar_metadata(sess, cli_meta)
            return j(handler, {"session": redact_session_data(sess)})

    if parsed.path == "/api/session/lineage/report":
        sid = parse_qs(parsed.query).get("session_id", [""])[0]
        if not sid:
            return bad(handler, "session_id required", 400)
        report = read_session_lineage_report(_active_state_db_path(), sid)
        if not report.get("found"):
            return bad(handler, "Session not found", 404)
        return j(handler, report)

    if parsed.path == "/api/session/recovery/audit":
        from api.sessions import audit_session_recovery

        return j(
            handler,
            audit_session_recovery(SESSION_DIR, state_db_path=_active_state_db_path()),
        )

    if parsed.path == "/api/session/status":
        sid = parse_qs(parsed.query).get("session_id", [""])[0]
        if not sid:
            return bad(handler, "Missing session_id")
        try:
            from api.sessions import session_status

            _clear_stale_stream_state(get_session(sid, metadata_only=True))
            return j(handler, session_status(sid))
        except KeyError:
            return bad(handler, "Session not found", 404)

    if parsed.path == "/api/session/yolo":
        sid = parse_qs(parsed.query).get("session_id", [""])[0]
        if not sid:
            return bad(handler, "Missing session_id")
        return j(handler, {"yolo_enabled": is_session_yolo_enabled(sid)})

    if parsed.path == "/api/session/usage":
        sid = parse_qs(parsed.query).get("session_id", [""])[0]
        if not sid:
            return bad(handler, "Missing session_id")
        try:
            from api.sessions import session_usage

            return j(handler, session_usage(sid))
        except KeyError:
            return bad(handler, "Session not found", 404)

    if parsed.path == "/api/background/status":
        sid = parse_qs(parsed.query).get("session_id", [""])[0]
        if not sid:
            return bad(handler, "Missing session_id")
        from api.runs import get_background_results

        return j(handler, {"results": get_background_results(sid)})

    if parsed.path == "/api/sessions":
        diag = RequestDiagnostics.maybe_start(
            "GET",
            parsed.path,
            logger=logger,
            print_fn=getattr(handler, "_safe_webui_print", None),
        )
        try:
            from api import profiles as profiles_api

            diag.stage("load_settings")
            settings = load_settings()
            show_cli_sessions = bool(settings.get("show_cli_sessions"))
            show_claude_code_sessions = bool(settings.get("show_claude_code_sessions"))
            show_previous_messaging_sessions = bool(
                settings.get("show_previous_messaging_sessions")
            )
            show_cron_sessions = bool(settings.get("show_cron_sessions"))
            show_webhook_sessions = bool(settings.get("show_webhook_sessions"))
            agent_session_source_filter = settings.get("agent_session_source_filter")
            active_profile = profiles_api.get_active_profile_name()
            all_profiles = _all_profiles_enabled(parsed)
            include_archived = _query_flag(parsed, "include_archived")
            exclude_hidden = _query_flag(parsed, "exclude_hidden")
            archived_limit = _query_positive_int(
                parsed, "archived_limit", default=None, maximum=2000
            )
            archived_offset = _query_positive_int(
                parsed, "archived_offset", default=0, maximum=200000
            )
            sidebar_source = (
                parse_qs(parsed.query).get("sidebar_source", [""])[0].strip().lower()
                or None
            )
            if sidebar_source not in ("webui", "cli"):
                sidebar_source = None
            # /api/sessions is the default sidebar contract, so keep the route-owned
            # visible-row filter in the shared cache builder for both cache hits and misses.
            key = _session_list_cache_key(
                active_profile=active_profile,
                all_profiles=all_profiles,
                show_cli_sessions=show_cli_sessions,
                show_claude_code_sessions=show_claude_code_sessions,
                show_previous_messaging_sessions=show_previous_messaging_sessions,
                show_cron_sessions=show_cron_sessions,
                include_archived=include_archived,
                exclude_hidden=exclude_hidden,
                visible_only=True,
                show_webhook_sessions=show_webhook_sessions,
                source_filter=agent_session_source_filter,
                sidebar_source=sidebar_source,
                archived_limit=archived_limit,
                archived_offset=archived_offset,
            )
            # Keep the visible /api/sessions contract unchanged even though the
            # heavy lifting now lives in the cache builder: profile scoping via
            # `_profiles_match(s.get("profile"), active_profile)` still happens
            # before `_keep_latest_messaging_session_per_source(`.
            payload = _get_cached_session_list_payload(
                key=key,
                builder=lambda: _build_session_list_cache_payload(
                    active_profile=active_profile,
                    all_profiles=all_profiles,
                    show_cli_sessions=show_cli_sessions,
                    show_claude_code_sessions=show_claude_code_sessions,
                    show_previous_messaging_sessions=show_previous_messaging_sessions,
                    show_cron_sessions=show_cron_sessions,
                    include_archived=include_archived,
                    exclude_hidden=exclude_hidden,
                    visible_only=True,
                    show_webhook_sessions=show_webhook_sessions,
                    source_filter=agent_session_source_filter,
                    sidebar_source=sidebar_source,
                    archived_limit=archived_limit,
                    archived_offset=archived_offset,
                    diag=diag,
                ),
                diag=diag,
            )
            diag.stage("response_write")
            return j(handler, _session_list_payload_to_response(payload), pretty=False)
        finally:
            diag.finish()

    if parsed.path == "/api/projects":
        # ── Profile scoping (#1614) ────────────────────────────────────────
        # Default: filter to the active profile. ?all_profiles=1 returns the
        # aggregate list so settings/admin UIs can still see everything.
        from api import profiles as profiles_api

        active_profile = profiles_api.get_active_profile_name()
        all_projects = load_projects()
        isolated_profile_mode = _is_isolated_profile_mode()
        all_profiles = _all_profiles_enabled(parsed)
        if all_profiles:
            scoped = all_projects
            other_profile_count = 0
        else:
            scoped = [
                p
                for p in all_projects
                if _profiles_match(p.get("profile"), active_profile)
            ]
            other_profile_count = (
                0 if isolated_profile_mode else len(all_projects) - len(scoped)
            )
        return j(
            handler,
            {
                "projects": scoped,
                "all_profiles": all_profiles,
                "active_profile": active_profile,
                "other_profile_count": other_profile_count,
            },
        )
    return UNHANDLED
