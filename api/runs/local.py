"""Complete local-agent run orchestration for the streaming facade.

This module owns one admitted local run from TurnExecution startup through
journal publication, transcript persistence, recovery, terminal projection,
and idempotent cleanup. Keep those phases together: splitting callbacks or
settlement from this lifecycle would separate state decisions from their owner.
"""

from __future__ import annotations

from types import ModuleType


def run_agent_streaming(
    api: ModuleType,
    session_id,
    msg_text,
    model,
    workspace,
    stream_id,
    attachments=None,
    *,
    ephemeral=False,
    model_provider=None,
    goal_related=False,
    moa_config=None,
):
    """Run agent in background thread, writing SSE events to STREAMS[stream_id].

    When ephemeral=True, session mutations are skipped — used by /btw to get
    a streaming answer without persisting to the parent session.
    """

    # Bind every collaborator from the canonical compatibility facade once at
    # run entry. Tests and callers may monkeypatch api.streaming; resolving here
    # preserves those seams without importing the facade into this module or
    # creating a second owner for mutable runtime state.
    LOCK = api.LOCK
    PENDING_GOAL_CONTINUATION = api.PENDING_GOAL_CONTINUATION
    Path = api.Path
    SESSIONS = api.SESSIONS
    STREAM_PARTIAL_TEXT = api.STREAM_PARTIAL_TEXT
    STREAM_REASONING_TEXT = api.STREAM_REASONING_TEXT
    TurnExecution = api.TurnExecution
    _ENV_LOCK = api._ENV_LOCK
    _STREAMING_CRON_PROFILE_HOME = api._STREAMING_CRON_PROFILE_HOME
    _TOOL_ARG_CONTENT_CAP = api._TOOL_ARG_CONTENT_CAP
    _TOOL_ARG_CONTENT_KEYS = api._TOOL_ARG_CONTENT_KEYS
    _accept_pending_async_delegations = api._accept_pending_async_delegations
    _adopt_session_db_for_cached_agent = api._adopt_session_db_for_cached_agent
    _advance_truncation_watermark_after_commit = api._advance_truncation_watermark_after_commit
    _agent_cache_api_key_sig = api._agent_cache_api_key_sig
    _agent_result_terminal_failure = api._agent_result_terminal_failure
    _agent_result_tool_limit_reached = api._agent_result_tool_limit_reached
    _aiagent_import_error_detail = api._aiagent_import_error_detail
    _apply_profile_home_context_to_streaming_model = api._apply_profile_home_context_to_streaming_model
    _assign_stable_message_ids = api._assign_stable_message_ids
    _assistant_reply_added_after_current_turn = api._assistant_reply_added_after_current_turn
    _attachment_name = api._attachment_name
    _attempt_credential_self_heal = api._attempt_credential_self_heal
    _build_agent_thread_env = api._build_agent_thread_env
    _build_native_multimodal_message = api._build_native_multimodal_message
    _build_session_db_for_stream = api._build_session_db_for_stream
    _cached_agent_matches_session = api._cached_agent_matches_session
    _cached_agent_session_identity = api._cached_agent_session_identity
    _cancel_event_payload = api._cancel_event_payload
    _clarify_timeout_seconds = api._clarify_timeout_seconds
    _classify_provider_error = api._classify_provider_error
    _cleanup_ephemeral_cancelled_turn = api._cleanup_ephemeral_cancelled_turn
    _clear_thread_env = api._clear_thread_env
    _close_cached_agent_entry_at_session_boundary = api._close_cached_agent_entry_at_session_boundary
    _close_evicted_agent_at_session_boundary = api._close_evicted_agent_at_session_boundary
    _compact_summary_text = api._compact_summary_text
    _compact_for_echo_compare = api._compact_for_echo_compare
    _compression_anchor_message_key = api._compression_anchor_message_key
    _compression_summary_from_messages = api._compression_summary_from_messages
    _dedupe_replayed_context_messages = api._dedupe_replayed_context_messages
    _deduplicate_context_messages = api._deduplicate_context_messages
    _drain_webui_process_notifications = api._drain_webui_process_notifications
    _drop_synthetic_max_iteration_summary_requests = api._drop_synthetic_max_iteration_summary_requests
    _estimate_post_compression_context_tokens = api._estimate_post_compression_context_tokens
    _evict_sessions_over_cap = api._evict_sessions_over_cap
    _extract_gateway_routing_metadata = api._extract_gateway_routing_metadata
    _extract_tool_calls_from_messages = api._extract_tool_calls_from_messages
    _finalize_cancelled_turn = api._finalize_cancelled_turn
    _first_exchange_snippets = api._first_exchange_snippets
    _get_ai_agent = api._get_ai_agent
    _get_session_agent_lock = api._get_session_agent_lock
    _has_new_assistant_reply = api._has_new_assistant_reply
    _install_streaming_cronjob_profile_wrapper = api._install_streaming_cronjob_profile_wrapper
    _is_agent_compression_start_status = api._is_agent_compression_start_status
    _is_context_compression_marker = api._is_context_compression_marker
    _is_fallback_lifecycle_message = api._is_fallback_lifecycle_message
    _is_provisional_title = api._is_provisional_title
    _last_resort_sync_from_core = api._last_resort_sync_from_core
    _load_webui_prefill_context = api._load_webui_prefill_context
    _live_usage_session_snapshot = api._live_usage_session_snapshot
    _log_stream_writeback_timings = api._log_stream_writeback_timings
    _looks_invalid_generated_title = api._looks_invalid_generated_title
    _main_model_request_overrides = api._main_model_request_overrides
    _mark_latest_assistant_tool_limit_status = api._mark_latest_assistant_tool_limit_status
    _materialize_pending_user_turn_before_error = api._materialize_pending_user_turn_before_error
    _maybe_inject_max_iteration_summary_fallback = api._maybe_inject_max_iteration_summary_fallback
    _maybe_schedule_title_refresh = api._maybe_schedule_title_refresh
    _merge_display_messages_after_agent_result = api._merge_display_messages_after_agent_result
    _merged_transcript_lacks_final_assistant_answer = api._merged_transcript_lacks_final_assistant_answer
    _new_turn_context_from_messages = api._new_turn_context_from_messages
    _normalize_prefill_messages_before_user_turn = api._normalize_prefill_messages_before_user_turn
    _persistent_state_changes = api._persistent_state_changes
    _persistent_state_snapshot = api._persistent_state_snapshot
    _prefill_messages_with_webui_context = api._prefill_messages_with_webui_context
    _preserve_pre_compression_snapshot = api._preserve_pre_compression_snapshot
    _prewarm_skill_tool_modules = api._prewarm_skill_tool_modules
    _provider_error_payload = api._provider_error_payload
    _prune_context_tool_results_after_compression = api._prune_context_tool_results_after_compression
    _public_prefill_context_status = api._public_prefill_context_status
    _refresh_cached_agent_runtime = api._refresh_cached_agent_runtime
    _replace_session_db_in_kwargs = api._replace_session_db_in_kwargs
    _reset_turn_session_identity = api._reset_turn_session_identity
    _resolve_custom_provider_runtime_overrides = api._resolve_custom_provider_runtime_overrides
    _restore_display_reasoning_metadata = api._restore_display_reasoning_metadata
    _restore_reasoning_metadata = api._restore_reasoning_metadata
    _run_background_title_update = api._run_background_title_update
    _runtime_preferred_base_url = api._runtime_preferred_base_url
    _sanitize_messages_for_api = api._sanitize_messages_for_api
    _save_streaming_checkpoint = api._save_streaming_checkpoint
    _session_lacks_final_assistant_answer = api._session_lacks_final_assistant_answer
    _session_payload_with_full_messages = api._session_payload_with_full_messages
    _set_thread_env = api._set_thread_env
    _set_turn_session_identity = api._set_turn_session_identity
    _snapshot_and_append_partial_on_error = api._snapshot_and_append_partial_on_error
    _split_thinking_from_content = api._split_thinking_from_content
    _stamp_missing_message_timestamps = api._stamp_missing_message_timestamps
    _stream_writeback_can_supersede_recovery_marker = api._stream_writeback_can_supersede_recovery_marker
    _stream_writeback_is_current = api._stream_writeback_is_current
    _stream_writeback_stage = api._stream_writeback_stage
    _strip_compact_echo_suffix = api._strip_compact_echo_suffix
    _strip_xml_tool_calls = api._strip_xml_tool_calls
    _tool_result_snippet = api._tool_result_snippet
    _webui_ephemeral_system_prompt = api._webui_ephemeral_system_prompt
    _workspace_context_prefix = api._workspace_context_prefix
    alias_session_agent_lock = api.alias_session_agent_lock
    append_turn_journal_event_for_stream = api.append_turn_journal_event_for_stream
    append_runtime_partial_text = api.append_runtime_partial_text
    append_runtime_reasoning_text = api.append_runtime_reasoning_text
    attach_runtime_agent = api.attach_runtime_agent
    clear_process_wakeup_pause = api.clear_process_wakeup_pause
    coerce_reasoning_effort_for_model = api.coerce_reasoning_effort_for_model
    contextlib = api.contextlib
    ensure_agent_runtime_current = api.ensure_agent_runtime_current
    emit_todo_state = api.emit_todo_state
    finish_runtime_tool_call = api.finish_runtime_tool_call
    get_session = api.get_session
    get_state_db_session_messages = api.get_state_db_session_messages
    logger = api.logger
    json = api.json
    live_usage_prompt_estimate_after_tool_delta = api.live_usage_prompt_estimate_after_tool_delta
    meter = api.meter
    model_with_provider_context = api.model_with_provider_context
    os = api.os
    parse_reasoning_effort = api.parse_reasoning_effort
    prompt_cache_hit_percent = api.prompt_cache_hit_percent
    re = api.re
    reconciled_state_db_messages_for_session = api.reconciled_state_db_messages_for_session
    record_process_wakeup_provider_unavailable_pause = api.record_process_wakeup_provider_unavailable_pause
    redact_session_data = api.redact_session_data
    replace_runtime_reasoning_text = api.replace_runtime_reasoning_text
    resolve_model_provider = api.resolve_model_provider
    stamp_compression_exhausted_recovery = api.stamp_compression_exhausted_recovery
    start_runtime_tool_call = api.start_runtime_tool_call
    threading = api.threading
    time = api.time
    title_from = api.title_from
    traceback = api.traceback
    update_active_run = api.update_active_run
    visible_messages_for_anchor = api.visible_messages_for_anchor
    warm_models_catalog_provenance_if_cold = api.warm_models_catalog_provenance_if_cold
    _turn_route_model = model
    _turn_route_provider = model_provider
    execution = TurnExecution.start(
        stream_id=stream_id,
        session_id=session_id,
        phase="starting",
        logger=logger,
        log_label="local run",
        workspace=str(workspace),
        model=model,
        provider=model_provider,
        ephemeral=bool(ephemeral),
        record_worker_started=not ephemeral,
    )
    if execution is None:
        return
    cancel_event = execution.cancel_event
    event_sink = execution.event_sink
    s = None
    _rt = {}
    old_cwd = None
    old_exec_ask = None
    old_session_key = None
    old_session_id = None
    old_session_platform = None
    old_hermes_home = None
    old_profile_env = {}

    # MCP discovery moved to AFTER the per-profile HERMES_HOME mutation below
    # (was here at v0.51.30) — the previous placement always read the default
    # profile's mcp_servers because os.environ['HERMES_HOME'] hadn't been
    # rewritten yet.  See https://github.com/nesquena/hermes-webui/issues/1968.

    agent = None
    _live_prompt_estimate_tokens = [0]
    _live_prompt_exact_tokens = [0]
    _live_prompt_estimate_tool_delta_tokens = [0]
    _live_prompt_estimate_seen_ids = set()
    # Per-stream cache for the real per-model context_length (#3256 perf).
    # _live_usage_snapshot() runs on every metering tick (~10x/sec during
    # streaming); recomputing get_model_context_length() there triggered a
    # config read + potential metadata/network probe on every token for
    # non-default models (e.g. claude-opus-4.7-1m), freezing the stream while
    # the default model was unaffected. The value is constant for a given
    # (model, base_url, provider) within one stream, so resolve it at most
    # once. Sentinel: None=not computed, 0=not applicable/failed, >0=real cap.
    _real_ctx_cache = [None]
    _live_usage_session_cache = [None]

    def _current_live_usage_session():
        return _live_usage_session_snapshot(
            session_id,
            s,
            _live_usage_session_cache,
        )

    def _seed_live_prompt_estimate() -> int:
        """Capture the latest exact prompt size before adding live tool deltas."""
        if _live_prompt_estimate_tokens[0] > 0:
            return _live_prompt_estimate_tokens[0]
        _base = 0
        _agent = agent
        if _agent is not None:
            try:
                _cc = getattr(_agent, 'context_compressor', None)
                if _cc:
                    _base = getattr(_cc, 'last_prompt_tokens', 0) or 0
            except Exception:
                _base = 0
        if not _base:
            try:
                _session_obj = _current_live_usage_session()
                _base = getattr(_session_obj, 'last_prompt_tokens', 0) or 0
            except Exception:
                _base = 0
        _live_prompt_estimate_tokens[0] = int(_base or 0)
        _live_prompt_exact_tokens[0] = _live_prompt_estimate_tokens[0]
        return _live_prompt_estimate_tokens[0]

    def _bump_live_prompt_estimate(messages) -> int:
        """Increment a rough next-prompt estimate from live tool activity."""
        if not messages:
            return _live_prompt_estimate_tokens[0]
        _seed_live_prompt_estimate()
        _usage = live_usage_prompt_estimate_after_tool_delta(
            base_prompt_tokens=_live_prompt_exact_tokens[0],
            exact_prompt_tokens=_live_prompt_exact_tokens[0],
            messages=messages,
            turn_tool_prompt_tokens=_live_prompt_estimate_tool_delta_tokens[0],
        )
        _live_prompt_estimate_tokens[0] = _usage['last_prompt_tokens']
        _live_prompt_estimate_tool_delta_tokens[0] = _usage['turn_tool_prompt_tokens']
        return _live_prompt_estimate_tokens[0]

    def _live_usage_snapshot():
        """Best-effort live usage payload for mid-stream UI updates.

        During tool execution the final `done` event has not fired yet, but the
        frontend still benefits from seeing the latest known token / context
        values. These are exact for the most recent model call and a truthful
        lower bound for the pending next call after a tool result is appended.
        """
        _usage = {
            'input_tokens': 0,
            'output_tokens': 0,
            'estimated_cost': 0,
            'cache_read_tokens': 0,
            'cache_write_tokens': 0,
            'cache_hit_percent': None,
            'context_length': 0,
            'threshold_tokens': 0,
            'last_prompt_tokens': 0,
            'post_compression_context_tokens_estimate': None,
        }
        _session_obj = _current_live_usage_session()

        _agent = agent
        if _agent is not None:
            try:
                _usage['input_tokens'] = getattr(_agent, 'session_prompt_tokens', 0) or 0
                _usage['output_tokens'] = getattr(_agent, 'session_completion_tokens', 0) or 0
                _usage['estimated_cost'] = getattr(_agent, 'session_estimated_cost_usd', 0) or 0
                _usage['cache_read_tokens'] = getattr(_agent, 'session_cache_read_tokens', 0) or 0
                _usage['cache_write_tokens'] = getattr(_agent, 'session_cache_write_tokens', 0) or 0
            except Exception:
                pass
            try:
                _cc = getattr(_agent, 'context_compressor', None)
                if _cc:
                    _cc_cl_u = getattr(_cc, 'context_length', 0) or 0
                    # Stale-compressor self-heal (#3256, broadened): the
                    # agent-side compressor caches a context_length from the
                    # model it was *built/last-updated* with. After an in-place
                    # model switch (or when agent_init seeded it with the global
                    # model.context_length cap), that cached value can be the
                    # WRONG model's window — e.g. a session on claude-opus-4.8
                    # (1M / 936k prompt on Copilot) whose compressor still holds
                    # claude-opus-4.5's 168k. The original guard only corrected
                    # the narrow case where the cached value equalled the config
                    # cap exactly; a leftover *other-model* value (168k) slipped
                    # straight through to the live usage payload. Broaden it:
                    # ALWAYS resolve the real per-model window for the agent's
                    # CURRENT model and, when that differs from the cached value,
                    # surface the real one. Frontend hydration (GET /api/session)
                    # already does this; this aligns the streaming path with it
                    # so "refresh shows 1M, send-a-message drops to 168k" can't
                    # happen.
                    # PERF: resolve at most once per stream (cached in
                    # _real_ctx_cache). This snapshot runs on every metering
                    # tick; doing the config read + metadata lookup per tick
                    # froze non-default-model streams.
                    if _real_ctx_cache[0] is None:
                        _resolved_real = 0  # 0 = no correction / lookup failed
                        try:
                            _sm_u = str(getattr(_agent, 'model', '') or '').strip()
                            _prov_u = str(getattr(_agent, 'provider', '') or '').strip()
                            _base_u = str(getattr(_agent, 'base_url', '') or '').strip()
                            _key_u = getattr(_agent, 'api_key', '') or ''
                            if _sm_u:
                                # Resolve the real window through the SAME helper
                                # hydration uses (routes._context_length_lookup_inputs_for_model
                                # + get_model_context_length). This honors the
                                # nested per-model config override
                                # (model.<provider>.models.<model>.context_length,
                                # e.g. claude-opus-4.8 -> 1,000,000) and custom-
                                # provider keys, so the streaming/SSE path and the
                                # GET /api/session path land on the IDENTICAL value.
                                # Reusing the helper (instead of hand-reading the
                                # flat top-level model.context_length, which is
                                # None here) is what prevents a new mismatch like
                                # "refresh shows 1M, send-a-message shows 936k".
                                try:
                                    _cli_u = api._context_length_lookup_inputs_for_model
                                    _accept_u = api._should_accept_session_context_length_refresh
                                    from agent.model_metadata import get_model_context_length as _g_u
                                    # Resolve the SESSION's own profile config, not
                                    # the ambient one. This worker is a detached
                                    # thread that does NOT inherit the per-request
                                    # thread-local profile context, so a bare
                                    # get_config() resolves the process-global
                                    # (default) profile (#3294) — for a non-default
                                    # profile that pins a different per-model
                                    # context_length, that would surface the WRONG
                                    # profile's window in the live payload. Read the
                                    # session's profile home explicitly, mirroring
                                    # the worker's own _cfg resolution below.
                                    try:
                                        from api.config import get_config_for_profile_home as _gch_u
                                        from api.profiles import get_hermes_home_for_profile as _ghp_u
                                        _ph_u = _ghp_u(getattr(_session_obj, 'profile', None))
                                        _cfg_u = _gch_u(_ph_u)
                                    except Exception:
                                        from api.config import get_config as _gc_u
                                        _cfg_u = _gc_u()
                                    _lk_u = _cli_u(
                                        _sm_u,
                                        _prov_u,
                                        base_url=_base_u,
                                        api_key=_key_u,
                                        cfg=_cfg_u if isinstance(_cfg_u, dict) else {},
                                    )
                                    _real_u = _g_u(
                                        _sm_u,
                                        _lk_u.base_url,
                                        api_key=_lk_u.api_key,
                                        config_context_length=_lk_u.config_context_length,
                                        provider=_lk_u.provider or _prov_u or '',
                                        custom_providers=_lk_u.custom_providers,
                                    ) or 0
                                    # Only treat it as a correction when the real
                                    # window is valid AND disagrees with the
                                    # compressor's cached value. Equal => nothing
                                    # to fix, leave the fast path untouched.
                                    # #4248: never let a low-confidence 256k metadata
                                    # fallback clobber a LARGER cached window — that
                                    # would reintroduce the very "drops to a smaller
                                    # window mid-stream" regression this guard fixes.
                                    # Reuse the exact acceptance gate hydration uses.
                                    # NOTE: we deliberately omit model_changed (=False
                                    # default) here, unlike hydration. The streaming
                                    # path can't cheaply know if the model changed
                                    # since the compressor was seeded, so we err
                                    # toward the LARGER window (auto-compress fires
                                    # late, not early — the safe direction), and the
                                    # next GET /api/session hydration self-heals any
                                    # genuine downward 256k case via model_changed.
                                    if (
                                        _real_u and _real_u != _cc_cl_u
                                        and _accept_u(_cc_cl_u, _real_u)
                                    ):
                                        _resolved_real = _real_u
                                except TypeError:
                                    # Older hermes-agent: legacy 2-arg form.
                                    try:
                                        _accept2_u = api._should_accept_session_context_length_refresh
                                        from agent.model_metadata import get_model_context_length as _g2_u
                                        _real_u = _g2_u(_sm_u, _base_u) or 0
                                        if (
                                            _real_u and _real_u != _cc_cl_u
                                            and _accept2_u(_cc_cl_u, _real_u)
                                        ):
                                            _resolved_real = _real_u
                                    except Exception:
                                        pass
                                except Exception:
                                    pass
                        except Exception:
                            _resolved_real = 0
                        _real_ctx_cache[0] = _resolved_real
                    # Apply the cached real cap when the guard determined one.
                    if _real_ctx_cache[0]:
                        # Also rescale threshold_tokens by the same ratio so the
                        # auto-compress trigger reflects the real window, not
                        # the stale global cap (e.g. 197.2k @ 232K cap → ~850k
                        # @ 1M real cap).
                        _orig_cc_cl = getattr(_cc, 'context_length', 0) or 0
                        _orig_thresh = getattr(_cc, 'threshold_tokens', 0) or 0
                        _cc_cl_u = _real_ctx_cache[0]
                        if _orig_cc_cl > 0 and _orig_thresh > 0:
                            _scaled_thresh = int(_orig_thresh * _real_ctx_cache[0] / _orig_cc_cl)
                            _usage['context_length'] = _cc_cl_u
                            _usage['threshold_tokens'] = _scaled_thresh
                            _usage['last_prompt_tokens'] = getattr(_cc, 'last_prompt_tokens', 0) or 0
                        else:
                            _usage['context_length'] = _cc_cl_u
                            _usage['threshold_tokens'] = _orig_thresh
                            _usage['last_prompt_tokens'] = getattr(_cc, 'last_prompt_tokens', 0) or 0
                    else:
                        _usage['context_length'] = _cc_cl_u
                        _usage['threshold_tokens'] = getattr(_cc, 'threshold_tokens', 0) or 0
                        _usage['last_prompt_tokens'] = getattr(_cc, 'last_prompt_tokens', 0) or 0
            except Exception:
                pass

        if _session_obj is not None:
            for _field in ('input_tokens', 'output_tokens', 'estimated_cost', 'cache_read_tokens', 'cache_write_tokens', 'context_length', 'threshold_tokens', 'last_prompt_tokens'):
                if not _usage.get(_field):
                    try:
                        _usage[_field] = getattr(_session_obj, _field, 0) or 0
                    except Exception:
                        pass
            _post_compression_estimate = getattr(
                _session_obj, 'post_compression_context_tokens_estimate', None,
            )
            if isinstance(_post_compression_estimate, int) and _post_compression_estimate > 0:
                _usage['post_compression_context_tokens_estimate'] = _post_compression_estimate

        _real_prompt_tokens = int(_usage.get('last_prompt_tokens') or 0)
        _usage['cache_hit_percent'] = prompt_cache_hit_percent(
            _usage.get('cache_read_tokens') or 0,
            _usage.get('input_tokens') or 0,
        )
        if _real_prompt_tokens and _real_prompt_tokens != _live_prompt_exact_tokens[0]:
            _live_prompt_exact_tokens[0] = _real_prompt_tokens
            _live_prompt_estimate_tokens[0] = _real_prompt_tokens
            _live_prompt_estimate_tool_delta_tokens[0] = 0
        elif _live_prompt_estimate_tokens[0] > _real_prompt_tokens:
            _usage['last_prompt_tokens'] = _live_prompt_estimate_tokens[0]

        return _usage

    # Metering ticker — emits a metering event at 1 Hz while sessions are active.
    # When get_interval() returns >= 10.0 (no active sessions), the ticker exits
    # so no idle readings are emitted and the SSE consumer sees nothing.
    #
    # #4633/#2476: begin_session() and the ticker .start() are deferred into the
    # outer `try` below so the outer `finally` (which pops STREAMS/CANCEL_FLAGS)
    # always runs its paired end_session()/_metering_stop.set() teardown. A raise
    # between here and that `try` would otherwise leak the _sessions[stream_id]
    # entry — get_stats() only prunes sessions with first_token_ts > 0, so a
    # zero-token turn (pre-flight cancel, setup raise) is never reclaimed and its
    # count inflates the SSE `active` field. Deferring .start() until after `put`
    # is defined also removes a latent start-before-put ordering window.
    _metering_stop = threading.Event()

    def _metering_ticker():
        while True:
            interval = meter().get_interval()
            if interval >= 10.0:
                break  # nothing active — stop the ticker
            if _metering_stop.wait(interval):
                break  # stream was cancelled or ended — exit
            stats = meter().get_stats(stream_id)
            stats['session_id'] = session_id
            stats['usage'] = _live_usage_snapshot()
            put('metering', stats)

    _metering_thread = threading.Thread(target=_metering_ticker, daemon=True)

    _success_writeback_committed = False
    def put(event, data):
        # If cancelled, drop all further events except the cancel event itself
        if cancel_event.is_set() and not _success_writeback_committed and event not in ('cancel', 'error'):
            return
        event_sink.publish(event, data)

    # #5940: capture a terminal (non-retryable) provider error the Agent emits via
    # its lifecycle status_callback. The Agent aborts a non-retryable API error
    # (e.g. HTTP 400 "invalid model / no credentials") with
    # `_emit_status("❌ Non-retryable error (HTTP <code>): <detail>")` but the run
    # result / agent._last_error are empty for that path, so turn-completion below
    # fell through to the misleading `no_response` "silent rate limit, try again"
    # message. Stash the emitted terminal error here (single-element list = closure
    # write without nonlocal) so it can seed `_last_err` and let the classifier
    # surface the real, actionable cause (model_not_found / auth_mismatch).
    _captured_terminal_error = [None]

    def _agent_status_callback(kind, message):
        """Bridge Agent lifecycle status into WebUI SSE.

        Passes compression events as 'compressing' events and rate-limit/fallback
        events as 'warning' events so the frontend can surface them to the user.
        Also captures a terminal non-retryable provider error (#5940) so the
        turn-completion classifier can report the real cause instead of the
        generic no_response fallback. All other lifecycle messages are dropped.
        """
        _message = str(message or '').strip()
        _kind = str(kind or '').strip().lower()
        if not _message:
            return
        _lower = _message.lower()
        # #5940: a non-retryable terminal provider error the Agent aborted on. Keep
        # the FIRST one seen this turn (the original cause; later fallback notices
        # are handled separately below). Matched on the Agent's emitted shape.
        if (
            _captured_terminal_error[0] is None
            and 'non-retryable error' in _lower
            and 'http' in _lower
        ):
            _captured_terminal_error[0] = _message
        if _is_agent_compression_start_status(_kind, _message):
            put('compressing', {
                'session_id': session_id,
                'message': 'Compressing context',
            })
            return
        # Pass through rate-limit and fallback messages so the frontend can
        # show them as warnings via the existing messages.js 'warning' listener.
        _is_fallback_notice = _is_fallback_lifecycle_message(_kind, _message)
        if _is_fallback_notice:
            put('warning', {'type': 'fallback', 'message': _message})

    # xsession wakeup misroute root fix (Option 1): pre-init so the outer
    # finally can always reset even if an exception fires before the bind.
    # Placed ABOVE the _checkpoint_stop cluster so that cluster stays adjacent
    # to the `try:` (preserves the Issue #765 static-locator invariant).
    _turn_session_identity_tokens = None
    _streaming_cron_profile_home_token = None
    _turn_pending_source = 'webui'
    # Initialised here (before any code that may raise) so the outer `finally`
    # block can safely check `if _checkpoint_stop is not None` even when an
    # exception fires before the checkpoint thread is created (Issue #765).
    _checkpoint_stop = None
    _ckpt_thread = None
    _agent_lock = None
    try:
        # Register this stream with the global streaming meter and start the 1 Hz
        # metering ticker. Kept INSIDE the outer try so the outer `finally`'s
        # end_session()/_metering_stop.set() teardown is always paired (#4633/#2476).
        meter().begin_session(stream_id)
        _metering_thread.start()
        # Bind THIS turn's session identity to the worker thread/context BEFORE
        # any agent work (so every mid-turn notify_on_complete background spawn
        # captures THIS session, not a concurrent turn's process-global env).
        # Co-located with the existing env-restore lifecycle: set here, reset
        # in the outer finally next to _clear_thread_env().
        _turn_session_identity_tokens = _set_turn_session_identity(session_id)
        s = get_session(session_id)
        _turn_pending_source = getattr(s, 'pending_user_source', None) or 'webui'
        update_active_run(stream_id, phase="running", session_id=session_id)
        s.workspace = str(Path(workspace).expanduser().resolve())
        _last_persisted_model = None
        _last_persisted_provider = None
        _turn_owns_persisted_model = False
        provider_context = (
            str(model_provider).strip().lower()
            if model_provider is not None
            else getattr(s, "model_provider", None)
        )
        provider_context = str(provider_context).strip().lower() if provider_context else None
        _agent_lock = _get_session_agent_lock(session_id)
        # #4251: the route layer already persisted this turn's model under the
        # session lock before dispatch, so a mismatch here means a newer picker
        # write won the race and must not be clobbered by the worker thread.
        with _agent_lock:
            _last_persisted_model = getattr(s, "model", None)
            _last_persisted_provider = getattr(s, "model_provider", None)
            if _last_persisted_provider is not None:
                _last_persisted_provider = str(_last_persisted_provider).strip().lower() or None
            _persisted_model_is_empty = _last_persisted_model in (None, "")
            _provider_matches = _last_persisted_provider in (None, provider_context)
            if _persisted_model_is_empty or (
                _last_persisted_model == model and _provider_matches
            ):
                s.model = model
                s.model_provider = provider_context
                _last_persisted_model = model
                _last_persisted_provider = provider_context
                _turn_owns_persisted_model = True

        # TD1: set thread-local env context so concurrent sessions don't clobber globals
        # Check for pre-flight cancel (user cancelled before agent even started)
        if cancel_event.is_set():
            with _agent_lock:
                _finalize_cancelled_turn(s, ephemeral=ephemeral, message='Task cancelled before start.')
            put('cancel', _cancel_event_payload('Cancelled before start'))
            return

        # Resolve profile home for this agent run — use the session's own profile
        # (stamped at new_session() time from the client's S.activeProfile) so that
        # two concurrent tabs on different profiles don't clobber each other via the
        # process-level active-profile global.  Falls back gracefully.
        try:
            from api.profiles import (
                filter_runtime_env_for_gateway_parity,
                patch_skill_home_modules,
                get_hermes_home_for_profile,
                get_profile_runtime_env,
            )
            _profile_home_path = get_hermes_home_for_profile(getattr(s, 'profile', None))
            _profile_home = str(_profile_home_path)
            _streaming_cron_profile_home_token = _STREAMING_CRON_PROFILE_HOME.set(_profile_home)
            _profile_runtime_env = get_profile_runtime_env(_profile_home_path)
            _safe_profile_runtime_env = filter_runtime_env_for_gateway_parity(_profile_runtime_env)
        except ImportError:
            _profile_home = os.environ.get('HERMES_HOME', '')
            _profile_runtime_env = {}
            _safe_profile_runtime_env = {}
            patch_skill_home_modules = None

        # Profile-aware provider/model enrichment: when the session belongs
        # to a profile that specifies model.provider and model.default, use
        # those to set provider_context and repair stale models.
        model, provider_context, _repaired = _apply_profile_home_context_to_streaming_model(
            model=model,
            provider_context=provider_context,
            profile_home=_profile_home,
            has_profile=bool(getattr(s, "profile", None)),
        )
        # #4251: only apply the profile-repair persistence if this turn still
        # owns the session model/provider pair it last wrote.
        provider_context = str(provider_context).strip().lower() if provider_context else None
        with _agent_lock:
            _current_provider = getattr(s, "model_provider", None)
            if _current_provider is not None:
                _current_provider = str(_current_provider).strip().lower() or None
            if (
                _turn_owns_persisted_model
                and getattr(s, "model", None) == _last_persisted_model
                and _current_provider == _last_persisted_provider
            ):
                s.model_provider = provider_context
                if _repaired and model != (s.model or ""):
                    s.model = model

        # Capture the resolved profile name now, while profile context is
        # reliable. Used in the compression migration block to stamp s.profile
        # on the continuation session. We resolve it here rather than calling
        # get_active_profile_name() at compression time because that function
        # reads thread-local storage (_tls.profile) set by set_request_profile()
        # on the HTTP handler thread. The streaming thread is a separate
        # threading.Thread and does not inherit TLS. At compression time,
        # get_active_profile_name() would fall back to the process-global
        # _active_profile, which may belong to a different concurrent tab.
        _resolved_profile_name = getattr(s, 'profile', None)
        if not _resolved_profile_name:
            try:
                from api.profiles import get_active_profile_name
                _resolved_profile_name = get_active_profile_name()
            except Exception:
                _resolved_profile_name = None

        _thread_env = _build_agent_thread_env(
            _profile_runtime_env,
            str(s.workspace),
            session_id,
            _profile_home,
        )
        _set_thread_env(**_thread_env)
        # process_complete agent-wakeup wiring (ours-original, Option B): bind
        # this session's HERMES_SESSION_KEY to its WebUI session_id so the
        # drain thread can route notify_on_complete events back to the right
        # SSE channel / server-side wakeup.
        try:
            from api.background_process import register_process_session
            register_process_session(session_id, session_id)
        except Exception:
            logger.debug("register_process_session failed", exc_info=True)
        # first-time module initialisation (which can be slow) does not
        # block other concurrent sessions waiting on _ENV_LOCK (#2024).
        ensure_agent_runtime_current()
        _prewarm_skill_tool_modules()
        _install_streaming_cronjob_profile_wrapper()
        # Still set process-level env as fallback for tools that bypass thread-local
        # Acquire lock only for the env mutation, then release before the agent runs.
        # The finally block re-acquires to restore — keeping critical sections short
        # and preventing a deadlock where the restore would re-enter the same lock.
        with _ENV_LOCK:
            old_profile_env = {key: os.environ.get(key) for key in _safe_profile_runtime_env}
            old_cwd = os.environ.get('TERMINAL_CWD')
            old_exec_ask = os.environ.get('HERMES_EXEC_ASK')
            old_session_key = os.environ.get('HERMES_SESSION_KEY')
            old_session_id = os.environ.get('HERMES_SESSION_ID')
            old_session_platform = os.environ.get('HERMES_SESSION_PLATFORM')
            old_session_chat_id = os.environ.get('HERMES_SESSION_CHAT_ID')
            old_hermes_home = os.environ.get('HERMES_HOME')
            os.environ.update(_safe_profile_runtime_env)
            os.environ['TERMINAL_CWD'] = str(s.workspace)
            os.environ['HERMES_EXEC_ASK'] = '1'
            os.environ['HERMES_SESSION_KEY'] = session_id
            os.environ['HERMES_SESSION_ID'] = session_id
            os.environ['HERMES_SESSION_PLATFORM'] = 'webui'
            # process_complete wiring (ours-original, Option B): see
            # _build_agent_thread_env above.
            os.environ['HERMES_SESSION_CHAT_ID'] = str(session_id)
            if _profile_home:
                os.environ['HERMES_HOME'] = _profile_home
                # Patch skill module caches to match the active profile.
                # _set_hermes_home() does this for process-wide switches
                # but per-request switches skip it (#1700). The in-chat
                # cronjob tool is wrapped separately at its tool-call boundary
                # with cron_profile_context_for_home (#4580) so cron.jobs path
                # caches are not mutated for the entire agent turn.
                # Modules were prewarmed by _prewarm_skill_tool_modules()
                # above, so we only do lightweight sys.modules lookups and
                # attribute assignments here — no first-time import under
                # the lock (#2024).
                if patch_skill_home_modules is not None:
                    patch_skill_home_modules(Path(_profile_home))
        # Lock released — agent runs without holding it
        # ── MCP Server Discovery (lazy import, idempotent) ──
        # MUST run AFTER the HERMES_HOME mutation above — `discover_mcp_tools()`
        # reads `~/.hermes/config.yaml` via `get_hermes_home()`, which uses
        # `os.environ['HERMES_HOME']`.  Calling it before the mutation always
        # loaded the default profile's `mcp_servers`, even when the session
        # was stamped with a non-default profile.  See issue #1968.
        #
        # NOTE: `_servers` in `tools/mcp_tool.py` is a process-global registry
        # keyed by server name.  This means once profile A registers a server
        # named e.g. `postgres`, profile B's discovery sees it as already
        # connected and skips it — even if B's config points at a different
        # binary.  Fully fixing multi-profile concurrent use requires keying
        # `_servers` by `(profile_home, name)` upstream in hermes-agent; that
        # lives outside this WebUI repo.  This change fixes the headline bug
        # for users who run a single non-default profile per WebUI process.
        try:
            from tools.mcp_tool import discover_mcp_tools
            discover_mcp_tools()
        except Exception:
            pass  # MCP not available or not configured — non-fatal

        # Register a gateway-style notify callback so the approval system can
        # push the `approval` SSE event the moment a dangerous command is
        # detected, without waiting for the next on_tool() poll cycle.
        # Without this, the agent thread blocks inside the terminal tool
        # waiting for approval that the UI never knew to ask for, leaving
        # the chat stuck in "Thinking…" forever.
        _approval_registered = False
        _unreg_notify = None
        _cleanup_gateway_pending_mirror = None
        try:
            try:
                from api.route_approvals import (
                    submit_gateway_pending_mirror as _submit_pending_for_polling,
                    reconcile_gateway_pending_mirror_locked as _reconcile_gateway_pending_mirror_locked,
                    _approval_sse_notify_locked as _approval_sse_notify_locked,
                    _lock as _approval_lock,
                )
                def _cleanup_gateway_pending_mirror():
                    with _approval_lock:
                        head, total, _changed = _reconcile_gateway_pending_mirror_locked(session_id)
                        _approval_sse_notify_locked(session_id, head, total)
            except ImportError:
                _submit_pending_for_polling = None
                _cleanup_gateway_pending_mirror = None
            from tools.approval import (
                register_gateway_notify as _reg_notify,
                unregister_gateway_notify as _unreg_notify,
            )
            def _approval_notify_cb(approval_data):
                if _submit_pending_for_polling is not None:
                    try:
                        _submit_pending_for_polling(session_id, approval_data)
                    except Exception:
                        logger.warning("Failed to mirror approval into WebUI polling state", exc_info=True)
                put('approval', approval_data)
            _reg_notify(session_id, _approval_notify_cb)
            _approval_registered = True
        except ImportError:
            logger.debug("Approval module not available, falling back to polling")

        _clarify_registered = False
        _unreg_clarify_notify = None
        try:
            from api.clarify import (
                register_gateway_notify as _reg_clarify_notify,
                unregister_gateway_notify as _unreg_clarify_notify,
            )

            def _clarify_notify_cb(clarify_data):
                put('clarify', clarify_data)

            _reg_clarify_notify(session_id, _clarify_notify_cb)
            _clarify_registered = True
        except ImportError:
            logger.debug("Clarify module not available, falling back to polling")

        def _clarify_callback_impl(question, choices, sid, cancel_evt, put_event):
            """Bridge Hermes clarify prompts to the WebUI."""
            timeout = _clarify_timeout_seconds()
            choices_list = [str(choice) for choice in (choices or [])]
            data = {
                'question': str(question or ''),
                'choices_offered': choices_list,
                'session_id': sid,
                'kind': 'clarify',
                'requested_at': time.time(),
                'timeout_seconds': timeout,
            }
            try:
                from api.clarify import submit_pending as _submit_clarify_pending, clear_pending as _clear_clarify_pending
            except ImportError:
                return (
                    "The user did not provide a response within the time limit. "
                    "Use your best judgement to make the choice and proceed."
                )

            entry = _submit_clarify_pending(sid, data)
            deadline = time.monotonic() + timeout
            while True:
                if cancel_evt.is_set():
                    _clear_clarify_pending(sid)
                    return (
                        "The user did not provide a response within the time limit. "
                        "Use your best judgement to make the choice and proceed."
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _clear_clarify_pending(sid)
                    return (
                        "The user did not provide a response within the time limit. "
                        "Use your best judgement to make the choice and proceed."
                    )
                if entry.event.wait(timeout=min(1.0, remaining)):
                    response = str(entry.result or "").strip()
                    return (
                        response
                        or "The user did not provide a response within the time limit. "
                           "Use your best judgement to make the choice and proceed."
                    )

        try:
            _token_sent = False  # tracks whether any streamed tokens were sent
            _self_healed = False  # (#1401) prevents infinite self-heal retries
            # Per-message reasoning: dict maps assistant-message index → accumulated text
            # (#3587) replaces the flat _reasoning_text string so each intermediate
            # assistant turn (before tool calls) keeps its own reasoning segment.
            _reasoning_segments: dict = {}
            _current_reasoning_idx = 0
            _tool_boundary_advanced = False
            _live_tool_calls = []  # tool progress fallback when final messages omit tool IDs

            # Throttle: emit metering events at most every 100 ms so the per-message
            # TPS label feels live during fast token streams without flooding SSE.
            _metering_last_emit = [time.monotonic() - 1]  # fire immediately on first token
            _reasoning_last_put = [0.0]
            _reasoning_buffer = ['']
            _metering_output_deltas = [0]
            _metering_reasoning_deltas = [0]

            def _flush_reasoning_buffer():
                # #4729: emit any coalesced-but-not-yet-flushed reasoning text immediately.
                # The ~10 Hz throttle in on_reasoning leaves a sub-100ms tail in the buffer;
                # the agent never calls reasoning_callback(None), and reasoning can transition
                # to tool calls / visible output, so we must flush at every boundary that
                # closes or reorders the live reasoning stream — otherwise the tail is
                # silently lost from the live Thinking view (the frontend appends deltas).
                if _reasoning_buffer[0]:
                    put('reasoning', {'text': _reasoning_buffer[0]})
                    _reasoning_buffer[0] = ''


            def _emit_metering():
                now = time.monotonic()
                if now - _metering_last_emit[0] < 0.1:
                    return
                _metering_last_emit[0] = now
                stats = meter().get_stats(stream_id)
                stats['session_id'] = session_id
                stats['usage'] = _live_usage_snapshot()
                stats.setdefault('tps_available', False)
                stats.setdefault('estimated', False)
                put('metering', stats)

            def _is_visible_output_echo(text: str) -> bool:
                candidate = _compact_for_echo_compare(text)
                if not candidate:
                    return False
                visible_output = STREAM_PARTIAL_TEXT.get(stream_id, '')
                visible_tail = _compact_for_echo_compare(
                    visible_output[-max(len(str(text)) * 2, 512):]
                )
                if visible_tail and visible_tail.endswith(candidate):
                    return True
                # Some runtimes can report a prefix of the already-streamed final
                # answer through reasoning after visible output has completed. That
                # prefix is not a tail echo, so catch only substantial chunks that
                # are already present in the visible assistant stream. Short text
                # stays on the stricter suffix path to avoid hiding genuine
                # reasoning that happens to reuse an answer phrase.
                if len(candidate) < 80:
                    return False
                visible_compact = _compact_for_echo_compare(visible_output)
                return bool(visible_compact and candidate in visible_compact)

            def _strip_reasoning_output_echo(text: str) -> bool:
                nonlocal _reasoning_segments
                removed = False
                if stream_id in STREAM_REASONING_TEXT:
                    next_text, did_remove = _strip_compact_echo_suffix(
                        STREAM_REASONING_TEXT.get(stream_id, ''),
                        text,
                    )
                    if did_remove:
                        replace_runtime_reasoning_text(stream_id, next_text)
                        removed = True
                next_buffer, did_remove_buffer = _strip_compact_echo_suffix(_reasoning_buffer[0], text)
                if did_remove_buffer:
                    _reasoning_buffer[0] = next_buffer
                    removed = True
                for idx in (_current_reasoning_idx, _current_reasoning_idx - 1):
                    if idx not in _reasoning_segments:
                        continue
                    next_segment, did_remove_segment = _strip_compact_echo_suffix(
                        _reasoning_segments.get(idx, ''),
                        text,
                    )
                    if not did_remove_segment:
                        continue
                    if next_segment:
                        _reasoning_segments[idx] = next_segment
                    else:
                        _reasoning_segments.pop(idx, None)
                    removed = True
                    break
                return removed

            def on_token(text):
                nonlocal _token_sent
                if text is None:
                    return  # end-of-stream sentinel
                # #4729: visible output is starting — flush any buffered reasoning tail
                # first so the live Thinking stream is complete before/at the transition.
                _flush_reasoning_buffer()
                _token_sent = True
                # Mirror recoverable partial text through its lifecycle owner;
                # a late callback cannot recreate buffers after teardown.
                append_runtime_partial_text(stream_id, text)
                put('token', {'text': text})
                # Update live throughput from stream delta callbacks, not from
                # byte/character length. If a backend cannot provide live deltas,
                # the frontend hides TPS rather than showing an estimate.
                _metering_output_deltas[0] += 1
                meter().record_token(stream_id, _metering_output_deltas[0])
                _emit_metering()

            def on_reasoning(text):
                nonlocal _reasoning_segments, _current_reasoning_idx, _tool_boundary_advanced
                if text is None:
                    # Flush any remaining coalesced reasoning buffer so the last
                    # partial window is not lost when the reasoning phase ends.
                    _flush_reasoning_buffer()
                    return
                _tool_boundary_advanced = False
                reasoning_delta = str(text)
                # Some runtimes mirror user-visible progress text through the
                # reasoning channel after it already streamed as normal assistant
                # output. Treat that as an echo, otherwise the UI renders the
                # same sentence again inside a Thinking card.
                if _is_visible_output_echo(reasoning_delta):
                    return
                # Accumulate into the current message's segment (#3587)
                _reasoning_segments[_current_reasoning_idx] = (
                    _reasoning_segments.get(_current_reasoning_idx, '') + reasoning_delta
                )
                # Mirror full concatenation to shared dict so cancel_stream() can persist
                # it (#1361 §A). Cancel only creates one partial message, so the flat
                # concatenation is correct there.
                append_runtime_reasoning_text(stream_id, reasoning_delta)
                # Accumulate into a coalescing buffer so every delta reaches the
                # browser — reasoning deltas are incremental, not idempotent.
                _reasoning_buffer[0] += reasoning_delta
                # Throttle reasoning SSE events to ~10 Hz to avoid overwhelming the
                # frontend renderer. Each event triggers _parseStreamState() which
                # scans the full accumulated text — 10k+ reasoning tokens/second
                # builds up and locks the JS main thread. The user still sees live
                # Thinking updates, just at a sustainable rate.
                now = time.monotonic()
                if now - _reasoning_last_put[0] >= 0.1:
                    _reasoning_last_put[0] = now
                    put('reasoning', {'text': _reasoning_buffer[0]})
                    _reasoning_buffer[0] = ''
                # Track reasoning deltas in the meter so live TPS reflects all AI output.
                _metering_reasoning_deltas[0] += 1
                meter().record_reasoning(stream_id, _metering_reasoning_deltas[0])
                _emit_metering()

            def on_interim_assistant(text, **cb_kwargs):
                nonlocal _current_reasoning_idx
                # Advance the per-message reasoning index unconditionally (#3587):
                # even if this callback fires with empty text, a new assistant
                # segment is starting and subsequent reasoning must be attributed
                # to the next message.
                _current_reasoning_idx += 1
                if text is None:
                    return
                visible = str(text).strip()
                if not visible:
                    return
                reasoning_echo = _strip_reasoning_output_echo(visible)
                already_streamed = bool(cb_kwargs.get('already_streamed', False)) or _is_visible_output_echo(visible)
                payload = {
                    'text': visible,
                    'already_streamed': already_streamed,
                }
                if reasoning_echo:
                    payload['reasoning_echo'] = True
                put('interim_assistant', payload)

            # Pre-initialise the activity counter here so on_tool (which
            # closes over it) never captures an unbound name even if this
            # block is reordered later (Issue #765).
            _checkpoint_activity = [0]
            _live_tool_event_start_ids = set()
            _live_tool_event_complete_ids = set()

            def _tool_args_snapshot(args):
                args_snap = {}
                if isinstance(args, dict):
                    for k, v in list(args.items())[:4]:
                        s2 = str(v)
                        cap = _TOOL_ARG_CONTENT_CAP if str(k).lower() in _TOOL_ARG_CONTENT_KEYS else 120
                        args_snap[k] = s2[:cap] + ('...' if len(s2) > cap else '')
                return args_snap

            def _record_live_tool_start(tool_call_id, name, args):
                if not tool_call_id or tool_call_id in _live_prompt_estimate_seen_ids:
                    return False
                _live_prompt_estimate_seen_ids.add(tool_call_id)
                _tool_call = {
                    'id': tool_call_id,
                    'type': 'function',
                    'function': {
                        'name': str(name or ''),
                        'arguments': json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False, sort_keys=True),
                    },
                }
                _bump_live_prompt_estimate([{
                    'role': 'assistant',
                    'content': '',
                    'tool_calls': [_tool_call],
                }])
                return True

            def _record_live_tool_complete(tool_call_id, name, function_result):
                if not tool_call_id:
                    return False
                _result_text = _tool_result_snippet(function_result)
                _bump_live_prompt_estimate([{
                    'role': 'tool',
                    'name': str(name or ''),
                    'tool_call_id': tool_call_id,
                    'content': _result_text,
                }])
                return True

            def on_tool(*cb_args, **cb_kwargs):
                nonlocal _reasoning_segments, _current_reasoning_idx, _tool_boundary_advanced
                # #4729: a tool boundary closes/reorders the live reasoning stream — flush
                # any buffered reasoning tail first so it isn't stranded behind the tool event.
                _flush_reasoning_buffer()
                event_type = None
                name = None
                preview = None
                args = None

                if len(cb_args) >= 4:
                    event_type, name, preview, args = cb_args[:4]
                elif len(cb_args) == 3:
                    name, preview, args = cb_args
                    event_type = 'tool.started'
                elif len(cb_args) == 2:
                    event_type, name = cb_args
                elif len(cb_args) == 1:
                    name = cb_args[0]
                    event_type = 'tool.started'

                if event_type in ('reasoning.available', '_thinking'):
                    reason_text = preview if event_type == 'reasoning.available' else name
                    if reason_text:
                        reason_delta = str(reason_text)
                        # Older tool-progress paths can mirror the same visible
                        # progress text already emitted through stream_delta_callback.
                        # Suppress those echoes like the dedicated reasoning callback.
                        if _is_visible_output_echo(reason_delta):
                            return
                        # Accumulate into the current message's segment (#3587)
                        _reasoning_segments[_current_reasoning_idx] = (
                            _reasoning_segments.get(_current_reasoning_idx, '') + reason_delta
                        )
                        # Mirror full concatenation for cancellation recovery.
                        append_runtime_reasoning_text(stream_id, reason_delta)
                        put('reasoning', {'text': reason_delta})
                        _metering_reasoning_deltas[0] += 1
                        meter().record_reasoning(stream_id, _metering_reasoning_deltas[0])
                        _emit_metering()
                    return

                # (#3587) Advance reasoning index at tool-call boundaries.
                # on_interim_assistant is suppressed for contentless tool-call
                # messages (run_agent.py:3834), so the index never advances
                # there. The first tool.started event after reasoning indicates
                # a new assistant message boundary.
                if not _tool_boundary_advanced and _current_reasoning_idx in _reasoning_segments:
                    _current_reasoning_idx += 1
                    _tool_boundary_advanced = True

                args_snap = _tool_args_snapshot(args)

                # Modern Hermes Agent builds can call both tool_progress_callback
                # and the structured tool_start/tool_complete callbacks for the
                # same tool. Prefer the structured path when it is supported so
                # the browser receives one tid-tagged tool card per real call.
                if event_type in (None, 'tool.started') and 'tool_start_callback' in _agent_params:
                    return

                if event_type in (None, 'tool.started'):
                    _live_tool_calls.append({
                        'name': name,
                        'args': args if isinstance(args, dict) else {},
                    })
                    # Mirror to the runtime owner so cancellation can persist it.
                    start_runtime_tool_call(
                        stream_id,
                        name=name,
                        args=args if isinstance(args, dict) else {},
                    )
                    put('tool', {
                        'event_type': event_type or 'tool.started',
                        'name': name,
                        'preview': preview,
                        'args': args_snap,
                    })
                    _tool_stats = meter().get_stats(stream_id)
                    _tool_stats['session_id'] = session_id
                    _tool_stats['usage'] = _live_usage_snapshot()
                    put('metering', _tool_stats)
                    # Fallback: poll for pending approval in case notify_cb wasn't
                    # registered (e.g. older approval module without gateway support).
                    try:
                        from api.route_approvals import (
                            _gateway_queues as _approval_gateway_queues,
                            _lock as _approval_lock,
                            _pending as _approval_pending,
                            reconcile_gateway_pending_mirror_locked as _reconcile_gateway_pending_mirror_locked,
                        )
                        from tools.approval import has_blocking_approval as _has_blocking_approval
                        if _has_blocking_approval(session_id):
                            p = None
                            with _approval_lock:
                                _reconcile_gateway_pending_mirror_locked(session_id)
                                queue = _approval_pending.get(session_id)
                                if isinstance(queue, list):
                                    p = dict(queue[0]) if queue else None
                                elif queue:
                                    p = dict(queue)
                                if p is None:
                                    gw_queue = _approval_gateway_queues.get(session_id) or []
                                    if gw_queue:
                                        raw = getattr(gw_queue[0], 'data', None) or {}
                                        if raw:
                                            p = dict(raw)
                                        else:
                                            logger.warning("Gateway queue entry for %s has no .data attribute", session_id)
                            if p:
                                put('approval', p)
                    except ImportError:
                        pass
                    return

                if event_type == 'tool.completed' and 'tool_complete_callback' in _agent_params:
                    return

                if event_type == 'tool.completed':
                    for live_tc in reversed(_live_tool_calls):
                        if live_tc.get('done'):
                            continue
                        if not name or live_tc.get('name') == name:
                            live_tc['done'] = True
                            live_tc['duration'] = cb_kwargs.get('duration')
                            live_tc['is_error'] = bool(cb_kwargs.get('is_error', False))
                            break
                    finish_runtime_tool_call(
                        stream_id,
                        name=name,
                        duration=cb_kwargs.get('duration'),
                        is_error=bool(cb_kwargs.get('is_error', False)),
                    )
                    # Signal the checkpoint thread that new work has completed (Issue #765).
                    # Each completed tool call is a meaningful unit of progress worth persisting.
                    _checkpoint_activity[0] += 1
                    put('tool_complete', {
                        'event_type': event_type,
                        'name': name,
                        'preview': preview,
                        'args': args_snap,
                        'duration': cb_kwargs.get('duration'),
                        'is_error': bool(cb_kwargs.get('is_error', False)),
                    })
                    # Mirror the todo tool's in-memory state into a
                    # dedicated SSE event so the Todos panel can update
                    # in real-time without waiting for the turn to
                    # settle. The helper guards on name=='todo', sends
                    # the full snapshot (idempotent under SSE replay)
                    # and swallows internal errors so emission never
                    # breaks tool delivery. Prefer the structured
                    # `result` kwarg from modern Hermes builds; fall
                    # back to the truncated `preview` only when the
                    # callback was invoked without one (older builds).
                    #
                    # Graceful degradation on old builds: `preview` is a
                    # truncated snippet, so its JSON is usually unparseable.
                    # parse_todo_tool_result() then returns None and NO
                    # todo_state event is emitted — live panel updates are
                    # silently unavailable on pre-`result` builds. This is
                    # intended: the panel still hydrates via cold-load on the
                    # next session GET; it just won't update mid-stream.
                    emit_todo_state(
                        put,
                        name=name,
                        function_result=(
                            cb_kwargs.get('result')
                            if cb_kwargs.get('result') is not None
                            else preview
                        ),
                        session_id=session_id,
                        stream_id=stream_id,
                    )
                    _tool_stats = meter().get_stats(stream_id)
                    _tool_stats['session_id'] = session_id
                    _tool_stats['usage'] = _live_usage_snapshot()
                    put('metering', _tool_stats)
                    return

            def on_tool_start(tool_call_id, name, args):
                try:
                    _record_live_tool_start(tool_call_id, name, args)
                    if tool_call_id and tool_call_id not in _live_tool_event_start_ids:
                        _live_tool_event_start_ids.add(tool_call_id)
                        _live_tool_calls.append({
                            'name': name,
                            'args': args if isinstance(args, dict) else {},
                            'tid': tool_call_id,
                        })
                        start_runtime_tool_call(
                            stream_id,
                            name=name,
                            args=args if isinstance(args, dict) else {},
                            tool_call_id=tool_call_id,
                        )
                        put('tool', {
                            'event_type': 'tool.started',
                            'name': name,
                            'preview': None,
                            'args': _tool_args_snapshot(args),
                            'tid': tool_call_id,
                        })
                    _tool_stats = meter().get_stats(stream_id)
                    _tool_stats['session_id'] = session_id
                    _tool_stats['usage'] = _live_usage_snapshot()
                    put('metering', _tool_stats)
                except Exception:
                    logger.debug('Failed to update live prompt estimate on tool start', exc_info=True)

            def on_tool_complete(tool_call_id, name, args, function_result):
                try:
                    _record_live_tool_complete(tool_call_id, name, function_result)
                    if tool_call_id and tool_call_id not in _live_tool_event_complete_ids:
                        _live_tool_event_complete_ids.add(tool_call_id)
                        result_snippet = _tool_result_snippet(function_result)
                        for live_tc in reversed(_live_tool_calls):
                            if live_tc.get('done'):
                                continue
                            if live_tc.get('tid') == tool_call_id or (not live_tc.get('tid') and live_tc.get('name') == name):
                                live_tc['done'] = True
                                live_tc['snippet'] = result_snippet
                                break
                        finish_runtime_tool_call(
                            stream_id,
                            name=name,
                            tool_call_id=tool_call_id,
                            snippet=result_snippet,
                        )
                        _checkpoint_activity[0] += 1
                        put('tool_complete', {
                            'event_type': 'tool.completed',
                            'name': name,
                            'preview': result_snippet,
                            'args': _tool_args_snapshot(args),
                            'tid': tool_call_id,
                            'is_error': False,
                        })
                        # Mirror the todo tool's in-memory state into
                        # a dedicated SSE event so the Todos panel can
                        # update in real-time without waiting for the
                        # turn to settle. See the legacy path above
                        # for the contract; the helper handles the
                        # name guard, payload shape, and swallow-all
                        # error policy.
                        emit_todo_state(
                            put,
                            name=name,
                            function_result=function_result,
                            session_id=session_id,
                            stream_id=stream_id,
                        )
                    _tool_stats = meter().get_stats(stream_id)
                    _tool_stats['session_id'] = session_id
                    _tool_stats['usage'] = _live_usage_snapshot()
                    put('metering', _tool_stats)
                except Exception:
                    logger.debug('Failed to update live prompt estimate on tool completion', exc_info=True)

            _AIAgent = _get_ai_agent()
            if _AIAgent is None:
                raise ImportError(_aiagent_import_error_detail())

            # Initialize SessionDB so session_search works in WebUI sessions
            _state_db_path = (Path(_profile_home) / "state.db") if _profile_home else None
            _session_db = _build_session_db_for_stream(_state_db_path)
            # #5979: publish catalog provenance from the durable disk cache when
            # memory is cold, so the custom-proxy resolver below sees the
            # endpoint-advertised model ids (non-blocking, disk-only, never
            # live-rebuilds). Both the warm and the resolve read profile-keyed
            # config (cache path + source fingerprint via get_active_profile_name),
            # but this streaming worker is a separate thread that does NOT inherit
            # the HTTP handler's request-profile TLS — without binding it, a cold
            # send from a NAMED profile would resolve against the DEFAULT profile's
            # config and route to the wrong provider/base_url. Bind the captured
            # owning-session profile across warm + resolve so both see the right
            # profile (no-op for the default/root profile).
            from api import profiles as profiles_api
            # #5979: treat this send as a deliberate pick ONLY when the persisted
            # explicit-pick signature matches the CURRENT model+provider routing
            # context. Storing/comparing a signature (not a bare bool) means a
            # later model/provider change via /api/chat/start, /api/session/update,
            # normalization, or provider repair automatically invalidates a stale
            # pick — so a #433 first-party leftover is never wrongly preserved on
            # a cold catalog. Only affects the cold custom-proxy branch; warm
            # endpoint-advertised provenance always wins over this flag.
            from api.models import model_explicit_pick_signature as _mk_sig
            _picked_sig = getattr(s, "model_explicit_pick_signature", None)
            # Compare against the session's persisted model+provider — the exact
            # fields /api/chat/start stamped the signature from (it persists the
            # resolved model+provider onto the session before dispatch). Falls
            # back to the worker's model/provider_context if the session fields
            # are unset. A mismatch (any later model/provider change) yields a
            # different signature → treated as NOT a deliberate pick.
            _sig_model = getattr(s, "model", None) or model
            _sig_provider = getattr(s, "model_provider", None) or provider_context
            _current_sig = _mk_sig(_sig_model, _sig_provider)
            _explicitly_picked = bool(_picked_sig) and _picked_sig == _current_sig
            with profiles_api.profile_scope_for_detached_worker(
                _resolved_profile_name, "model resolution", logger_override=logger
            ):
                warm_models_catalog_provenance_if_cold()
                resolved_model, resolved_provider, resolved_base_url = resolve_model_provider(
                    model_with_provider_context(model, provider_context),
                    explicitly_picked=_explicitly_picked,
                )
            configured_base_url = resolved_base_url

            # Resolve API key via Hermes runtime provider (matches gateway behaviour).
            # Pass the resolved provider so non-default providers get their own credentials.
            resolved_api_key = None
            try:
                from api.oauth import resolve_runtime_provider_with_anthropic_env_lock
                from hermes_cli.runtime_provider import resolve_runtime_provider
                _rt = resolve_runtime_provider_with_anthropic_env_lock(
                    resolve_runtime_provider,
                    requested=resolved_provider,
                    target_model=resolved_model,
                )
                resolved_api_key = _rt.get("api_key")
                if not resolved_provider:
                    resolved_provider = _rt.get("provider")
                resolved_base_url = _runtime_preferred_base_url(
                    _rt, resolved_provider, configured_base_url
                )
            except Exception as _e:
                print(f"[webui] WARNING: resolve_runtime_provider failed: {_e}", flush=True)

            # Named custom providers (custom:slug) may not be resolvable by
            # hermes_cli.runtime_provider directly. Fall back to config.yaml
            # custom_providers[] so WebUI can pass explicit creds/base_url.
            resolved_provider, resolved_api_key, resolved_base_url = _resolve_custom_provider_runtime_overrides(
                resolved_provider, resolved_api_key, resolved_base_url
            )

            # Read per-profile config at call time (not module-level snapshot).
            # The streaming worker is a detached thread that does NOT inherit the
            # per-request thread-local profile context, so the ambient
            # get_config() would resolve the process-global (default) profile and
            # leak the wrong profile's toolsets / prefill / fallback config into
            # this run (issue #3294). Read the SESSION's own profile home
            # explicitly so toolsets and context match the profile the session
            # actually runs under.
            from api.config import get_config_for_profile_home as _get_config_for_home
            try:
                _cfg = _get_config_for_home(_profile_home)
            except Exception:
                from api.config import get_config as _get_config
                _cfg = _get_config()
            _prefill_context = _load_webui_prefill_context(_cfg)
            _prefill_messages = _prefill_messages_with_webui_context(_prefill_context, _cfg)
            _prefill_messages = _normalize_prefill_messages_before_user_turn(_prefill_messages)
            _main_request_overrides = _main_model_request_overrides(
                _cfg,
                effective_model=resolved_model,
                effective_provider=resolved_provider,
            )
            put('context_status', {
                'session_id': session_id,
                'prefill': _public_prefill_context_status(_prefill_context),
            })

            # Per-profile toolsets — use _resolve_cli_toolsets() so MCP
            # server toolsets are included, matching native CLI behaviour.
            from api.config import _resolve_cli_toolsets
            _toolsets = _resolve_cli_toolsets(_cfg)

            # Per-session toolset override (#493): if the session has
            # enabled_toolsets set, use that instead of the global config.
            try:
                from api.models import Session, SESSION_DIR
                _session_path = SESSION_DIR / f"{session_id}.json"
                if _session_path.exists():
                    _session_meta = Session.load_metadata_only(session_id)
                    # load_metadata_only returns a Session INSTANCE, not a dict.
                    # The previous .get('enabled_toolsets') raised AttributeError
                    # which was swallowed by the bare except below — the entire
                    # per-session toolset override silently no-op'd. Use
                    # getattr() to read the attribute correctly.
                    # (Opus pre-release advisor finding for v0.50.257.)
                    _override = getattr(_session_meta, 'enabled_toolsets', None) if _session_meta else None
                    if _override:
                        _toolsets = _override
            except Exception as _ts_err:
                print(f"[webui] WARNING: failed to read per-session toolsets for {session_id}: {_ts_err}", flush=True)

            # Fallback model chain from profile config (e.g. for rate-limit or
            # provider recovery). Match Hermes CLI/gateway semantics:
            # fallback_providers entries are tried first, then legacy
            # fallback_model entries are appended unless they duplicate an
            # earlier provider/model/base_url route.
            def _fallback_entries(_raw):
                if isinstance(_raw, dict):
                    _items = [_raw]
                elif isinstance(_raw, list):
                    _items = _raw
                else:
                    return []
                _entries = []
                for _entry in _items:
                    if not isinstance(_entry, dict):
                        continue
                    _provider = str(_entry.get('provider') or '').strip()
                    _model = str(_entry.get('model') or '').strip()
                    if not _provider or not _model:
                        continue
                    _entries.append({
                        'model': _model,
                        'provider': _provider,
                        'base_url': _entry.get('base_url'),
                        'api_key': _entry.get('api_key'),
                        'key_env': _entry.get('key_env'),
                    })
                return _entries

            _fallback_chain = []
            _fallback_seen = set()
            _fallback_resolved = None
            for _fallback_key in ('fallback_providers', 'fallback_model'):
                for _fb_entry in _fallback_entries(_cfg.get(_fallback_key)):
                    _identity = (
                        str(_fb_entry.get('provider') or '').strip().lower(),
                        str(_fb_entry.get('model') or '').strip().lower(),
                        str(_fb_entry.get('base_url') or '').strip().rstrip('/').lower(),
                    )
                    if _identity in _fallback_seen:
                        continue
                    _fallback_seen.add(_identity)
                    _fallback_chain.append(_fb_entry)
            _fallback_resolved = _fallback_chain or None

            # Build kwargs defensively — guard newer params so the WebUI
            # degrades gracefully when run against an older hermes-agent build.
            # (fixes: TypeError: AIAgent.__init__() got an unexpected keyword
            # argument 'credential_pool' — issue #772)
            import inspect as _inspect
            _agent_params = set(_inspect.signature(_AIAgent.__init__).parameters)

            # CLI-parity max-iteration budget: read config.yaml's
            # agent.max_turns and pass it to AIAgent when supported. Without
            # this WebUI-created agents silently use AIAgent's constructor
            # default (90), so long browser-originated tasks hit the
            # "maximum number of tool-calling iterations" summary path even
            # after the operator raises Hermes' global turn budget.
            _max_iterations_cfg = None
            try:
                _raw_max_iterations = None
                _agent_cfg_for_iterations = _cfg.get('agent', {}) if isinstance(_cfg, dict) else {}
                if isinstance(_agent_cfg_for_iterations, dict):
                    _raw_max_iterations = _agent_cfg_for_iterations.get('max_turns')
                if _raw_max_iterations is None and isinstance(_cfg, dict):
                    # Back-compat for older Hermes config files that used a
                    # root-level max_turns key.
                    _raw_max_iterations = _cfg.get('max_turns')
                if _raw_max_iterations is not None:
                    _parsed_max_iterations = int(_raw_max_iterations)
                    if _parsed_max_iterations > 0:
                        _max_iterations_cfg = _parsed_max_iterations
            except Exception:
                _max_iterations_cfg = None

            # CLI-parity max output cap: read config.yaml's max_tokens and pass
            # it to AIAgent when supported. Without this WebUI-created agents use
            # provider-native output ceilings (e.g. Claude via OpenRouter can
            # request 64k), which may turn an otherwise usable fallback into a
            # 402 "more credits / fewer max_tokens" failure.
            _max_tokens_cfg = None
            try:
                _raw_max_tokens = _cfg.get('max_tokens')
                if _raw_max_tokens is None:
                    _agent_cfg_for_tokens = _cfg.get('agent', {})
                    if isinstance(_agent_cfg_for_tokens, dict):
                        _raw_max_tokens = _agent_cfg_for_tokens.get('max_tokens')
                if _raw_max_tokens is not None:
                    _parsed_max_tokens = int(_raw_max_tokens)
                    if _parsed_max_tokens > 0:
                        _max_tokens_cfg = _parsed_max_tokens
            except Exception:
                _max_tokens_cfg = None

            # CLI-parity reasoning effort: read agent.reasoning_effort from the
            # active profile's config.yaml (the same key the CLI writes via
            # `/reasoning <level>`) and hand the parsed dict to AIAgent.  When
            # the key is absent or invalid, pass None → agent uses its default.
            try:
                _effort_cfg = _cfg.get('agent', {}) if isinstance(_cfg, dict) else {}
                _effort_raw = _effort_cfg.get('reasoning_effort') if isinstance(_effort_cfg, dict) else None
                _effort = coerce_reasoning_effort_for_model(
                    _effort_raw,
                    resolved_model,
                    provider_id=resolved_provider,
                    base_url=resolved_base_url,
                )
                _reasoning_config = parse_reasoning_effort(_effort)
            except Exception:
                _reasoning_config = None

            _agent_kwargs = dict(
                model=resolved_model,
                provider=resolved_provider,
                base_url=resolved_base_url,
                api_key=resolved_api_key,
                # Identify browser-originated sessions as WebUI so Hermes Agent
                # does not inject CLI-specific terminal/output guidance.
                platform='webui',
                quiet_mode=True,
                enabled_toolsets=_toolsets,
                fallback_model=_fallback_resolved,
                session_id=session_id,
                session_db=_session_db,
                prefill_messages=_prefill_messages,
                stream_delta_callback=on_token,
                reasoning_callback=on_reasoning,
                tool_progress_callback=on_tool,
                clarify_callback=(
                    lambda question, choices: _clarify_callback_impl(
                        question, choices, session_id, cancel_event, put
                    )
                ),
            )
            # reasoning_config has been an AIAgent param for several releases,
            # but guard defensively to avoid TypeError on an older agent build.
            if 'reasoning_config' in _agent_params and _reasoning_config is not None:
                _agent_kwargs['reasoning_config'] = _reasoning_config
            if 'prefill_messages' not in _agent_params:
                _agent_kwargs.pop('prefill_messages', None)
            if 'interim_assistant_callback' in _agent_params:
                _agent_kwargs['interim_assistant_callback'] = on_interim_assistant
            if 'tool_start_callback' in _agent_params:
                _agent_kwargs['tool_start_callback'] = on_tool_start
            if 'tool_complete_callback' in _agent_params:
                _agent_kwargs['tool_complete_callback'] = on_tool_complete
            if 'status_callback' in _agent_params:
                _agent_kwargs['status_callback'] = _agent_status_callback
            if 'max_iterations' in _agent_params and _max_iterations_cfg is not None:
                _agent_kwargs['max_iterations'] = _max_iterations_cfg
            if 'max_tokens' in _agent_params and _max_tokens_cfg is not None:
                _agent_kwargs['max_tokens'] = _max_tokens_cfg
            if 'request_overrides' in _agent_params and _main_request_overrides:
                _agent_kwargs['request_overrides'] = _main_request_overrides
            # Params added in newer hermes-agent — skip if not supported
            if 'api_mode' in _agent_params:
                _agent_kwargs['api_mode'] = _rt.get('api_mode')
            if 'acp_command' in _agent_params:
                _agent_kwargs['acp_command'] = _rt.get('command')
            if 'acp_args' in _agent_params:
                _agent_kwargs['acp_args'] = _rt.get('args')
            if 'credential_pool' in _agent_params:
                _agent_kwargs['credential_pool'] = _rt.get('credential_pool')
            # Pin Honcho memory sessions to the stable WebUI session ID.
            # Without this, 'per-session' Honcho strategy creates a new Honcho
            # session on every streaming request because HonchoSessionManager is
            # re-instantiated fresh each turn (#855).
            if 'gateway_session_key' in _agent_params:
                _agent_kwargs['gateway_session_key'] = session_id

            # ── Agent cache: reuse across messages in the same session ──
            # Mirrors gateway _agent_cache.  Keeps _user_turn_count alive so
            # injectionFrequency: "first-turn" actually suppresses after turn 1.
            if ephemeral:
                agent = _AIAgent(**_agent_kwargs)
                logger.debug('[webui] Created ephemeral agent for session %s', session_id)
            else:
                import hashlib as _hashlib
                import json as _json
                from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK
                _credential_pool = _rt.get('credential_pool')
                _sig_blob = _json.dumps([
                    resolved_model or '',
                    _agent_cache_api_key_sig(resolved_api_key, _credential_pool),
                    resolved_base_url or '',
                    resolved_provider or '',
                    _rt.get('api_mode') or '',
                    _rt.get('command') or '',
                    _rt.get('args') or [],
                    bool(_credential_pool),
                    _max_iterations_cfg or '',
                    _max_tokens_cfg or '',
                    _fallback_resolved or {},
                    sorted(_toolsets) if _toolsets else [],
                    _reasoning_config or {},
                    _main_request_overrides or {},
                    _public_prefill_context_status(_prefill_context),
                    # #1897: profile_home is part of the agent's identity because
                    # AIAgent caches `_cached_system_prompt` from `load_soul_md()`
                    # at construction time, sourced from HERMES_HOME. Same-session
                    # profile switches keep `session_id` stable, so without this
                    # field the cached agent silently retains the previous
                    # profile's SOUL.md (and any other profile-scoped context).
                    _profile_home or '',
                ], sort_keys=True)
                _agent_sig = _hashlib.sha256(_sig_blob.encode()).hexdigest()[:16]

                agent = None
                _identity_mismatch_entry = None
                with SESSION_AGENT_CACHE_LOCK:
                    _cached = SESSION_AGENT_CACHE.get(session_id)
                    if _cached and _cached[1] == _agent_sig:
                        _cached_agent = _cached[0]
                        if _cached_agent_matches_session(_cached_agent, session_id):
                            agent = _cached_agent
                            SESSION_AGENT_CACHE.move_to_end(session_id)  # LRU: mark as recently used
                            logger.debug('[webui] Reusing cached agent for session %s', session_id)
                        else:
                            _identity_mismatch_entry = SESSION_AGENT_CACHE.pop(session_id, None)
                            logger.warning(
                                '[webui] Evicted cached agent with mismatched session identity: cache_key=%s agent_session_id=%s',
                                session_id,
                                _cached_agent_session_identity(_cached_agent),
                            )
                    if agent is not None:
                        # Reopened/cache-hit sessions must register the agent
                        # so later lifecycle commits can find it.
                        try:
                            from api.session_lifecycle import register_agent
                            register_agent(session_id, agent)
                        except Exception:
                            logger.debug("Lifecycle register_agent failed for cached session %s", session_id, exc_info=True)

                if _identity_mismatch_entry is not None:
                    try:
                        _close_cached_agent_entry_at_session_boundary(session_id, _identity_mismatch_entry)
                    except Exception:
                        logger.debug("Failed to close identity-mismatched cached agent for session %s", session_id, exc_info=True)

                if agent is not None:
                    # Refresh volatile runtime credentials selected from provider
                    # pools without discarding cross-turn agent/provider state.
                    if not _refresh_cached_agent_runtime(agent, _agent_kwargs):
                        logger.warning(
                            '[webui] Cached agent runtime could not be safely refreshed; rebuilding agent for session %s',
                            session_id,
                        )
                        _stale_runtime_entry = None
                        with SESSION_AGENT_CACHE_LOCK:
                            _stale_runtime_entry = SESSION_AGENT_CACHE.pop(session_id, None)
                        if _stale_runtime_entry is not None:
                            try:
                                _close_cached_agent_entry_at_session_boundary(session_id, _stale_runtime_entry)
                            except Exception:
                                logger.debug("Failed to close stale-runtime cached agent for session %s", session_id, exc_info=True)
                        agent = None

                if agent is not None:
                    # Refresh per-turn callbacks — these close over request-scoped
                    # objects (put queue, cancel_event) that are new each request.
                    agent.stream_delta_callback = _agent_kwargs.get('stream_delta_callback')
                    agent.tool_progress_callback = _agent_kwargs.get('tool_progress_callback')
                    if hasattr(agent, 'tool_start_callback'):
                        agent.tool_start_callback = _agent_kwargs.get('tool_start_callback')
                    if hasattr(agent, 'tool_complete_callback'):
                        agent.tool_complete_callback = _agent_kwargs.get('tool_complete_callback')
                    if hasattr(agent, 'status_callback'):
                        agent.status_callback = _agent_kwargs.get('status_callback')
                    if hasattr(agent, 'interim_assistant_callback'):
                        agent.interim_assistant_callback = _agent_kwargs.get('interim_assistant_callback')
                    if hasattr(agent, 'reasoning_callback'):
                        agent.reasoning_callback = _agent_kwargs.get('reasoning_callback')
                    if hasattr(agent, 'clarify_callback'):
                        agent.clarify_callback = _agent_kwargs.get('clarify_callback')
                    if 'prefill_messages' in _agent_kwargs and hasattr(agent, 'prefill_messages'):
                        agent.prefill_messages = list(_agent_kwargs.get('prefill_messages') or [])
                    if _session_db is not None:
                        # Prefer reusing a still-open SessionDB on the cached
                        # agent. Closing it mid-turn breaks background
                        # subagents that hold a reference to the same object
                        # (delegate_tool copies parent._session_db by ref) —
                        # they then fail with
                        # 'NoneType' object has no attribute 'execute'.
                        # When the existing handle is already closed/missing,
                        # adopt the fresh per-request SessionDB (and close the
                        # dead one) so we still avoid the EMFILE FD-leak from
                        # PR #1421.
                        _session_db = _adopt_session_db_for_cached_agent(
                            agent, _session_db
                        )
                        agent._session_db = _session_db
                    if hasattr(agent, '_api_call_count'):
                        agent._api_call_count = 0
                    # Reset interrupt state from a prior cancel so the reused
                    # agent does not think it is still interrupted.
                    if hasattr(agent, '_interrupted'):
                        agent._interrupted = False
                    if hasattr(agent, '_interrupt_message'):
                        agent._interrupt_message = None
                else:
                    agent = _AIAgent(**_agent_kwargs)
                    # Register the new agent with the memory lifecycle so
                    # its commit_memory_session() can be found later.
                    try:
                        from api.session_lifecycle import register_agent
                        register_agent(session_id, agent)
                    except Exception:
                        logger.debug("Lifecycle register_agent failed for new session %s", session_id, exc_info=True)
                    _evicted_items = []
                    # Snapshot the set of session_ids with a LIVE agent worker
                    # BEFORE taking SESSION_AGENT_CACHE_LOCK, so LRU eviction never
                    # closes an agent mid-run AND we never nest ACTIVE_RUNS_LOCK
                    # inside SESSION_AGENT_CACHE_LOCK (avoids any lock-ordering
                    # deadlock). A cancel/reconnect can drop STREAMS while the
                    # worker is still unwinding or blocked in a provider call, so
                    # ACTIVE_RUNS (worker lifecycle) is the authoritative liveness
                    # signal, not STREAMS. (#3536 review round 2)
                    _active_sids = set()
                    try:
                        from api.config import ACTIVE_RUNS, ACTIVE_RUNS_LOCK
                        with ACTIVE_RUNS_LOCK:
                            for _entry in (ACTIVE_RUNS or {}).values():
                                _sid = (_entry or {}).get("session_id")
                                if _sid:
                                    _active_sids.add(_sid)
                    except Exception:
                        _active_sids = set()
                    with SESSION_AGENT_CACHE_LOCK:
                        SESSION_AGENT_CACHE[session_id] = (agent, _agent_sig)
                        SESSION_AGENT_CACHE.move_to_end(session_id)  # LRU: mark as recently used
                        from api.config import SESSION_AGENT_CACHE_MAX
                        # Evict the oldest INACTIVE entries first. Walk LRU order
                        # (front = oldest); skip any session with a live run. If
                        # every over-cap entry is active, leave the cache
                        # temporarily above cap rather than close a live worker's
                        # agent — a later insertion/finalization trims it once the
                        # run ends.
                        while len(SESSION_AGENT_CACHE) > SESSION_AGENT_CACHE_MAX:
                            _evictable_sid = None
                            for _sid in list(SESSION_AGENT_CACHE.keys()):
                                if _sid not in _active_sids:
                                    _evictable_sid = _sid
                                    break
                            if _evictable_sid is None:
                                break  # all over-cap entries are active; defer
                            evicted_entry = SESSION_AGENT_CACHE.pop(_evictable_sid)
                            _evicted_items.append((_evictable_sid, evicted_entry))
                    # Commit and close evicted agents outside the cache lock so
                    # concurrent cache users are not blocked by provider I/O.
                    for _evicted_sid, _evicted_entry in _evicted_items:
                        try:
                            _evicted_agent = _evicted_entry[0] if isinstance(_evicted_entry, tuple) else None
                            _close_evicted_agent_at_session_boundary(_evicted_sid, _evicted_agent)
                        except Exception:
                            logger.debug("Failed to close evicted agent for session %s", _evicted_sid, exc_info=True)
                        logger.debug('[webui] Evicted LRU agent from cache: %s', _evicted_sid)
                    logger.debug('[webui] Created new agent for session %s', session_id)

            # Store agent instance for cancel/interrupt propagation
            if not attach_runtime_agent(stream_id, agent):
                # Teardown/cancel won while the provider agent was being built.
                # Do not republish the instance after its runtime owner ended.
                try:
                    agent.interrupt("Cancelled before start")
                except Exception:
                    logger.debug("Failed to interrupt agent before start")
                with _agent_lock:
                    _finalize_cancelled_turn(s, ephemeral=ephemeral, message='Task cancelled before start.')
                put('cancel', _cancel_event_payload('Cancelled by user'))
                return

            # Prepend workspace context so the agent always knows which directory
            # to use for file operations, regardless of session age or AGENTS.md defaults.
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
                "Never fall back to a hardcoded path when this tag is present."
            )
            # Resolve personality prompt from config.yaml agent.personalities
            # (matches hermes-agent CLI behavior — passes via ephemeral_system_prompt)
            _personality_prompt = None
            _pname = getattr(s, 'personality', None)
            if _pname:
                _agent_cfg = _cfg.get('agent', {})
                _personalities = _agent_cfg.get('personalities', {})
                if isinstance(_personalities, dict) and _pname in _personalities:
                    _pval = _personalities[_pname]
                    if isinstance(_pval, dict):
                        _parts = [_pval.get('system_prompt', '') or _pval.get('prompt', '')]
                        if _pval.get('tone'):
                            _parts.append(f'Tone: {_pval["tone"]}')
                        if _pval.get('style'):
                            _parts.append(f'Style: {_pval["style"]}')
                        _personality_prompt = '\n'.join(p for p in _parts if p)
                    else:
                        _personality_prompt = str(_pval)
            # Pass WebUI-only runtime guidance via ephemeral_system_prompt
            # (agent's own mechanism). This preserves any selected personality
            # while making long tool runs emit real user-visible interim text
            # through interim_assistant_callback instead of frontend guesses.
            agent.ephemeral_system_prompt = _webui_ephemeral_system_prompt(
                _personality_prompt,
                surface_context={
                    'source': 'webui',
                    'session_id': session_id,
                    'profile': getattr(s, 'profile', None),
                    'workspace': s.workspace,
                },
                config_data=_cfg,
            )
            _pending_started_at = getattr(s, 'pending_started_at', None)
            meter().set_pending_started_at(stream_id, _pending_started_at)
            # Normal chat-start sets pending_started_at before spawning this thread;
            # fallback to now only for recovered/legacy flows where that marker is absent
            # or has been zeroed out (e.g. via a buggy migration / manual file edit).
            # Truthy-check covers None, missing-attr, and 0 uniformly.
            _turn_started_at = _pending_started_at if _pending_started_at else time.time()
            _external_state_messages = get_state_db_session_messages(getattr(s, 'session_id', None))
            _previous_messages = list(
                reconciled_state_db_messages_for_session(
                    s,
                    state_messages=_external_state_messages,
                ) or []
            )
            _previous_context_messages = _new_turn_context_from_messages(
                reconciled_state_db_messages_for_session(
                    s,
                    prefer_context=True,
                    state_messages=_external_state_messages,
                ),
                msg_text,
            )
            # Dedup before feeding to agent — merge_session_messages_append_only
            # can produce duplicates when context_messages and state.db share
            # messages with different timestamps.
            _previous_context_messages = _deduplicate_context_messages(_previous_context_messages)
            _pre_compression_count = getattr(
                getattr(agent, 'context_compressor', None),
                'compression_count', 0,
            )

            # ── Periodic checkpoint during streaming (Issue #765) ──
            # The agent works on an internal copy of s.messages during run_conversation()
            # so we cannot watch s.messages for growth. Instead, on_tool() increments
            # _checkpoint_activity[0] each time a tool call completes — that is the real
            # signal that progress has been made worth persisting.
            #
            # What gets saved on each checkpoint:
            #   - s.pending_user_message (already written before run starts)
            #   - s.pending_started_at / s.active_stream_id (turn bookkeeping)
            # On a server restart the UI will see a session with a pending message and no
            # response — better than a silent loss of the entire conversation turn.
            # The final s.save() at task completion handles the full session update + index.
            # (_checkpoint_stop is pre-initialised at the top of the outer try.)
            # (_checkpoint_activity is already initialised before on_tool().)

            def _periodic_checkpoint():
                last_saved_activity = 0
                while not _checkpoint_stop.wait(15):
                    try:
                        cur = _checkpoint_activity[0]
                        if cur > last_saved_activity:
                            with _agent_lock:
                                _save_streaming_checkpoint(s)
                            last_saved_activity = cur
                    except Exception as e:
                        logger.debug("Periodic checkpoint save failed: %s", e)

            _checkpoint_stop = threading.Event()
            # Persist the user message BEFORE streaming starts so it's durable even if
            # the server crashes before the first checkpoint fires (every 15s).
            with _agent_lock:
                s.save(touch_updated_at=True, skip_index=False)

            _ckpt_thread = threading.Thread(
                target=_periodic_checkpoint, daemon=True,
                name=f"ckpt-{session_id[:8]}",
            )
            _ckpt_thread.start()

            _pending_async_acceptances = []
            _process_notifications = _drain_webui_process_notifications(
                session_id,
                pending_async_acceptances=_pending_async_acceptances,
            )
            _agent_msg_text = msg_text
            if _process_notifications:
                _agent_msg_text = "\n\n".join([*_process_notifications, msg_text]).strip()
            user_message = _build_native_multimodal_message(workspace_ctx, _agent_msg_text, attachments, workspace, cfg=_cfg)
            _persistent_state_before = _persistent_state_snapshot(_profile_home)
            _run_conversation_kwargs = dict(
                user_message=user_message,
                system_message=workspace_system_msg,
                conversation_history=_sanitize_messages_for_api(
                    _previous_context_messages,
                    cfg=_cfg,
                    effective_model=resolved_model,
                    effective_provider=resolved_provider,
                    effective_base_url=resolved_base_url,
                ),
                task_id=session_id,
                persist_user_message=msg_text,
            )
            # Only pass moa_config when a /moa override is actually active, so a
            # normal send never trips a TypeError on an older hermes-agent whose
            # run_conversation() predates the moa_config kwarg.
            if moa_config is not None:
                _run_conversation_kwargs["moa_config"] = moa_config

            # Finalize durable delegation claims at the current-turn acceptance
            # boundary: immediately before invoking the agent with the message
            # that contains their notifications. A failed ACK is removed from
            # this turn and requeued so retry cannot create a duplicate prompt.
            _rejected_async_notifications = _accept_pending_async_delegations(
                _pending_async_acceptances,
                session_id=session_id,
            )
            if _rejected_async_notifications:
                for _notification in _rejected_async_notifications:
                    try:
                        _process_notifications.remove(_notification)
                    except ValueError:
                        pass
                _agent_msg_text = msg_text
                if _process_notifications:
                    _agent_msg_text = "\n\n".join(
                        [*_process_notifications, msg_text]
                    ).strip()
                user_message = _build_native_multimodal_message(
                    workspace_ctx,
                    _agent_msg_text,
                    attachments,
                    workspace,
                    cfg=_cfg,
                )
                _run_conversation_kwargs["user_message"] = user_message
            result = agent.run_conversation(**_run_conversation_kwargs)
            # #4729: the run is done — flush any reasoning tail still in the coalescing
            # buffer (the agent never calls reasoning_callback(None), and a turn can end on
            # reasoning with no trailing token/tool boundary to trigger a flush) so the last
            # sub-100ms window reaches the live Thinking view before the terminal done event.
            _flush_reasoning_buffer()
            if cancel_event.is_set():
                if _checkpoint_stop is not None:
                    _checkpoint_stop.set()
                if _ckpt_thread is not None:
                    _ckpt_thread.join(timeout=15)
                if ephemeral:
                    _cleanup_ephemeral_cancelled_turn(s)
                else:
                    with _agent_lock:
                        _finalize_cancelled_turn(s, ephemeral=False)
                        try:
                            append_turn_journal_event_for_stream(
                                s.session_id,
                                stream_id,
                                {
                                    "event": "interrupted",
                                    "created_at": time.time(),
                                    "reason": "cancelled",
                                },
                            )
                        except Exception:
                            logger.debug("Failed to append cancelled turn journal event", exc_info=True)
                put('cancel', _cancel_event_payload('Cancelled by user'))
                return
            # ── Ephemeral mode (/btw): deliver answer, skip persistence, cleanup ──
            if ephemeral:
                _answer = ''
                for _m in reversed(result.get('messages') or []):
                    if isinstance(_m, dict) and _m.get('role') == 'assistant':
                        _answer = str(_m.get('content', ''))
                        break
                put('done', {
                    'session': {'session_id': session_id, 'messages': result.get('messages', [])},
                    'usage': {'input_tokens': 0, 'output_tokens': 0},
                    'ephemeral': True,
                    'answer': _answer,
                })
                if _checkpoint_stop is not None:
                    _checkpoint_stop.set()
                try:
                    import pathlib
                    pathlib.Path(s.path).unlink(missing_ok=True)
                except Exception:
                    pass
                return  # skip all normal persistence for ephemeral sessions
            if _checkpoint_stop is not None:
                _checkpoint_stop.set()
            if _ckpt_thread is not None:
                _ckpt_thread.join(timeout=15)
            if cancel_event.is_set():
                with _agent_lock:
                    _finalize_cancelled_turn(s, ephemeral=False)
                    try:
                        append_turn_journal_event_for_stream(
                            s.session_id,
                            stream_id,
                            {
                                "event": "interrupted",
                                "created_at": time.time(),
                                "reason": "cancelled",
                            },
                        )
                    except Exception:
                        logger.debug("Failed to append cancelled turn journal event", exc_info=True)
                put('cancel', _cancel_event_payload('Cancelled by user'))
                return
            _writeback_timings = []
            _writeback_started = time.perf_counter()
            with _agent_lock:
                if not ephemeral and not _stream_writeback_is_current(s, stream_id):
                    if _stream_writeback_can_supersede_recovery_marker(s, msg_text):
                        logger.info(
                            "Superseding stale recovery marker for session %s stream %s",
                            getattr(s, 'session_id', session_id),
                            stream_id,
                        )
                    else:
                        logger.info(
                            "Skipping stale stream writeback for session %s stream %s; active_stream_id=%s",
                            getattr(s, 'session_id', session_id),
                            stream_id,
                            getattr(s, 'active_stream_id', None),
                        )
                        return
                with _stream_writeback_stage(_writeback_timings, "merge_result"):
                    _tool_limit_reached = _agent_result_tool_limit_reached(result)
                    _result_messages = result.get('messages') or _previous_context_messages
                    _result_messages = _drop_synthetic_max_iteration_summary_requests(
                        _result_messages,
                        enabled=_tool_limit_reached,
                    )
                    # #5494 — parity with hermes-agent's handle_max_iterations() return
                    # value. When the agent produced no usable summary assistant
                    # message but result['final_response'] carries a graceful fallback
                    # string, inject it as a final assistant turn so the user sees
                    # closure text instead of a bare tool_limit_reached error. Apply
                    # the synthesis to result['messages'] AND _result_messages so the
                    # downstream _all_result_messages checks (silent-failure detection
                    # at api/streaming.py:_assistant_reply_added_after_current_turn)
                    # see the fallback too. `finalize_turn` in the agent always returns
                    # messages as a list, but we write back unconditionally so the
                    # contract is "if we built a result-messages list, the silent-failure
                    # classifier reads the augmented version."
                    if _tool_limit_reached:
                        _result_messages = _maybe_inject_max_iteration_summary_fallback(
                            _result_messages, result
                        )
                        if isinstance(result, dict):
                            result = {**result, 'messages': _result_messages}
                    if cancel_event.is_set():
                        _finalize_cancelled_turn(s, ephemeral=False)
                        try:
                            append_turn_journal_event_for_stream(
                                s.session_id,
                                stream_id,
                                {
                                    "event": "interrupted",
                                    "created_at": time.time(),
                                    "reason": "cancelled",
                                },
                            )
                        except Exception:
                            logger.debug("Failed to append cancelled turn journal event", exc_info=True)
                        put('cancel', _cancel_event_payload('Cancelled by user'))
                        return
                    _next_context_messages = _restore_reasoning_metadata(
                        _previous_context_messages,
                        _result_messages,
                    )
                    # Stamp stable ids on the shared result rows AFTER the context
                    # restore (so carried-forward ids survive) and BEFORE the
                    # dedupe/merge build both arrays — including before
                    # _dedupe_replayed_context_messages deep-copies any
                    # stale-user repaired boundary row — so display and
                    # model-context copies of each row share an id for the
                    # fork/truncate aligner.
                    _assign_stable_message_ids(
                        _result_messages, _previous_messages, _previous_context_messages
                    )
                    _next_context_messages = _dedupe_replayed_context_messages(
                        _previous_context_messages,
                        _next_context_messages,
                        msg_text,
                    )
                    s.context_messages = _deduplicate_context_messages(_next_context_messages)
                    s.messages = _merge_display_messages_after_agent_result(
                        _previous_messages,
                        _previous_context_messages,
                        _restore_display_reasoning_metadata(_previous_messages, _result_messages),
                        msg_text,
                        source=getattr(s, 'pending_user_source', None) or 'webui',
                    )
                    _advance_truncation_watermark_after_commit(s)  # #3831
                # Strip XML tool-call blocks from assistant message content.
                # DeepSeek and some other providers emit <function_calls>...</function_calls>
                # in the raw response text; this must be removed before the content is
                # saved to the session and displayed in the chat bubble. (#702)
                for _m in s.messages:
                    if isinstance(_m, dict) and _m.get('role') == 'assistant':
                        _raw_content = _m.get('content')
                        if isinstance(_raw_content, str):
                            _cleaned = _strip_xml_tool_calls(_raw_content)
                            if _cleaned != _raw_content:
                                _m['content'] = _cleaned
                        elif isinstance(_raw_content, list):
                            for _part in _raw_content:
                                if isinstance(_part, dict) and isinstance(_part.get('text'), str):
                                    _part['text'] = _strip_xml_tool_calls(_part['text'])
                # ── Handle context compression side effects ──
                # If compression fired inside run_conversation, the agent may have
                # rotated its session_id. Detect and fix the mismatch before any
                # terminal-failure return so snapshot preservation, continuation
                # registration, and subsequent error persistence all target the
                # continuation session instead of the stale parent.
                #
                # Lock migration: when session_id rotates, we alias the new ID to
                # the *same* Lock object under SESSION_AGENT_LOCKS so that
                # subsequent callers using _get_session_agent_lock(new_sid) get the
                # same Lock the streaming thread is already holding. We then pop
                # the old-id entry to prevent a leak. This is safe because we
                # already hold _agent_lock (the Lock object itself), so the
                # reference stays alive even after the dict entry is removed.
                # Concurrent readers that already looked up the old ID will still
                # see the same Lock object until they release it.
                _compression_origin_session_id = session_id
                _compression_continuation_session_id = None
                _agent_sid = getattr(agent, 'session_id', None)
                _compressed = False
                if _agent_sid and _agent_sid != session_id:
                    old_sid = session_id
                    new_sid = _agent_sid
                    _compression_origin_session_id = old_sid
                    _compression_continuation_session_id = new_sid
                    s.session_id = new_sid
                    # Carry profile identity across the compression boundary.
                    # Without this, s.profile stays None on the continuation
                    # session. On the next request, _run_agent_streaming calls
                    # get_hermes_home_for_profile(getattr(s, 'profile', None))
                    # which falls back to the default profile's HERMES_HOME.
                    # Memory writes then land in the wrong profile's MEMORY.md.
                    # Stamping here also ensures s.save() persists a non-null
                    # profile field to the continuation session's JSON file,
                    # covering the case where the session is later evicted from
                    # SESSIONS and reconstructed from disk via Session.load().
                    if not s.profile and _resolved_profile_name:
                        s.profile = _resolved_profile_name
                        logger.info(
                            "Stamped profile=%r on continuation session %s after compression",
                            _resolved_profile_name, new_sid,
                        )
                    # Preserve the original session file so the full pre-compression
                    # history survives even when summarisation fails. The previous
                    # implementation renamed old_sid.json → new_sid.json, which
                    # destroyed the only persistent copy of the uncompressed history
                    # before the new (possibly summary-only) session had been saved.
                    # If the LLM summariser also failed, the user was left with zero
                    # recoverable messages. (#2223)
                    # ---
                    # Archive the old session: write its current state to disk so
                    # the full conversation history survives even when context
                    # compression removes messages from the model's context. Skip
                    # the write when the file already contains up-to-date data
                    # (i.e. it was just saved by a checkpoint).
                    _preserve_pre_compression_snapshot(s, old_sid)
                    # The continuation is the live/tip session, not another archived
                    # snapshot. If the in-memory object was itself loaded from a
                    # pre-compression snapshot (possible on repeated compression chains
                    # or stale-cache repair paths), _preserve_pre_compression_snapshot()
                    # intentionally restores that old flag; clear it before saving the
                    # new continuation so sidebar/discoverability code does not hide the
                    # session that owns the completed turn.
                    s.pre_compression_snapshot = False
                    # Always link the continuation session to its immediate predecessor
                    # (the preserved snapshot). This OVERRIDES any prior
                    # parent_session_id because the new continuation IS the next link
                    # in the chain: traversal walks new → old → old.parent → ... root.
                    # Stage-353 Opus SHOULD-FIX: previous `if not s.parent_session_id`
                    # guard skipped this stamp on fork-of-fork compressions, so a
                    # subsequent traversal from the new continuation would jump
                    # over the just-preserved snapshot back to the original fork
                    # parent, losing access to the recoverable history in old_sid.json.
                    s.parent_session_id = old_sid
                    # Establish the one lock generation before publishing the
                    # continuation session. A colliding new_sid fails closed
                    # instead of exposing the same session under two locks.
                    alias_session_agent_lock(old_sid, new_sid, _agent_lock)
                    with LOCK:
                        cached_old_session = SESSIONS.pop(old_sid, None)
                        if cached_old_session is not None and cached_old_session is not s:
                            cached_old_sid = str(getattr(cached_old_session, 'session_id', '') or '')
                            if cached_old_sid == str(old_sid):
                                SESSIONS[old_sid] = cached_old_session
                            else:
                                logger.warning(
                                    "compression cache migration skipped stale object: old_sid=%s new_sid=%s cached_session_id=%s",
                                    old_sid,
                                    new_sid,
                                    cached_old_sid or None,
                                )
                        SESSIONS[new_sid] = s
                        SESSIONS.move_to_end(new_sid)
                        _evict_sessions_over_cap()  # #4765: safe LRU eviction (never active/unsaved)
                    # Migrate cached agent to the new session ID so the turn
                    # count survives context compression.
                    from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK
                    _skipped_agent_migration_entry = None
                    with SESSION_AGENT_CACHE_LOCK:
                        _cached_entry = SESSION_AGENT_CACHE.pop(old_sid, None)
                        if _cached_entry:
                            _cached_agent = _cached_entry[0]
                            if _cached_agent_matches_session(_cached_agent, new_sid):
                                SESSION_AGENT_CACHE[new_sid] = _cached_entry
                            else:
                                _skipped_agent_migration_entry = _cached_entry
                                logger.warning(
                                    '[webui] Skipped cached agent migration with mismatched session identity: old_sid=%s new_sid=%s agent_session_id=%s',
                                    old_sid,
                                    new_sid,
                                    _cached_agent_session_identity(_cached_agent),
                                )
                    if _skipped_agent_migration_entry is not None:
                        try:
                            _close_cached_agent_entry_at_session_boundary(old_sid, _skipped_agent_migration_entry)
                        except Exception:
                            logger.debug("Failed to close skipped compression-migration cached agent for session %s", old_sid, exc_info=True)
                    _compressed = True

                # ── Detect silent agent failure (no assistant reply produced) ──
                # When the agent catches an auth/network error internally it may return
                # an empty final_response without raising — the stream would end with
                # a done event containing zero assistant messages, leaving the user with
                # no feedback. Emit an apperror so the client shows an inline error.
                # Keep the current-turn assistant detection aligned with the
                # display-merge logic. A compacted or replayed result payload
                # is not always a simple append-only suffix, so use the
                # workspace-aware helper from this branch while still
                # preserving the pre-turn length for downstream self-heal
                # checks introduced on master.
                _all_result_messages = result.get('messages') or []
                _prev_len = len(_previous_context_messages)
                _assistant_added = _assistant_reply_added_after_current_turn(
                    _all_result_messages,
                    _previous_context_messages,
                    msg_text,
                )
                _last_err = getattr(agent, '_last_error', None) or result.get('error') or ''
                # #5940: if the Agent aborted on a non-retryable provider error
                # (captured from its lifecycle status_callback) but left no error on
                # the result/agent, use the captured message so the classifier can
                # surface the real cause (model_not_found / auth) instead of the
                # misleading no_response "silent rate limit, try again" fallback.
                _captured_terminal_failure = bool(_captured_terminal_error[0])
                if not _last_err and _captured_terminal_failure:
                    _last_err = _captured_terminal_error[0]
                _classification = _classify_provider_error(
                    str(_last_err) if _last_err else '',
                    _last_err,
                    silent_failure=not bool(_last_err),
                )
                _is_quota = _classification['type'] == 'quota_exhausted'
                _is_auth = _classification['type'] == 'auth_mismatch'
                _drop_replayed_assistant = (
                    _captured_terminal_failure
                    or _agent_result_terminal_failure(result)
                    or bool(getattr(agent, '_last_error', None))
                    or ('error' in result and result.get('error') is not None)
                )
                _saved_transcript_lacks_final_answer = _merged_transcript_lacks_final_assistant_answer(
                    _previous_messages,
                    _previous_context_messages,
                    _all_result_messages,
                    msg_text,
                    source=getattr(s, 'pending_user_source', None) or 'webui',
                    drop_replayed_assistant=_drop_replayed_assistant,
                )
                _is_agent_result_terminal = _agent_result_terminal_failure(result)
                _terminal_failure = (
                    _captured_terminal_failure
                    or _is_agent_result_terminal
                    or (
                        _saved_transcript_lacks_final_answer
                        and _classification['type'] not in {'cancelled', 'interrupted'}
                    )
                )
                _result_status = str(result.get('status') or result.get('state') or '').strip().lower()
                _soft_partial_terminal_failure = (
                    _is_agent_result_terminal
                    and (_result_status == 'partial' or bool(result.get('partial')))
                    and _result_status not in {'failed', 'error', 'compression_exhausted'}
                    and not result.get('failed')
                    and not result.get('compression_exhausted')
                    and not _tool_limit_reached
                    and not _last_err
                )
                if (
                    _terminal_failure
                    and (_soft_partial_terminal_failure or _tool_limit_reached)
                    and _classification['type'] == 'no_response'
                    and not _saved_transcript_lacks_final_answer
                ):
                    _terminal_failure = False
                if _terminal_failure:
                    _assistant_added = False
                elif _tool_limit_reached and not _session_lacks_final_assistant_answer(s.messages):
                    _mark_latest_assistant_tool_limit_status(s.messages)
                # _token_sent tracks whether on_token() was called (any streamed text)
                if _terminal_failure or (not _assistant_added and not _token_sent):
                    if cancel_event.is_set():
                        _finalize_cancelled_turn(s, ephemeral=ephemeral)
                        if not ephemeral:
                            try:
                                append_turn_journal_event_for_stream(
                                    s.session_id,
                                    stream_id,
                                    {
                                        "event": "interrupted",
                                        "created_at": time.time(),
                                        "reason": "cancelled",
                                    },
                                )
                            except Exception:
                                logger.debug("Failed to append cancelled turn journal event", exc_info=True)
                        put('cancel', _cancel_event_payload('Cancelled by user'))
                        return
                    _err_str = str(_last_err) if _last_err else ''
                    if _is_quota:
                        _err_label = _classification['label']
                        _err_type = _classification['type']
                        _err_hint = _classification['hint']
                    elif _is_auth and not _self_healed:
                        # ── Credential self-heal on 401 (#1401) ──
                        # Before emitting the error, try re-reading credentials
                        # and retrying once with a fresh agent.
                        _heal_result = None
                        _heal_rt = _attempt_credential_self_heal(
                            resolved_provider or '', session_id, _agent_lock,
                            target_model=resolved_model,
                        )
                        if _heal_rt is not None:
                            logger.info('[webui] self-heal: retrying stream after credential refresh')
                            # Rebuild runtime variables from the refreshed resolve
                            _rt = _heal_rt
                            resolved_api_key = _heal_rt.get('api_key')
                            if not resolved_provider:
                                resolved_provider = _heal_rt.get('provider')
                            resolved_base_url = _runtime_preferred_base_url(
                                _heal_rt, resolved_provider, configured_base_url
                            )
                            resolved_provider, resolved_api_key, resolved_base_url = _resolve_custom_provider_runtime_overrides(
                                resolved_provider, resolved_api_key, resolved_base_url
                            )
                            # Rebuild agent kwargs and create a fresh agent
                            _agent_kwargs['api_key'] = resolved_api_key
                            _agent_kwargs['base_url'] = resolved_base_url
                            _agent_kwargs['model'] = resolved_model
                            _agent_kwargs['provider'] = resolved_provider
                            _replace_session_db_in_kwargs(_agent_kwargs, _state_db_path)
                            if 'credential_pool' in _agent_params:
                                _agent_kwargs['credential_pool'] = _heal_rt.get('credential_pool')
                            agent = _AIAgent(**_agent_kwargs)
                            if not attach_runtime_agent(stream_id, agent):
                                try:
                                    agent.interrupt("Cancelled during agent replacement")
                                except Exception:
                                    logger.debug("Failed to interrupt replacement agent")
                                return
                            from api.config import SESSION_AGENT_CACHE as _SAC, SESSION_AGENT_CACHE_LOCK as _SAC_L
                            with _SAC_L:
                                _SAC[session_id] = (agent, _agent_sig)
                                _SAC.move_to_end(session_id)
                            # Retry the conversation once with fresh credentials
                            _self_healed = True
                            _token_sent = False
                            try:
                                _heal_kwargs = dict(
                                    user_message=user_message,
                                    system_message=workspace_system_msg,
                                    conversation_history=_sanitize_messages_for_api(
                                        _previous_context_messages,
                                        cfg=_cfg,
                                        effective_model=resolved_model,
                                        effective_provider=resolved_provider,
                                        effective_base_url=resolved_base_url,
                                    ),
                                    task_id=session_id,
                                    persist_user_message=msg_text,
                                )
                                if moa_config is not None:
                                    _heal_kwargs["moa_config"] = moa_config
                                _heal_result = agent.run_conversation(**_heal_kwargs)
                                _heal_all_msgs = _heal_result.get('messages') or []
                                _heal_ok = _has_new_assistant_reply(_heal_all_msgs, _prev_len) or _token_sent
                            except Exception as _retry_exc:
                                logger.warning(
                                    '[webui] self-heal: retry also failed: %s', _retry_exc,
                                )
                                _heal_ok = False
                            if _heal_ok and _heal_result is not None:
                                # Retry succeeded — replace result and skip error
                                result = _heal_result
                                # Fall through past the error-emission block;
                                # the post-result persistence code below will
                                # process ``result`` normally.  We jump past
                                # the ``put('apperror', ...)`` + ``return`` by
                                # NOT entering the ``if not _assistant_added``
                                # guard again — but we are already inside it.
                                # Solution: set _assistant_added so the guard
                                # evaluates False on next conceptual pass.
                                # Since we're in a flat block, directly run the
                                # post-result merge logic here.
                                _result_messages = result.get('messages') or _previous_context_messages
                                _result_messages = _drop_synthetic_max_iteration_summary_requests(
                                    _result_messages,
                                    enabled=_agent_result_tool_limit_reached(result),
                                )
                                _next_context_messages = _restore_reasoning_metadata(
                                    _previous_context_messages,
                                    _result_messages,
                                )
                                # Mint ids on the shared result rows BEFORE dedupe
                                # deep-copies any stale-user boundary row, so both
                                # arrays share the id (#5564).
                                _assign_stable_message_ids(
                                    _result_messages, _previous_messages, _previous_context_messages
                                )
                                _next_context_messages = _dedupe_replayed_context_messages(
                                    _previous_context_messages,
                                    _next_context_messages,
                                    msg_text,
                                )
                                s.context_messages = _deduplicate_context_messages(_next_context_messages)
                                s.messages = _merge_display_messages_after_agent_result(
                                    _previous_messages,
                                    _previous_context_messages,
                                    _restore_reasoning_metadata(_previous_messages, _result_messages),
                                    msg_text,
                                    source=getattr(s, 'pending_user_source', None) or 'webui',
                                )
                                _advance_truncation_watermark_after_commit(s)  # #3831
                                # Skip the error block — jump directly to the
                                # normal post-result persistence path by
                                # leaving _assistant_added truthy (set below).
                                _assistant_added = True  # prevent re-entering guard
                        if not _assistant_added:
                            # Self-heal didn't apply or retry failed — emit error
                            _err_label = 'Authentication failed'
                            _err_type = 'auth_mismatch'
                            _err_hint = (
                                'The selected model may not be supported by your configured provider or '
                                'your API key is invalid. Run `hermes model` in your terminal to '
                                'update credentials, then restart the WebUI.'
                            )
                    elif _is_auth:
                        _err_label = 'Authentication failed'
                        _err_type = 'auth_mismatch'
                        _err_hint = (
                            'The selected model may not be supported by your configured provider or '
                            'your API key is invalid. Run `hermes model` in your terminal to '
                            'update credentials, then restart the WebUI.'
                        )
                    elif _tool_limit_reached:
                        _err_label = 'Tool iteration limit reached'
                        _err_type = 'tool_limit_reached'
                        _err_hint = (
                            'The agent reached its configured tool iteration limit before producing '
                            'a final answer. Start a narrower follow-up or increase agent.max_turns.'
                        )
                        _err_str = (
                            'The agent reached its configured tool iteration limit before producing '
                            'a final answer.'
                        )
                    else:
                        _err_label = _classification['label']
                        _err_type = _classification['type']
                        _err_hint = _classification['hint']
                    # Skip error emission if credential self-heal succeeded
                    # (#1401) — _assistant_added is set True on successful retry.
                    if _assistant_added:
                        # Self-heal succeeded: messages are already merged into s,
                        # fall through to normal post-result persistence below.
                        pass
                    else:
                        _error_payload = _provider_error_payload(
                            _err_str or f'{_err_label}.',
                            _err_type,
                            _err_hint,
                        )
                        if _turn_pending_source == 'process_wakeup':
                            _recorded_pause = record_process_wakeup_provider_unavailable_pause(
                                s,
                                classification=_err_type,
                                model=_turn_route_model,
                                provider=_turn_route_provider,
                            )
                            # Disclose the suppression so the silence reads as
                            # intentional, not a stuck agent (#3929 UX) — but ONLY
                            # when a pause was actually recorded (credential-pool
                            # exhaustion), never for a rate-limit/other wakeup
                            # failure that doesn't pause. Keep the SSE payload hint
                            # in sync with the persisted bubble.
                            if _recorded_pause:
                                _err_hint = (
                                    (_err_hint + ' ' if _err_hint else '')
                                    + 'Automatic retries for this conversation are paused until you '
                                    + 'send a message, switch the model/provider, or fix the credentials.'
                                )
                                _error_payload['hint'] = _err_hint
                        _materialize_pending_user_turn_before_error(s)
                        s.active_stream_id = None
                        s.pending_user_message = None
                        s.pending_attachments = []
                        s.pending_started_at = None
                        s.pending_user_source = None
                        try:
                            _snapshot_and_append_partial_on_error(s, stream_id)
                        except Exception:
                            logger.debug("Failed to snapshot partials on error for %s", stream_id, exc_info=True)
                        _error_content = (
                            f'**{_err_label}:** {_error_payload.get("message") or _err_label}'
                            + (f'\n\n*{_err_hint}*' if _err_hint else '')
                        )
                        _error_message = {
                            'role': 'assistant',
                            'content': _error_content,
                            'timestamp': int(time.time()),
                            '_error': True,
                        }
                        if _err_type == 'compression_exhausted':
                            _recovery = stamp_compression_exhausted_recovery(
                                s,
                                message=_error_payload.get('message') or _err_label,
                                details=_error_payload.get('details') or '',
                            )
                            _error_message['_compressionRecovery'] = _recovery
                            _error_payload['compression_recovery'] = _recovery
                            _error_payload['recommended_recovery_action'] = _recovery.get('recommended_action')
                        if _error_payload.get('details'):
                            _error_message['provider_details'] = _error_payload['details']
                        if _err_type == 'cancelled':
                            _error_message['provider_details_label'] = 'Cancellation details'
                        elif _err_type == 'interrupted':
                            _error_message['provider_details_label'] = 'Interruption details'
                        elif _err_type == 'tool_limit_reached':
                            _error_message['provider_details_label'] = 'Terminal state details'
                        s.messages.append(_error_message)
                        try:
                            s.save()
                        except Exception:
                            pass
                        _error_payload['session'] = redact_session_data(
                            _session_payload_with_full_messages(s, tool_calls=s.tool_calls)
                        )
                        _error_payload['session_id'] = s.session_id
                        _error_payload['old_session_id'] = _compression_origin_session_id
                        if _compression_continuation_session_id is not None:
                            _error_payload['new_session_id'] = _compression_continuation_session_id
                            _error_payload['continuation_session_id'] = _compression_continuation_session_id
                        if _err_type == 'tool_limit_reached':
                            _error_payload['terminal_state'] = 'tool_limit_reached'
                            _error_payload['terminal_reason'] = 'max_iterations'
                        put('apperror', _error_payload)
                        # Legacy #373 source tests and clients look for the
                        # no_response type; #1765 keeps that type but improves
                        # the catch-all label, hint, and provider details.
                        return  # apperror already closes the stream on the client side

                # ── Handle context compression side effects ──
                # Also detect compression via the result dict or compressor state
                if not _compressed:
                    _compressor = getattr(agent, 'context_compressor', None)
                    if _compressor and getattr(_compressor, 'compression_count', 0) > _pre_compression_count:
                        _compressed = True
                # Notify the frontend that compression happened
                if _compressed:
                    s.context_messages = _prune_context_tool_results_after_compression(
                        agent,
                        s.context_messages,
                    )
                    s.post_compression_context_tokens_estimate = _estimate_post_compression_context_tokens(
                        agent,
                        s.context_messages,
                        workspace_system_msg,
                    )
                    visible_after = visible_messages_for_anchor(s.messages, auto_compression=True)
                    # Find the LAST [CONTEXT COMPACTION] marker in s.messages
                    # and count visible messages before it. This is the correct
                    # anchor — it points to the compression boundary regardless
                    # of how many turns have been added since the boundary was
                    # established. Using len(visible_before)-1 is fragile when
                    # _previous_messages doesn't include markers or when extra
                    # messages accumulate between compression and the done event.
                    _last_marker_raw_idx = None
                    for _mi, _m in enumerate(s.messages):
                        if _is_context_compression_marker(_m):
                            _last_marker_raw_idx = _mi
                    if _last_marker_raw_idx is not None:
                        _visible_before_marker = visible_messages_for_anchor(
                            s.messages[:_last_marker_raw_idx], auto_compression=True,
                        )
                        s.compression_anchor_visible_idx = max(0, len(_visible_before_marker) - 1)
                        logger.info(
                            '[ANCHOR-MARKER] session=%s marker_raw=%d vis_before=%d anchor=%d',
                            getattr(s, 'session_id', '?'),
                            _last_marker_raw_idx,
                            len(_visible_before_marker),
                            s.compression_anchor_visible_idx,
                        )
                    else:
                        # Fallback: use pre-turn display messages
                        visible_before = visible_messages_for_anchor(
                            _previous_messages, auto_compression=True,
                        )
                        if visible_before:
                            s.compression_anchor_visible_idx = max(0, len(visible_before) - 1)
                        elif visible_after:
                            s.compression_anchor_visible_idx = 0
                        else:
                            s.compression_anchor_visible_idx = None
                        logger.info(
                            '[ANCHOR-FALLBACK] session=%s vis_before=%d anchor=%d',
                            getattr(s, 'session_id', '?'),
                            len(visible_before) if visible_before else 0,
                            s.compression_anchor_visible_idx if s.compression_anchor_visible_idx is not None else -1,
                        )
                    # Pick anchor_msg for _compression_anchor_message_key
                    _anchor_vis_idx = s.compression_anchor_visible_idx
                    if _anchor_vis_idx is not None and visible_after and _anchor_vis_idx < len(visible_after):
                        anchor_msg = visible_after[_anchor_vis_idx]
                    elif visible_after:
                        anchor_msg = visible_after[-1]
                    else:
                        anchor_msg = None
                    s.compression_anchor_message_key = (
                        _compression_anchor_message_key(anchor_msg) if anchor_msg else None
                    )
                    s.compression_anchor_summary = _compact_summary_text(
                        _compression_summary_from_messages(s.messages)
                        or _compression_summary_from_messages(s.context_messages)
                    )
                    if _compression_continuation_session_id is None:
                        _compression_continuation_session_id = s.session_id
                    put('compressed', {
                        'session_id': _compression_origin_session_id,
                        'old_session_id': _compression_origin_session_id,
                        'new_session_id': _compression_continuation_session_id,
                        'continuation_session_id': _compression_continuation_session_id,
                        'message': 'Compression finished',
                        'usage': _live_usage_snapshot(),
                    })

                # Stamp 'timestamp' on any messages that don't have one yet,
                # preserving transcript order across compacted/reconciled batches.
                _stamp_missing_message_timestamps(s.messages)
                # Only auto-generate title when still default; preserves user renames
                if s.title == 'Untitled' or s.title == 'New Chat' or not s.title:
                    s.title = title_from(s.messages, s.title)
                _looks_default = (s.title == 'Untitled' or s.title == 'New Chat' or not s.title)
                _looks_provisional = _is_provisional_title(s.title, s.messages)
                _invalid_existing_title = _looks_invalid_generated_title(s.title)
                _should_bg_title = (
                    (_looks_default or _looks_provisional or _invalid_existing_title)
                    and (not getattr(s, 'llm_title_generated', False) or _invalid_existing_title)
                )
                _u0 = ''
                _a0 = ''
                if _should_bg_title:
                    _u0, _a0 = _first_exchange_snippets(s.messages)
                # Read token/cost usage from the agent object (if available).
                # Per-turn overwrite (#1857): replace cumulative session totals with the
                # agent's most recent values, which already represent the current turn's
                # full prompt+completion (input_tokens are the entire context, not delta).
                # Defensive: only overwrite when the agent reports non-zero / non-None
                # values. A rebuilt-from-cache-miss agent (post-restart, post-LRU-eviction)
                # starts at zero; without this guard, the next turn would zero out the
                # persisted disk total before any new tokens were spent. Per Opus advisor
                # on stage-320: prevents restart-induced regression of session usage data.
                input_tokens = getattr(agent, 'session_prompt_tokens', 0) or 0
                output_tokens = getattr(agent, 'session_completion_tokens', 0) or 0
                estimated_cost = getattr(agent, 'session_estimated_cost_usd', None)
                cache_read_tokens = getattr(agent, 'session_cache_read_tokens', 0) or 0
                cache_write_tokens = getattr(agent, 'session_cache_write_tokens', 0) or 0
                prev_input_tokens = getattr(s, 'input_tokens', 0) or 0
                prev_cache_read_tokens = getattr(s, 'cache_read_tokens', 0) or 0
                turn_input_tokens = max(0, input_tokens - prev_input_tokens)
                turn_cache_read_tokens = max(0, cache_read_tokens - prev_cache_read_tokens)
                # Per-turn percent is computed server-side from persisted session
                # counters so the message label uses the same denominator as the
                # final usage payload even if the browser missed an intermediate event.
                cache_hit_percent = prompt_cache_hit_percent(cache_read_tokens, input_tokens)
                turn_cache_hit_percent = prompt_cache_hit_percent(turn_cache_read_tokens, turn_input_tokens)
                if input_tokens > 0:
                    s.input_tokens = input_tokens
                if output_tokens > 0:
                    s.output_tokens = output_tokens
                if estimated_cost is not None:
                    s.estimated_cost = estimated_cost
                if cache_read_tokens > 0:
                    s.cache_read_tokens = cache_read_tokens
                if cache_write_tokens > 0:
                    s.cache_write_tokens = cache_write_tokens
                # Persist tool-call summaries even when the final message history only
                # kept bare tool rows and omitted explicit assistant tool_call IDs.
                tool_calls = _extract_tool_calls_from_messages(
                    s.messages,
                    live_tool_calls=_live_tool_calls,
                )
                s.tool_calls = tool_calls
                s.active_stream_id = None
                s.pending_user_message = None
                s.pending_attachments = []
                s.pending_started_at = None
                s.pending_user_source = None
                # Tag the matching user message with attachment filenames for display on reload
                # Only tag a user message whose content relates to this turn's text
                # (msg_text is the full message including the [Attached files: ...] suffix)
                if attachments:
                    display_attachments = [_attachment_name(a) for a in attachments if _attachment_name(a)]
                    for m in reversed(s.messages):
                        if m.get('role') == 'user':
                            content = str(m.get('content', ''))
                            # Match if content is part of the sent message or vice-versa
                            base_text = msg_text.split('\n\n[Attached files:')[0].strip() if '\n\n[Attached files:' in msg_text else msg_text
                            if base_text[:60] in content or content[:60] in msg_text:
                                m['attachments'] = display_attachments
                                break
                # Persist reasoning trace in the session so it survives reload.
                # Must run BEFORE s.save() — otherwise the mutation lives only in
                # memory until the next turn's save, and the last-turn thinking card
                # is lost when the user reloads immediately after a response.
                #
                # #3455/#3599: split inline thinking blocks out of the saved
                # assistant content into m['reasoning'] (server-side twin of the JS
                # _splitThinkFromContent). Inline-thinking providers (e.g. MiniMax-M3)
                # otherwise leave the thinking trace in m['content'], bloating the
                # persisted session file 30-50% and bypassing the thinking card. The
                # #3587: use per-message segments so intermediate assistant turns
                # (before tool calls) each receive their own reasoning trace rather
                # than all reasoning being written only to the last assistant message.
                # Scope the walk to this turn's newly-appended assistant messages
                # to prevent cross-turn reasoning clobber (multi-turn off-by-N).
                if s.messages:
                    _prev_asst = sum(
                        1 for m in (_previous_messages or [])
                        if isinstance(m, dict) and m.get('role') == 'assistant'
                    )
                    _asst_count = 0
                    for _rm in s.messages:
                        if not (isinstance(_rm, dict) and _rm.get('role') == 'assistant'):
                            continue
                        _turn_idx = _asst_count
                        _asst_count += 1
                        if _turn_idx < _prev_asst:
                            continue  # prior-turn message — never touch its reasoning
                        _seg_reasoning = _reasoning_segments.get(_turn_idx - _prev_asst, '')
                        _existing_reasoning = _seg_reasoning or _rm.get('reasoning') or ''
                        _content = _rm.get('content')
                        if isinstance(_content, str) and _content:
                            _new_content, _merged_reasoning = _split_thinking_from_content(
                                _content, _existing_reasoning
                            )
                            _rm['content'] = _new_content
                            if _merged_reasoning:
                                _rm['reasoning'] = _merged_reasoning
                        elif _existing_reasoning:
                            _rm['reasoning'] = _existing_reasoning
                try:
                    _turn_duration_seconds = max(0.0, time.time() - float(_turn_started_at))
                except Exception:
                    _turn_duration_seconds = 0.0
                _turn_tps = None
                if output_tokens and _turn_duration_seconds > 0:
                    _turn_tps = round(float(output_tokens) / _turn_duration_seconds, 1)
                _gateway_routing = _extract_gateway_routing_metadata(
                    agent,
                    result,
                    requested_model=resolved_model or model,
                    requested_provider=resolved_provider,
                )
                if _gateway_routing:
                    s.gateway_routing = _gateway_routing
                    _history = list(getattr(s, 'gateway_routing_history', None) or [])
                    _history.append(_gateway_routing)
                    s.gateway_routing_history = _history[-50:]
                if s.messages:
                    for _dm in reversed(s.messages):
                        if isinstance(_dm, dict) and _dm.get('role') == 'assistant':
                            _dm['_turnDuration'] = round(_turn_duration_seconds, 3)
                            if _turn_tps is not None:
                                _dm['_turnTps'] = _turn_tps
                            if _gateway_routing:
                                _dm['_gatewayRouting'] = _gateway_routing
                            _ttft_ms = meter().get_ttft_ms(stream_id)
                            if _ttft_ms is not None:
                                _dm['_firstTokenMs'] = _ttft_ms
                            break
                # Persist context window data on the session so the context-ring
                # indicator survives a page reload (#1318). Must run BEFORE
                # s.save() for the same reason as the reasoning trace above.
                # The fields are captured into the SSE usage payload below; this
                # block writes them to the session itself so GET /api/session
                # returns them on reload instead of falling back to 0.
                _cc_for_save = getattr(agent, 'context_compressor', None)
                # Initialized before the compressor block so the #3256/#3263
                # threshold-rescale below is safe even when there is no
                # compressor (fresh agent / interrupted stream): _skip_cc_cl
                # stays False and _cc_cl stays 0, so the rescale is a no-op.
                _skip_cc_cl = False
                _cc_cl = 0
                if _cc_for_save:
                    _cc_cl = getattr(_cc_for_save, 'context_length', 0) or 0
                    # Same guard as routes._resolve_context_length_for_session_model:
                    # the agent-side context_compressor was constructed with the
                    # global model.context_length applied to EVERY model. If the
                    # session's model isn't model.default, that value is a stale
                    # cap (e.g. 232K) that would clobber the real 1M metadata
                    # on every stream end. In that case skip the compressor
                    # value and let the fallback resolver below recompute.
                    # #4618: broaden the stale-compressor guard the same way the
                    # live-usage snapshot does. The OLD test only skipped the
                    # compressor value when it equalled the config cap EXACTLY
                    # (a non-default model carrying the global cap). But a
                    # compressor can hold a DIFFERENT model's window after an
                    # in-place model switch (e.g. opus-4.5's 168k lingering on an
                    # opus-4.8 1M session) — that value != the config cap, so the
                    # old guard let it persist to s.context_length and the SSE
                    # payload, snapping the indicator back to 168k at turn-end.
                    # Resolve the real per-model window via the SAME helper the
                    # live path + hydration use and skip the compressor value
                    # whenever the real window differs, honoring the #4248
                    # acceptance gate (never let a low-confidence 256k fallback
                    # clobber a larger cached window).
                    _skip_cc_cl = False
                    try:
                        _cli_cc = api._context_length_lookup_inputs_for_model
                        _accept_cc = api._should_accept_session_context_length_refresh
                        from agent.model_metadata import get_model_context_length as _g_cc
                        _sess_model_cc = str(getattr(agent, 'model', resolved_model or '') or '').strip()
                        if _sess_model_cc and _cc_cl > 0:
                            _lk_cc = _cli_cc(
                                _sess_model_cc,
                                resolved_provider or '',
                                base_url=getattr(agent, 'base_url', '') or resolved_base_url or '',
                                api_key=getattr(agent, 'api_key', '') or resolved_api_key or '',
                                cfg=_cfg if isinstance(_cfg, dict) else {},
                            )
                            try:
                                _real_cc = _g_cc(
                                    _sess_model_cc,
                                    _lk_cc.base_url,
                                    api_key=_lk_cc.api_key,
                                    config_context_length=_lk_cc.config_context_length,
                                    provider=_lk_cc.provider or resolved_provider or '',
                                    custom_providers=_lk_cc.custom_providers,
                                ) or 0
                            except TypeError:
                                _real_cc = _g_cc(_sess_model_cc, _lk_cc.base_url) or 0
                            if _real_cc and _real_cc != _cc_cl and _accept_cc(_cc_cl, _real_cc):
                                _skip_cc_cl = True
                    except Exception:
                        pass
                    if not _skip_cc_cl:
                        s.context_length = _cc_cl
                    s.threshold_tokens = getattr(_cc_for_save, 'threshold_tokens', 0) or 0
                    s.last_prompt_tokens = getattr(_cc_for_save, 'last_prompt_tokens', 0) or 0
                # Fallback: if the compressor didn't report a context_length
                # (fresh agent, interrupted stream, or compressor missing the
                # attribute), resolve it from the model's static metadata so
                # the indicator can still show a meaningful percentage.
                # Sourced from PR #1344 (@jasonjcwu) — extracted to a focused
                # follow-up after PR #1344 was closed as superseded by #1341.
                #
                # #1896: pass config_context_length, provider, and
                # custom_providers so explicit config overrides win over the
                # 256K default fallback. Without these, users on 1M-context
                # models who set `model.context_length: 1048576` (or rely on
                # a `custom_providers` per-model override) get a 256K
                # window in the persisted session and the SSE payload —
                # which then trips LCM auto-compress at ~25% of the wrong
                # value, cascading into 429 floods.
                #
                # #3256/#3263: ALSO run this fallback when _skip_cc_cl is true
                # (non-default model whose compressor carried the stale global
                # cap). Without this, a session that already had a stale 232K
                # context_length persisted keeps it forever — skipping the
                # compressor write removes the re-clobber but never recomputes
                # the real per-model window. Recompute and overwrite in that case.
                if (not getattr(s, 'context_length', 0)) or _skip_cc_cl:
                    try:
                        from agent.model_metadata import get_model_context_length
                        _context_length_lookup_inputs_for_model = (
                            api._context_length_lookup_inputs_for_model
                        )
                        _cfg_base_url = getattr(agent, 'base_url', '') or resolved_base_url or ''
                        _ctx_lookup = _context_length_lookup_inputs_for_model(
                            getattr(agent, 'model', resolved_model or '') or '',
                            resolved_provider,
                            base_url=_cfg_base_url,
                            cfg=_cfg if isinstance(_cfg, dict) else {},
                        )
                        _cfg_ctx_len = _ctx_lookup.config_context_length
                        _cfg_custom_providers = _ctx_lookup.custom_providers
                        _cfg_api_key = _ctx_lookup.api_key or getattr(agent, 'api_key', '') or resolved_api_key or ''
                        _cfg_base_url = _ctx_lookup.base_url or _cfg_base_url
                        _cfg_provider = _ctx_lookup.provider or resolved_provider or ''
                        _resolved_cl = get_model_context_length(
                            getattr(agent, 'model', resolved_model or '') or '',
                            _cfg_base_url,
                            api_key=_cfg_api_key,
                            config_context_length=_cfg_ctx_len,
                            provider=_cfg_provider,
                            custom_providers=_cfg_custom_providers,
                        )
                        if _resolved_cl:
                            s.context_length = _resolved_cl
                    except TypeError:
                        # Older hermes-agent builds whose get_model_context_length
                        # signature pre-dates the config_context_length /
                        # custom_providers kwargs. Retry with the legacy 2-arg
                        # form so the indicator still resolves *something*.
                        try:
                            from agent.model_metadata import get_model_context_length as _legacy_cl
                            _resolved_cl = _legacy_cl(
                                getattr(agent, 'model', resolved_model or '') or '',
                                _cfg_base_url,
                            )
                            if _resolved_cl:
                                s.context_length = _resolved_cl
                        except Exception:
                            pass
                    except Exception:
                        # Older hermes-agent builds may not expose this helper.
                        # Better to leave context_length=0 than crash the save.
                        pass
                # #3256/#3263: when we skipped the stale compressor cap for a
                # non-default model and recomputed the real per-model window
                # above, rescale the persisted threshold_tokens to that real cap
                # so the auto-compress trigger and the reloaded context-ring
                # match the live snapshot (which already rescales). Without this,
                # a reload shows a smaller compression trigger than streaming did.
                # Only rescale when both the original cap and threshold are
                # positive; otherwise clear the threshold to 0 (consistent with
                # the live-snapshot path) rather than leave a stale value.
                if _skip_cc_cl:
                    _orig_cap = _cc_cl  # the stale global cap the compressor reported
                    _orig_thresh = getattr(s, 'threshold_tokens', 0) or 0
                    _real_cap = getattr(s, 'context_length', 0) or 0
                    if _real_cap > 0 and _orig_cap > 0 and _orig_thresh > 0:
                        s.threshold_tokens = int(_orig_thresh * _real_cap / _orig_cap)
                    else:
                        s.threshold_tokens = 0
                if not ephemeral and s.messages:
                    _latest_assistant_idx = next(
                        (idx for idx in range(len(s.messages) - 1, -1, -1)
                         if isinstance(s.messages[idx], dict) and s.messages[idx].get('role') == 'assistant'),
                        None,
                    )
                    if _latest_assistant_idx is not None:
                        _latest_assistant = s.messages[_latest_assistant_idx]
                        try:
                            append_turn_journal_event_for_stream(
                                s.session_id,
                                stream_id,
                                {
                                    "event": "assistant_started",
                                    "created_at": float(_latest_assistant.get('timestamp') or time.time()),
                                    "assistant_message_index": _latest_assistant_idx,
                                },
                            )
                        except Exception:
                            logger.debug("Failed to append assistant_started turn journal event", exc_info=True)
                if cancel_event.is_set():
                    _finalize_cancelled_turn(s, ephemeral=False)
                    try:
                        append_turn_journal_event_for_stream(
                            s.session_id,
                            stream_id,
                            {
                                "event": "interrupted",
                                "created_at": time.time(),
                                "reason": "cancelled",
                            },
                        )
                    except Exception:
                        logger.debug("Failed to append cancelled turn journal event", exc_info=True)
                    put('cancel', _cancel_event_payload('Cancelled by user'))
                    return
                with _stream_writeback_stage(_writeback_timings, "session_save"):
                    s.save()
                if cancel_event.is_set():
                    _finalize_cancelled_turn(s, ephemeral=False)
                    try:
                        append_turn_journal_event_for_stream(
                            s.session_id,
                            stream_id,
                            {
                                "event": "interrupted",
                                "created_at": time.time(),
                                "reason": "cancelled",
                            },
                        )
                    except Exception:
                        logger.debug("Failed to append cancelled turn journal event", exc_info=True)
                    put('cancel', _cancel_event_payload('Cancelled by user'))
                    return
                if not ephemeral:
                    try:
                        append_turn_journal_event_for_stream(
                            s.session_id,
                            stream_id,
                            {
                                "event": "completed",
                                "created_at": time.time(),
                                "assistant_message_index": next(
                                    (idx for idx in range(len(s.messages) - 1, -1, -1)
                                     if isinstance(s.messages[idx], dict) and s.messages[idx].get('role') == 'assistant'),
                                    None,
                                ),
                            },
                        )
                    except Exception:
                        logger.debug("Failed to append completed turn journal event", exc_info=True)
                if not ephemeral:
                    # ── Memory-provider lifecycle: mark turn completed (CLI parity) ──
                    # Completed, non-ephemeral turns are marked dirty/uncommitted so
                    # boundary drains know there is work.  Per CLI semantics, the
                    # actual memory extraction/commit happens only at session boundaries
                    # (new session creation, LRU eviction, shutdown drain) — NOT after
                    # every completed turn.  This mirrors Hermes CLI where
                    # run_agent.py::_sync_external_memory_for_turn() records messages
                    # but only AIAgent.commit_memory_session()/shutdown_memory_provider()
                    # trigger extraction via provider on_session_end().  The mark is
                    # in-memory bookkeeping, not provider I/O, so keep it inside the
                    # per-session writeback lock to preserve completed-turn ordering.
                    try:
                        from api.session_lifecycle import mark_turn_completed
                        mark_turn_completed(s.session_id, agent=agent)
                    except Exception:
                        logger.debug("Memory lifecycle mark failed for session %s", s.session_id, exc_info=True)
                with _stream_writeback_stage(_writeback_timings, "persistent_state_scan"):
                    try:
                        _persistent_changes = _persistent_state_changes(
                            _persistent_state_before,
                            _persistent_state_snapshot(_profile_home),
                        )
                        if _persistent_changes.get("memory_saved"):
                            put("state_saved", {
                                "session_id": session_id,
                                "kind": "memory",
                                "action": "saved",
                            })
                        for _skill_change in _persistent_changes.get("skills") or []:
                            put("state_saved", {
                                "session_id": session_id,
                                "kind": "skill",
                                "action": _skill_change.get("action") or "updated",
                                "name": _skill_change.get("name") or "",
                            })
                    except Exception:
                        logger.debug("Persistent state change detection failed for session %s", s.session_id, exc_info=True)
            # Sync to state.db for /insights (opt-in setting)
            with _stream_writeback_stage(_writeback_timings, "state_sync"):
                try:
                    from api.config import load_settings as _load_settings
                    if _load_settings().get('sync_to_insights'):
                        from api.state_sync import sync_session_usage
                        sync_session_usage(
                            session_id=s.session_id,
                            input_tokens=s.input_tokens or 0,
                            output_tokens=s.output_tokens or 0,
                            estimated_cost=s.estimated_cost,
                            model=model,
                            title=s.title,
                            message_count=len(s.messages),
                            cache_read_tokens=s.cache_read_tokens or 0,
                            cache_write_tokens=s.cache_write_tokens or 0,
                            api_call_count=getattr(agent, 'session_api_calls', None),
                            # #2762: pass the session's profile explicitly so the
                            # background-thread state.db lookup doesn't fall
                            # through to the process-global active profile and
                            # write to the wrong DB (TLS profile is set on the
                            # HTTP thread but not propagated to this worker).
                            profile=getattr(s, 'profile', None),
                        )
                except Exception:
                    logger.debug("Failed to sync session to insights")
            # A late cancel can land during memory/state-sync writeback. Do not
            # clear a credential-exhausted process-wakeup pause unless this run
            # is still settling as a normal completion. The pause re-read, clear,
            # restore, and save must stay under the session lock so a concurrent
            # suppression cannot observe stale pause state or lose its update.
            _lock_ctx = _agent_lock if _agent_lock is not None else contextlib.nullcontext()
            with _lock_ctx:
                if cancel_event.is_set():
                    _finalize_cancelled_turn(s, ephemeral=False)
                    try:
                        append_turn_journal_event_for_stream(
                            s.session_id,
                            stream_id,
                            {
                                "event": "interrupted",
                                "created_at": time.time(),
                                "reason": "cancelled",
                            },
                        )
                    except Exception:
                        logger.debug("Failed to append cancelled turn journal event", exc_info=True)
                    put('cancel', _cancel_event_payload('Cancelled by user'))
                    return
                try:
                    _latest_pause_owner = get_session(getattr(s, 'session_id', session_id))
                    if _latest_pause_owner is not None:
                        s = _latest_pause_owner
                except Exception:
                    logger.debug(
                        "Failed to re-read process wakeup pause before success clear",
                        exc_info=True,
                    )
                _process_wakeup_pause_before_clear = dict(getattr(s, 'process_wakeup_pause', {}) or {})
                if clear_process_wakeup_pause(s, reason='run_completed'):
                    if cancel_event.is_set():
                        s.process_wakeup_pause = dict(_process_wakeup_pause_before_clear)
                        try:
                            s.save(touch_updated_at=False)
                        except Exception:
                            logger.debug("Failed to persist restored process wakeup pause", exc_info=True)
                        _finalize_cancelled_turn(s, ephemeral=False)
                        try:
                            append_turn_journal_event_for_stream(
                                s.session_id,
                                stream_id,
                                {
                                    "event": "interrupted",
                                    "created_at": time.time(),
                                    "reason": "cancelled",
                                },
                            )
                        except Exception:
                            logger.debug("Failed to append cancelled turn journal event", exc_info=True)
                        put('cancel', _cancel_event_payload('Cancelled by user'))
                        return
                    with _stream_writeback_stage(_writeback_timings, "process_wakeup_pause_clear_save"):
                        s.save(touch_updated_at=False)
                    if cancel_event.is_set():
                        s.process_wakeup_pause = dict(_process_wakeup_pause_before_clear)
                        try:
                            s.save(touch_updated_at=False)
                        except Exception:
                            logger.debug("Failed to persist restored process wakeup pause", exc_info=True)
                        _finalize_cancelled_turn(s, ephemeral=False)
                        try:
                            append_turn_journal_event_for_stream(
                                s.session_id,
                                stream_id,
                                {
                                    "event": "interrupted",
                                    "created_at": time.time(),
                                    "reason": "cancelled",
                                },
                            )
                        except Exception:
                            logger.debug("Failed to append cancelled turn journal event", exc_info=True)
                        put('cancel', _cancel_event_payload('Cancelled by user'))
                        return
                _success_writeback_committed = True
            usage = {
                'input_tokens': input_tokens,
                'output_tokens': output_tokens,
                'estimated_cost': estimated_cost,
                'cache_read_tokens': cache_read_tokens,
                'cache_write_tokens': cache_write_tokens,
                'cache_hit_percent': cache_hit_percent,
                'turn_cache_hit_percent': turn_cache_hit_percent,
                'duration_seconds': round(_turn_duration_seconds, 3),
            }
            if _turn_tps is not None:
                usage['tps'] = _turn_tps
            if _gateway_routing:
                usage['gateway_routing'] = _gateway_routing
            _ttft_ms = meter().get_ttft_ms(stream_id)
            if _ttft_ms is not None:
                usage['ttft_ms'] = _ttft_ms
            # Include context window data from the agent's compressor for the UI indicator.
            # The session-level persistence happens above (before s.save()) so the values
            # survive a page reload; this block only populates the live SSE usage payload.
            _cc = getattr(agent, 'context_compressor', None)
            if _cc:
                _cc_cl_sse = getattr(_cc, 'context_length', 0) or 0
                # #3256/#3263: remember the original compressor cap + threshold
                # so that if we drop the stale cap below and the fallback
                # resolves the real per-model window, we can rescale the
                # threshold consistently (the live snapshot already does this).
                _orig_cc_cl_sse = _cc_cl_sse
                _orig_cc_thresh_sse = getattr(_cc, 'threshold_tokens', 0) or 0
                _dropped_stale_cap_sse = False
                # Default-only guard (#3256), broadened (#4618): the agent-side
                # context_compressor caches a context_length from the model it
                # was built/last-updated with. For a non-default model it may be
                # the stale global cap (e.g. 232K); after an in-place model switch
                # it may be a DIFFERENT model's window (e.g. opus-4.5's 168k on an
                # opus-4.8 1M session). Either way, surfacing it via the terminal
                # `done` SSE makes the indicator REVERT to the wrong window on
                # stream end (messages.js overwrites S.lastUsage with this payload)
                # — the exact "send a message reverts to 168k" symptom. Resolve
                # the real per-model window via the SAME helper the live path +
                # hydration use; drop the compressor value whenever the real
                # window differs, honoring the #4248 acceptance gate (never let a
                # low-confidence 256k fallback clobber a larger cached window).
                try:
                    _cli_sse = api._context_length_lookup_inputs_for_model
                    _accept_sse = api._should_accept_session_context_length_refresh
                    from agent.model_metadata import get_model_context_length as _g_sse
                    _sess_model_sse = str(getattr(agent, 'model', resolved_model or '') or '').strip()
                    if _sess_model_sse and _cc_cl_sse > 0:
                        _lk_sse = _cli_sse(
                            _sess_model_sse,
                            resolved_provider or '',
                            base_url=getattr(agent, 'base_url', '') or resolved_base_url or '',
                            api_key=getattr(agent, 'api_key', '') or resolved_api_key or '',
                            cfg=_cfg if isinstance(_cfg, dict) else {},
                        )
                        try:
                            _real_sse = _g_sse(
                                _sess_model_sse,
                                _lk_sse.base_url,
                                api_key=_lk_sse.api_key,
                                config_context_length=_lk_sse.config_context_length,
                                provider=_lk_sse.provider or resolved_provider or '',
                                custom_providers=_lk_sse.custom_providers,
                            ) or 0
                        except TypeError:
                            _real_sse = _g_sse(_sess_model_sse, _lk_sse.base_url) or 0
                        if _real_sse and _real_sse != _cc_cl_sse and _accept_sse(_cc_cl_sse, _real_sse):
                            _cc_cl_sse = 0
                            _dropped_stale_cap_sse = True
                except Exception:
                    pass
                if _cc_cl_sse:
                    usage['context_length'] = _cc_cl_sse
                usage['threshold_tokens'] = getattr(_cc, 'threshold_tokens', 0) or 0
                usage['last_prompt_tokens'] = getattr(_cc, 'last_prompt_tokens', 0) or 0
            # Fallback: when the compressor is absent or reports context_length=0,
            # resolve the model's context window from metadata so the UI indicator
            # shows the correct percentage rather than overflowing against the 128K
            # JS default.  Mirrors the session-save fallback above (lines ~2205-2217).
            #
            # #1896: pass config_context_length, provider, and custom_providers so
            # explicit config overrides win over the 256K default fallback. The
            # SSE payload's `context_length` is what feeds the live token-usage
            # indicator, so a stale 256K here surfaces as the same wrong-window
            # display that motivates this fix.
            if not usage.get('context_length'):
                try:
                    from agent.model_metadata import get_model_context_length as _get_cl
                    _context_length_lookup_inputs_for_model = (
                        api._context_length_lookup_inputs_for_model
                    )
                    _ctx_lookup = _context_length_lookup_inputs_for_model(
                        getattr(agent, 'model', resolved_model or '') or '',
                        resolved_provider,
                        base_url=getattr(agent, 'base_url', '') or resolved_base_url or '',
                        cfg=_cfg if isinstance(_cfg, dict) else {},
                    )
                    _cfg_ctx_len = _ctx_lookup.config_context_length
                    _cfg_custom_providers = _ctx_lookup.custom_providers
                    _cfg_api_key = _ctx_lookup.api_key or getattr(agent, 'api_key', '') or resolved_api_key or ''
                    _cfg_base_url = _ctx_lookup.base_url
                    _cfg_provider = _ctx_lookup.provider or resolved_provider or ''
                    try:
                        _fb_cl = _get_cl(
                            getattr(agent, 'model', resolved_model or '') or '',
                            _cfg_base_url,
                            api_key=_cfg_api_key,
                            config_context_length=_cfg_ctx_len,
                            provider=_cfg_provider,
                            custom_providers=_cfg_custom_providers,
                        )
                    except TypeError:
                        # Older hermes-agent builds: fall back to legacy 2-arg form.
                        _fb_cl = _get_cl(
                            getattr(agent, 'model', resolved_model or '') or '',
                            _cfg_base_url,
                        )
                    if _fb_cl:
                        usage['context_length'] = _fb_cl
                        # #3256/#3263: if we dropped the stale compressor cap
                        # for a non-default model, the threshold_tokens written
                        # above is still the stale compressor value. Rescale it
                        # to the real resolved window so the terminal `done`
                        # payload matches the live snapshot (which rescales) —
                        # otherwise messages.js overwrites S.lastUsage with the
                        # stale threshold and the indicator reverts on stream end.
                        if _dropped_stale_cap_sse and _orig_cc_cl_sse > 0 and _orig_cc_thresh_sse > 0:
                            usage['threshold_tokens'] = int(_orig_cc_thresh_sse * _fb_cl / _orig_cc_cl_sse)
                except Exception:
                    pass
            # Fallback: when last_prompt_tokens is missing (no compressor), use the
            # session-persisted value rather than letting the frontend fall back to
            # the cumulative input_tokens counter, which overflows for long sessions.
            if not usage.get('last_prompt_tokens'):
                _sess_lpt = getattr(s, 'last_prompt_tokens', 0) or 0
                if _sess_lpt:
                    usage['last_prompt_tokens'] = _sess_lpt
            _post_compression_estimate = getattr(s, 'post_compression_context_tokens_estimate', None)
            usage['post_compression_context_tokens_estimate'] = (
                _post_compression_estimate
                if isinstance(_post_compression_estimate, int) and _post_compression_estimate > 0
                else None
            )
            # (reasoning trace already attached + saved above, before s.save())
            # Leftover-steer delivery: if a /steer was queued (via
            # api/chat/steer) but the agent finished its turn before
            # reaching a tool-result boundary that would consume it,
            # the text is still stashed in agent._pending_steer. Drain
            # it now and emit a pending_steer_leftover SSE event so the
            # frontend can queue it for the next turn — same fallback
            # path as the CLI in cli.py:8788-8794.
            try:
                _drain_pending_steer = getattr(agent, '_drain_pending_steer', None)
                _leftover = _drain_pending_steer() if _drain_pending_steer else None
                if _leftover:
                    put('pending_steer_leftover', {
                        'session_id': session_id,
                        'text': str(_leftover),
                    })
            except Exception:
                logger.debug("Failed to drain pending steer for session %s", session_id)
            # /goal parity: after a successful assistant turn, run the Hermes
            # GoalManager judge before terminal done/stream_end events. The
            # frontend surfaces the status line and queues continuation_prompt as
            # a normal next user message so /queue and user input keep priority.
            # #1932: only evaluate when the turn was goal-related (set via
            # STREAM_GOAL_RELATED or goal_related parameter).
            try:
                from api.goals import evaluate_goal_after_turn, has_active_goal

                if not goal_related or not has_active_goal(session_id, profile_home=_profile_home):
                    _goal_decision = {}
                else:
                    _last_goal_response = ''
                    for _goal_msg in reversed(s.messages or []):
                        if not isinstance(_goal_msg, dict) or _goal_msg.get('role') != 'assistant':
                            continue
                        _goal_content = _goal_msg.get('content', '')
                        if isinstance(_goal_content, list):
                            _goal_parts = []
                            for _goal_part in _goal_content:
                                if isinstance(_goal_part, dict):
                                    _goal_text = _goal_part.get('text') or _goal_part.get('content')
                                    if _goal_text:
                                        _goal_parts.append(str(_goal_text))
                            _last_goal_response = '\n'.join(_goal_parts)
                        else:
                            _last_goal_response = str(_goal_content or '')
                        break
                    put('goal', {
                        'session_id': session_id,
                        'state': 'evaluating',
                        'message': 'Evaluating goal progress…',
                        'message_key': 'goal_evaluating_progress',
                    })
                    _goal_decision = evaluate_goal_after_turn(
                        session_id,
                        _last_goal_response,
                        user_initiated=True,
                        profile_home=_profile_home,
                    )
                decision = _goal_decision or {}
                _goal_message = str(decision.get('message') or '').strip()
                if _goal_message:
                    put('goal', {
                        'session_id': session_id,
                        'state': 'continuing' if decision.get('should_continue') else 'idle',
                        'message': _goal_message,
                        'message_key': decision.get('message_key') or ('goal_continuing' if _goal_message else ''),
                        'message_args': decision.get('message_args') or [],
                        'decision': decision,
                    })
                if decision.get('should_continue'):
                    continuation_prompt = str(decision.get('continuation_prompt') or '').strip()
                    if continuation_prompt:
                        # #1932: mark this session as pending a goal continuation
                        # so the next /chat/start creates a goal-related stream.
                        PENDING_GOAL_CONTINUATION.add(session_id)
                        put('goal_continue', {
                            'session_id': session_id,
                            'continuation_prompt': continuation_prompt,
                            'text': continuation_prompt,
                            'message': _goal_message,
                            'message_key': decision.get('message_key') or 'goal_continuing',
                            'message_args': decision.get('message_args') or [],
                            'decision': decision,
                        })
            except Exception as _goal_exc:
                logger.debug("Goal continuation hook failed for session %s: %s", session_id, _goal_exc)
            with _stream_writeback_stage(_writeback_timings, "done_payload"):
                raw_session = _session_payload_with_full_messages(s, tool_calls=tool_calls)
                _done_payload = {'session': redact_session_data(raw_session), 'usage': usage}
                if _tool_limit_reached:
                    _done_payload['terminal_state'] = 'tool_limit_reached'
                    _done_payload['terminal_reason'] = 'max_iterations'
                put('done', _done_payload)
                # Emit one last metering packet for the live message-header TPS label.
                meter_stats = meter().get_stats(stream_id)
                meter_stats['session_id'] = session_id
                meter_stats.setdefault('tps_available', False)
                meter_stats.setdefault('estimated', False)
                put('metering', meter_stats)
            try:
                _log_stream_writeback_timings(
                    getattr(s, 'session_id', session_id),
                    stream_id,
                    _writeback_timings,
                    _writeback_started,
                )
            except Exception:
                # Diagnostics must never affect the stream lifecycle: a
                # misbehaving log handler here would otherwise skip the
                # background-title thread spawn below. (#4923 gate hardening)
                pass
            if _should_bg_title and _u0 and _a0:
                threading.Thread(
                    target=_run_background_title_update,
                    args=(s.session_id, _u0, _a0, str(s.title or '').strip(), put, agent),
                    daemon=True,
                ).start()
            else:
                # Use the original session_id parameter (never reassigned), not s.session_id
                # which may be rotated during context compression. The client captured
                # activeSid = original session_id so they must match for stream_end to close.
                put('stream_end', {'session_id': session_id})
                # Adaptive title refresh: re-generate title from latest exchange
                # every N exchanges (when enabled in settings). Runs after stream_end
                # so it doesn't block the stream.
                _maybe_schedule_title_refresh(s, put, agent)
        finally:
            # #4729: guaranteed-exit flush of any reasoning tail still buffered. On the
            # normal path the on_token/on_tool/post-run flushes already emptied it (no-op
            # here); on an exception or retry path that bypassed those, this emits the tail
            # before the outer handler sends apperror — so the live Thinking view never
            # loses its last coalesced chunk. Runs before stream teardown; STREAM_REASONING_TEXT
            # already mirrors the full text for persistence regardless.
            try:
                _flush_reasoning_buffer()
            except Exception:
                pass
            # Stop the live metering ticker
            _metering_stop.set()
            # Unregister the gateway approval callback and unblock any threads
            # still waiting on approval (e.g. stream cancelled mid-approval).
            if _approval_registered and _unreg_notify is not None:
                try:
                    _unreg_notify(session_id)
                except Exception:
                    logger.debug("Failed to unregister approval callback")
            if _cleanup_gateway_pending_mirror is not None:
                try:
                    _cleanup_gateway_pending_mirror()
                except Exception:
                    logger.debug("Failed to reconcile gateway approval mirror")
            if _clarify_registered and _unreg_clarify_notify is not None:
                try:
                    _unreg_clarify_notify(session_id)
                except Exception:
                    logger.debug("Failed to unregister clarify callback")
            with _ENV_LOCK:
                for _key, _old_value in old_profile_env.items():
                    if _old_value is None: os.environ.pop(_key, None)
                    else: os.environ[_key] = _old_value
                if old_cwd is None: os.environ.pop('TERMINAL_CWD', None)
                else: os.environ['TERMINAL_CWD'] = old_cwd
                if old_exec_ask is None: os.environ.pop('HERMES_EXEC_ASK', None)
                else: os.environ['HERMES_EXEC_ASK'] = old_exec_ask
                if old_session_key is None: os.environ.pop('HERMES_SESSION_KEY', None)
                else: os.environ['HERMES_SESSION_KEY'] = old_session_key
                if old_session_id is None: os.environ.pop('HERMES_SESSION_ID', None)
                else: os.environ['HERMES_SESSION_ID'] = old_session_id
                if old_session_platform is None: os.environ.pop('HERMES_SESSION_PLATFORM', None)
                else: os.environ['HERMES_SESSION_PLATFORM'] = old_session_platform
                if old_session_chat_id is None: os.environ.pop('HERMES_SESSION_CHAT_ID', None)
                else: os.environ['HERMES_SESSION_CHAT_ID'] = old_session_chat_id
                if old_hermes_home is None: os.environ.pop('HERMES_HOME', None)
                else: os.environ['HERMES_HOME'] = old_hermes_home

    except Exception as e:
        print('[webui] stream error:\n' + traceback.format_exc(), flush=True)
        err_str = str(e)
        # Sanitize HTML from provider error responses — some providers return
        # full HTML pages (e.g. nginx "404 page not found") instead of JSON errors.
        # Strip HTML tags to avoid rendering raw markup in the chat message.
        _stripped = re.sub(r'<[^>]+>', ' ', err_str)
        _stripped = re.sub(r'\s+', ' ', _stripped).strip()
        if _stripped != err_str:
            err_str = _stripped
        _exc_lower = err_str.lower()
        _classification = _classify_provider_error(err_str, e)
        _exc_is_credential_pool_empty = _classification['type'] == 'credential_pool_empty'
        if cancel_event.is_set():
            if s is not None:
                if _checkpoint_stop is not None:
                    _checkpoint_stop.set()
                if _ckpt_thread is not None:
                    _ckpt_thread.join(timeout=15)
                _lock_ctx = _agent_lock if _agent_lock is not None else contextlib.nullcontext()
                with _lock_ctx:
                    if (
                        not ephemeral
                        and _turn_pending_source == 'process_wakeup'
                        and _exc_is_credential_pool_empty
                    ):
                        record_process_wakeup_provider_unavailable_pause(
                            s,
                            classification=_classification['type'],
                            model=_turn_route_model,
                            provider=_turn_route_provider,
                        )
                    _finalize_cancelled_turn(s, ephemeral=ephemeral)
                    if not ephemeral:
                        try:
                            append_turn_journal_event_for_stream(
                                s.session_id,
                                stream_id,
                                {
                                    "event": "interrupted",
                                    "created_at": time.time(),
                                    "reason": "cancelled",
                                },
                            )
                        except Exception:
                            logger.debug("Failed to append cancelled turn journal event", exc_info=True)
            put('cancel', _cancel_event_payload('Cancelled by user'))
            return
        _exc_is_quota = _classification['type'] == 'quota_exhausted'
        # Exception quota text still includes: 'more credits' in _exc_lower, 'can only afford' in _exc_lower, 'fewer max_tokens' in _exc_lower.
        # Rate-limit detection remains guarded as: (not _exc_is_quota).
        _exc_is_rate_limit = (_classification['type'] == 'rate_limit') and (not _exc_is_quota)
        _exc_is_auth = _classification['type'] == 'auth_mismatch'  # detects '401' and 'unauthorized' via _classify_provider_error.
        _exc_is_not_found = _classification['type'] == 'model_not_found'  # detects '404', 'not found', 'does not exist', and 'invalid model'.
        _exc_is_cancelled = _classification['type'] == 'cancelled'
        _exc_is_interrupted = _classification['type'] == 'interrupted'
        _exc_is_compression_exhausted = _classification['type'] == 'compression_exhausted'

        # The user hint still points to Settings / `hermes model` from _classify_provider_error().
        if _exc_is_quota:
            _exc_label, _exc_type, _exc_hint = (
                _classification['label'], _classification['type'], _classification['hint'],
            )
        elif _exc_is_credential_pool_empty:
            _exc_label, _exc_type, _exc_hint = (
                _classification['label'], _classification['type'], _classification['hint'],
            )
        elif _exc_is_rate_limit:
            _exc_label, _exc_type, _exc_hint = (
                _classification['label'], _classification['type'], _classification['hint'],
            )
        elif _exc_is_auth:
            if not _self_healed:
                # ── Credential self-heal on 401 (#1401) ──
                _heal_rt = _attempt_credential_self_heal(
                    resolved_provider or '', session_id, _agent_lock,
                    target_model=resolved_model,
                )
                if _heal_rt is not None:
                    logger.info('[webui] self-heal (except path): retrying stream after credential refresh')
                    _self_healed = True
                    # Rebuild runtime variables
                    _rt = _heal_rt
                    resolved_api_key = _heal_rt.get('api_key')
                    if not resolved_provider:
                        resolved_provider = _heal_rt.get('provider')
                    resolved_base_url = _runtime_preferred_base_url(
                        _heal_rt, resolved_provider, configured_base_url
                    )
                    resolved_provider, resolved_api_key, resolved_base_url = _resolve_custom_provider_runtime_overrides(
                        resolved_provider, resolved_api_key, resolved_base_url
                    )
                    # Build a fresh agent with the new credentials
                    _heal_kwargs = dict(_agent_kwargs) if '_agent_kwargs' in dir() else {}
                    _heal_kwargs['api_key'] = resolved_api_key
                    _heal_kwargs['base_url'] = resolved_base_url
                    _heal_kwargs['model'] = resolved_model
                    _heal_kwargs['provider'] = resolved_provider
                    _replace_session_db_in_kwargs(_heal_kwargs, _state_db_path)
                    if 'credential_pool' in _agent_params:
                        _heal_kwargs['credential_pool'] = _heal_rt.get('credential_pool')
                    _heal_agent = _AIAgent(**_heal_kwargs)
                    if not attach_runtime_agent(stream_id, _heal_agent):
                        try:
                            _heal_agent.interrupt("Cancelled during agent replacement")
                        except Exception:
                            logger.debug("Failed to interrupt replacement agent")
                        return
                    from api.config import SESSION_AGENT_CACHE as _SAC2, SESSION_AGENT_CACHE_LOCK as _SAC2_L
                    with _SAC2_L:
                        _SAC2[session_id] = (_heal_agent, _agent_sig)
                        _SAC2.move_to_end(session_id)
                    # Retry the conversation
                    _token_sent = False
                    try:
                        _heal_kwargs2 = dict(
                            user_message=user_message,
                            system_message=workspace_system_msg,
                            conversation_history=_sanitize_messages_for_api(
                                _previous_context_messages,
                                cfg=_cfg,
                                effective_model=resolved_model,
                                effective_provider=resolved_provider,
                                effective_base_url=resolved_base_url,
                            ),
                            task_id=session_id,
                            persist_user_message=msg_text,
                        )
                        if moa_config is not None:
                            _heal_kwargs2["moa_config"] = moa_config
                        _heal_result = _heal_agent.run_conversation(**_heal_kwargs2)
                        # Retry succeeded — persist the result normally
                        if s is not None:
                            if _checkpoint_stop is not None:
                                _checkpoint_stop.set()
                            if _ckpt_thread is not None:
                                _ckpt_thread.join(timeout=15)
                            _lock_ctx = _agent_lock if _agent_lock is not None else contextlib.nullcontext()
                            with _lock_ctx:
                                if not ephemeral and not _stream_writeback_is_current(s, stream_id):
                                    logger.info(
                                        "Skipping stale stream self-heal writeback for session %s stream %s; active_stream_id=%s",
                                        getattr(s, 'session_id', session_id),
                                        stream_id,
                                        getattr(s, 'active_stream_id', None),
                                    )
                                    return
                                _result_messages = _heal_result.get('messages') or _previous_context_messages
                                _next_context_messages = _restore_reasoning_metadata(
                                    _previous_context_messages, _result_messages,
                                )
                                # Mint ids on the shared result rows BEFORE dedupe
                                # deep-copies any stale-user boundary row, so both
                                # arrays share the id (#5564).
                                _assign_stable_message_ids(
                                    _result_messages, _previous_messages, _previous_context_messages
                                )
                                _next_context_messages = _dedupe_replayed_context_messages(
                                    _previous_context_messages,
                                    _next_context_messages,
                                    msg_text,
                                )
                                s.context_messages = _deduplicate_context_messages(_next_context_messages)
                                s.messages = _merge_display_messages_after_agent_result(
                                    _previous_messages,
                                    _previous_context_messages,
                                    _restore_reasoning_metadata(_previous_messages, _result_messages),
                                    msg_text,
                                    source=getattr(s, 'pending_user_source', None) or 'webui',
                                )
                                _advance_truncation_watermark_after_commit(s)  # #3831
                                s.save()
                        logger.info('[webui] self-heal (except path): retry succeeded')
                        return  # skip error emission
                    except Exception as _retry_exc2:
                        logger.warning('[webui] self-heal (except path): retry failed: %s', _retry_exc2)
                        # Fall through to emit the original error
            # Self-heal didn't apply or retry failed — emit the auth error
            _exc_label, _exc_type, _exc_hint = (
                'Authentication error', 'auth_mismatch',
                'The selected model may not be supported by your configured provider. '
                'Run `hermes model` in your terminal to switch providers, then restart the WebUI.',
            )
        elif _exc_is_not_found:
            _exc_label, _exc_type, _exc_hint = (
                _classification['label'], _classification['type'], _classification['hint'],
            )
        elif _exc_is_cancelled or _exc_is_interrupted:
            _exc_label, _exc_type, _exc_hint = (
                _classification['label'], _classification['type'], _classification['hint'],
            )
        elif _exc_is_compression_exhausted:
            _exc_label, _exc_type, _exc_hint = (
                _classification['label'], _classification['type'], _classification['hint'],
            )
        else:
            _exc_label, _exc_type, _exc_hint = 'Error', 'error', ''

        _error_payload = _provider_error_payload(err_str, _exc_type, _exc_hint)
        if s is not None:
            if _checkpoint_stop is not None:
                _checkpoint_stop.set()
            if _ckpt_thread is not None:
                _ckpt_thread.join(timeout=15)
            # Persist the error so it survives page reload.
            # _error=True ensures _sanitize_messages_for_api excludes it from subsequent
            # API calls so the LLM never sees its own error as prior context on the next turn.
            _lock_ctx = _agent_lock if _agent_lock is not None else contextlib.nullcontext()
            with _lock_ctx:
                if not ephemeral and not _stream_writeback_is_current(s, stream_id):
                    if _turn_pending_source == 'process_wakeup':
                        _pause = record_process_wakeup_provider_unavailable_pause(
                            s,
                            classification=_exc_type,
                            model=_turn_route_model,
                            provider=_turn_route_provider,
                        )
                        if _pause is not None:
                            try:
                                s.save(touch_updated_at=False)
                            except Exception:
                                logger.debug(
                                    "Failed to persist stale-stream process_wakeup pause for session %s",
                                    getattr(s, 'session_id', session_id),
                                    exc_info=True,
                                )
                    logger.info(
                        "Skipping stale stream error writeback for session %s stream %s; active_stream_id=%s",
                        getattr(s, 'session_id', session_id),
                        stream_id,
                        getattr(s, 'active_stream_id', None),
                    )
                    return

                if _turn_pending_source == 'process_wakeup':
                    _recorded_pause = record_process_wakeup_provider_unavailable_pause(
                        s,
                        classification=_exc_type,
                        model=_turn_route_model,
                        provider=_turn_route_provider,
                    )
                    # #3929 UX: disclose the pause in the error card ONLY when a
                    # pause was actually recorded (credential-pool exhaustion),
                    # keeping the SSE payload hint in sync with the persisted bubble.
                    if _recorded_pause:
                        _exc_hint = (
                            (_exc_hint + ' ' if _exc_hint else '')
                            + 'Automatic retries for this conversation are paused until you '
                            + 'send a message, switch the model/provider, or fix the credentials.'
                        )
                        _error_payload['hint'] = _exc_hint
                _materialize_pending_user_turn_before_error(s)
                s.active_stream_id = None
                s.pending_user_message = None
                s.pending_attachments = []
                s.pending_started_at = None
                s.pending_user_source = None
                try:
                    _snapshot_and_append_partial_on_error(s, stream_id)
                except Exception:
                    logger.debug("Failed to snapshot partials on error for %s", stream_id, exc_info=True)
                _error_message = {
                    'role': 'assistant',
                    'content': f'**{_exc_label}:** {_error_payload.get("message") or err_str}' + (f'\n\n*{_exc_hint}*' if _exc_hint else ''),
                    'timestamp': int(time.time()),
                    '_error': True,
                }
                if _exc_type == 'compression_exhausted':
                    _recovery = stamp_compression_exhausted_recovery(
                        s,
                        message=_error_payload.get('message') or err_str,
                        details=_error_payload.get('details') or '',
                    )
                    _error_message['_compressionRecovery'] = _recovery
                    _error_payload['compression_recovery'] = _recovery
                    _error_payload['recommended_recovery_action'] = _recovery.get('recommended_action')
                if _error_payload.get('details'):
                    _error_message['provider_details'] = _error_payload['details']
                if _exc_type == 'cancelled':
                    _error_message['provider_details_label'] = 'Cancellation details'
                elif _exc_type == 'interrupted':
                    _error_message['provider_details_label'] = 'Interruption details'
                s.messages.append(_error_message)
                try:
                    s.save()
                except Exception:
                    pass
                if not ephemeral:
                    try:
                        append_turn_journal_event_for_stream(
                            s.session_id,
                            stream_id,
                            {
                                "event": "interrupted",
                                "created_at": time.time(),
                                "reason": _exc_type,
                            },
                        )
                    except Exception:
                        logger.debug("Failed to append interrupted turn journal event", exc_info=True)
            _error_payload['session_id'] = getattr(s, 'session_id', session_id)
            _error_payload['old_session_id'] = session_id
        put('apperror', _error_payload)
    finally:
        # #4633/#2476: symmetric metering teardown. begin_session() (top of the
        # outer try) had no paired end_session(), so zero-token turns leaked a
        # _sessions[stream_id] entry that get_stats() pruning never reclaims (its
        # criterion requires first_token_ts > 0). end_session() is idempotent —
        # it just pops _sessions[stream_id]; the metering payload is unchanged.
        # _metering_stop.set() deterministically stops the ticker (the inner
        # finally also sets it on the normal path; setting twice is harmless).
        try:
            # 0: end_session() currently ignores final_output_tokens — it only
            # pops _sessions[stream_id]. If it is ever extended to consume the
            # count (e.g. persisting final output tokens to a billing ledger),
            # this teardown caller will need to supply the real total; the outer
            # finally doesn't have easy access to it today.
            meter().end_session(stream_id, 0)
        except Exception:
            logger.debug("Failed to end metering session for stream %s", stream_id, exc_info=True)
        _metering_stop.set()
        # Stop the periodic checkpoint thread before the final recovery path.
        # The checkpoint thread also uses the per-session lock; joining it first
        # avoids contending with checkpoint writes during stale-pending repair.
        if _checkpoint_stop is not None:
            _checkpoint_stop.set()
        if _ckpt_thread is not None:
            _ckpt_thread.join(timeout=15)
        if (s is not None
                and getattr(s, 'active_stream_id', None) == stream_id
                and getattr(s, 'pending_user_message', None)):
            update_active_run(stream_id, phase="finalizing")
            _last_resort_sync_from_core(s, stream_id, _agent_lock)
        _clear_thread_env()  # TD1: always clear thread-local context
        if _streaming_cron_profile_home_token is not None:
            _STREAMING_CRON_PROFILE_HOME.reset(_streaming_cron_profile_home_token)
        # xsession wakeup misroute root fix (Option 1): restore the per-turn
        # session-identity context-locals (reset-token semantics). MUST run on
        # every exit path so a reused thread-pool worker leaks no identity and
        # CLI/cron env fallback resumes — same lifecycle slot as the env
        # restore above.
        _reset_turn_session_identity(_turn_session_identity_tokens)
        execution.finish()
        # NOTE: do NOT discard PENDING_GOAL_CONTINUATION here. The marker
        # is set by goal_continue inside the SAME function call and consumed
        # atomically by `_start_chat_stream_for_session` when the next stream
        # starts. Discarding here would race ahead of the frontend round-trip
        # and break the goal-continuation chain.

        # ── Defer-path fix: turn-teardown idle-hook ────────────────────────
        # The session has just transitioned active→idle: unregister_active_run
        # above cleared this stream's ACTIVE_RUNS row (under ACTIVE_RUNS_LOCK,
        # independent of STREAMS_LOCK), so _session_has_active_turn() is now
        # False for this session unless a *different* stream is still active
        # (cancel/reconnect — drain_deferred_wakeups_for_session guards on
        # that and leaves the marker for the later teardown). A FAST
        # background task that completed while this turn was tearing down was
        # deferred by api/background_process._process_one (it could not start
        # a turn → would 409) and its wakeup_prompt persisted in
        # DEFERRED_PROCESS_WAKEUPS. For an autonomous agent there is no next
        # user turn, so the PR #2279 next-turn drain never runs; without this
        # hook the deferred wakeup is lost forever (the Test B failure). This
        # makes the busy-at-completion case symmetric with the idle case:
        # idle now → fire now (Option Z idle branch); busy now → fire here at
        # turn-end. claim_deferred_wakeups pops atomically, so this is
        # idempotent with the next-turn drain (no double-fire) and the wakeup
        # turn's own teardown finds nothing claimed (no wakeup loop). The
        # drain spawns its own daemon thread, so teardown never blocks.
        try:
            from api.background_process import drain_deferred_wakeups_for_session

            drain_deferred_wakeups_for_session(session_id)
        except Exception:
            logger.debug(
                "turn-teardown deferred-wakeup drain failed for session %s",
                session_id,
                exc_info=True,
            )
