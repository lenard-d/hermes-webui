"""Complete local-agent run orchestration for the streaming facade.

This module owns one admitted local run from TurnExecution startup through
journal publication, transcript persistence, recovery, terminal projection,
and idempotent cleanup. Keep those phases together: splitting callbacks or
settlement from this lifecycle would separate state decisions from their owner.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from api.config import (
    clear_thread_env,
    attach_runtime_agent,
    resolve_model_provider,
    update_active_run,
)
from api.metering import meter
from api.session_state import session_agent_lock as _get_session_agent_lock
from api.sessions.session_cache_repository import get_session
from api.sessions.process_wakeup import (
    clear_process_wakeup_pause,
)
from .agent_cache import (
    _attempt_credential_self_heal,
    _build_session_db_for_stream,
    _last_resort_sync_from_core,
)
from .agent_loader import _get_ai_agent
from .diagnostics import (
    _STREAMING_CRON_PROFILE_HOME,
    _stream_writeback_stage,
)
from .payloads import (
    _cancel_event_payload,
    _session_payload_with_full_messages,
)
from .provider_errors import _classify_provider_error
from .runtime_resolution import (
    _apply_profile_home_context_to_streaming_model,
)
from .terminal_outcomes import (
    _cleanup_ephemeral_cancelled_turn,
    _finalize_cancelled_turn,
)
from .title_generation.lifecycle import _maybe_schedule_title_refresh
from .turn_context import (
    _stream_writeback_can_supersede_recovery_marker,
    _stream_writeback_is_current,
)
from .turn_identity import _reset_turn_session_identity, _set_turn_session_identity
from .webui_prefill import (
    _load_webui_prefill_context,
    _normalize_prefill_messages_before_user_turn,
    _prefill_messages_with_webui_context,
)
from api.turn_journal import append_turn_journal_event_for_stream

from .local_agent_runtime import LocalAgentRequest, PreparedLocalAgent
from .local_checkpoint import LocalCheckpoint
from .local_compression import LocalCompressionOwner
from .local_conversation import LocalConversation
from .local_environment import LocalRunEnvironment
from .local_failures import LocalFailureContext, LocalFailureOwner
from .local_interactions import LocalInteractionBridge
from .local_model_lease import SessionModelLease
from .local_profile import LocalProfileContext
from .local_result import merge_local_result
from .local_success import (
    LocalSuccessProjection,
    publish_persistent_state_changes,
    publish_post_turn_controls,
    sync_success_to_insights,
)
from .local_events import LocalEventTranslator
from .local_usage import LocalUsageTracker
from .execution import TurnExecution


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LocalRunDependencies:
    """Narrow seams that the public package may replace for an embedded run."""

    get_session: Callable
    get_ai_agent: Callable
    resolve_model_provider: Callable
    get_session_agent_lock: Callable
    build_session_db_for_stream: Callable
    attempt_credential_self_heal: Callable
    load_webui_prefill_context: Callable
    prefill_messages_with_webui_context: Callable
    normalize_prefill_messages_before_user_turn: Callable
    classify_provider_error: Callable
    session_payload_with_full_messages: Callable
    maybe_schedule_title_refresh: Callable


_DEFAULT_DEPENDENCIES = LocalRunDependencies(
    get_session=get_session,
    get_ai_agent=_get_ai_agent,
    resolve_model_provider=resolve_model_provider,
    get_session_agent_lock=_get_session_agent_lock,
    build_session_db_for_stream=_build_session_db_for_stream,
    attempt_credential_self_heal=_attempt_credential_self_heal,
    load_webui_prefill_context=_load_webui_prefill_context,
    prefill_messages_with_webui_context=_prefill_messages_with_webui_context,
    normalize_prefill_messages_before_user_turn=_normalize_prefill_messages_before_user_turn,
    classify_provider_error=_classify_provider_error,
    session_payload_with_full_messages=_session_payload_with_full_messages,
    maybe_schedule_title_refresh=_maybe_schedule_title_refresh,
)


def run_agent_streaming(
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
    dependencies: LocalRunDependencies | None = None,
):
    """Run agent in background thread, writing SSE events to STREAMS[stream_id].

    When ephemeral=True, session mutations are skipped — used by /btw to get
    a streaming answer without persisting to the parent session.
    """

    dependencies = dependencies or _DEFAULT_DEPENDENCIES
    get_session = dependencies.get_session
    _get_ai_agent = dependencies.get_ai_agent
    resolve_model_provider = dependencies.resolve_model_provider
    _get_session_agent_lock = dependencies.get_session_agent_lock
    _build_session_db_for_stream = dependencies.build_session_db_for_stream
    _attempt_credential_self_heal = dependencies.attempt_credential_self_heal
    _load_webui_prefill_context = dependencies.load_webui_prefill_context
    _prefill_messages_with_webui_context = dependencies.prefill_messages_with_webui_context
    _normalize_prefill_messages_before_user_turn = dependencies.normalize_prefill_messages_before_user_turn
    _classify_provider_error = dependencies.classify_provider_error
    _session_payload_with_full_messages = dependencies.session_payload_with_full_messages
    _maybe_schedule_title_refresh = dependencies.maybe_schedule_title_refresh
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
    run_environment = LocalRunEnvironment()
    checkpoint = LocalCheckpoint(session_id=session_id, logger=logger)

    # MCP discovery moved to AFTER the per-profile HERMES_HOME mutation below
    # (was here at v0.51.30) — the previous placement always read the default
    # profile's mcp_servers because os.environ['HERMES_HOME'] hadn't been
    # rewritten yet.  See https://github.com/nesquena/hermes-webui/issues/1968.

    agent = None
    usage_tracker = LocalUsageTracker(
        session_id=session_id,
        session_getter=lambda: s,
        agent_getter=lambda: agent,
    )

    def _live_usage_snapshot():
        return usage_tracker.snapshot()

    def _bump_live_prompt_estimate(messages):
        return usage_tracker.add_tool_messages(messages)

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

    event_translator = LocalEventTranslator(
        session_id=session_id,
        stream_id=stream_id,
        publish=put,
        usage=usage_tracker,
        agent_params=lambda: _agent_params,
    )
    _agent_status_callback = event_translator.status
    _captured_terminal_error = event_translator.captured_terminal_error

    failure_context = LocalFailureContext(
        session=None,
        session_id=session_id,
        stream_id=stream_id,
        message_text=msg_text,
        pending_source="webui",
        route_model=_turn_route_model,
        route_provider=_turn_route_provider,
        ephemeral=ephemeral,
        cancel_event=cancel_event,
        checkpoint=checkpoint,
        session_lock=None,
        publish=put,
        classify=_classify_provider_error,
        session_payload=_session_payload_with_full_messages,
        credential_self_heal=_attempt_credential_self_heal,
        prepared_agent=None,
        conversation=None,
        event_translator=event_translator,
        previous_messages=[],
        previous_context_messages=[],
        logger=logger,
    )
    failure_owner = LocalFailureOwner(failure_context)

    # xsession wakeup misroute root fix (Option 1): pre-init so the outer
    # finally can always reset even if an exception fires before the bind.
    # Placed ABOVE the _checkpoint_stop cluster so that cluster stays adjacent
    # to the `try:` (preserves the Issue #765 static-locator invariant).
    _turn_session_identity_tokens = None
    _streaming_cron_profile_home_token = None
    _turn_pending_source = 'webui'
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
        provider_context = (
            str(model_provider).strip().lower()
            if model_provider is not None
            else getattr(s, "model_provider", None)
        )
        provider_context = str(provider_context).strip().lower() if provider_context else None
        _agent_lock = _get_session_agent_lock(session_id)
        model_lease = SessionModelLease.claim(
            s,
            _agent_lock,
            model=model,
            provider=provider_context,
        )
        failure_context.session = s
        failure_context.pending_source = _turn_pending_source
        failure_context.session_lock = _agent_lock

        # TD1: set thread-local env context so concurrent sessions don't clobber globals
        # Check for pre-flight cancel (user cancelled before agent even started)
        if cancel_event.is_set():
            with _agent_lock:
                _finalize_cancelled_turn(s, ephemeral=ephemeral, message='Task cancelled before start.')
            put('cancel', _cancel_event_payload('Cancelled before start'))
            return

        profile = LocalProfileContext.resolve(s, logger=logger)
        _profile_home = profile.home
        _resolved_profile_name = profile.resolved_name
        _streaming_cron_profile_home_token = _STREAMING_CRON_PROFILE_HOME.set(
            _profile_home
        )

        # Enrich the worker's route choice from the owning profile, then update
        # the session only while its model lease is still current.
        model, provider_context, _repaired = _apply_profile_home_context_to_streaming_model(
            model=model,
            provider_context=provider_context,
            profile_home=_profile_home,
            has_profile=bool(getattr(s, "profile", None)),
        )
        provider_context = str(provider_context).strip().lower() if provider_context else None
        model_lease.apply_profile_resolution(
            model=model,
            provider=provider_context,
            repaired=_repaired,
        )
        profile.enter_environment(
            run_environment,
            session_id=session_id,
            workspace=str(s.workspace),
        )

        interactions = LocalInteractionBridge(
            session_id=session_id,
            cancel_event=cancel_event,
            publish=put,
            logger=logger,
        )
        interactions.open()

        try:
            _flush_reasoning_buffer = event_translator.flush_reasoning
            _reasoning_segments = event_translator.reasoning_segments
            _live_tool_calls = event_translator.live_tool_calls
            _checkpoint_activity = event_translator.checkpoint_activity
            _self_healed = False

            prepared_agent = PreparedLocalAgent.prepare(
                LocalAgentRequest(
                    session=s,
                    session_id=session_id,
                    model=model,
                    provider=provider_context,
                    profile_home=_profile_home,
                    profile_name=_resolved_profile_name,
                    ephemeral=ephemeral,
                    callbacks=event_translator,
                    clarify_callback=interactions.clarify,
                    publish=put,
                    get_ai_agent=_get_ai_agent,
                    resolve_model_provider=resolve_model_provider,
                    build_session_db=_build_session_db_for_stream,
                    load_prefill_context=_load_webui_prefill_context,
                    build_prefill_messages=_prefill_messages_with_webui_context,
                    normalize_prefill_messages=(
                        _normalize_prefill_messages_before_user_turn
                    ),
                ),
                logger=logger,
            )
            agent = prepared_agent.agent
            _AIAgent = prepared_agent.agent_class
            _agent_sig = prepared_agent.signature
            _session_db = prepared_agent.session_db
            _state_db_path = prepared_agent.state_db_path
            resolved_model = prepared_agent.model
            resolved_provider = prepared_agent.provider
            resolved_base_url = prepared_agent.base_url
            resolved_api_key = prepared_agent.api_key
            _rt = prepared_agent.runtime
            _cfg = prepared_agent.config
            _agent_params = prepared_agent.parameters
            _agent_kwargs = prepared_agent.kwargs
            failure_context.prepared_agent = prepared_agent


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

            conversation = LocalConversation.prepare(
                session=s,
                session_id=session_id,
                stream_id=stream_id,
                message_text=msg_text,
                attachments=attachments,
                workspace=workspace,
                agent=agent,
                config=_cfg,
                resolved_model=resolved_model,
                resolved_provider=resolved_provider,
                resolved_base_url=resolved_base_url,
                profile_home=_profile_home,
                checkpoint=checkpoint,
                checkpoint_activity=_checkpoint_activity,
                session_lock=_agent_lock,
                moa_config=moa_config,
            )
            _previous_messages = conversation.previous_messages
            _previous_context_messages = conversation.previous_context_messages
            workspace_system_msg = conversation.system_message
            _turn_started_at = conversation.started_at
            _pre_compression_count = conversation.pre_compression_count
            _persistent_state_before = conversation.persistent_state_before
            failure_context.conversation = conversation
            failure_context.previous_messages = _previous_messages
            failure_context.previous_context_messages = _previous_context_messages
            result = conversation.run(agent)
            # #4729: the run is done — flush any reasoning tail still in the coalescing
            # buffer (the agent never calls reasoning_callback(None), and a turn can end on
            # reasoning with no trailing token/tool boundary to trigger a flush) so the last
            # sub-100ms window reaches the live Thinking view before the terminal done event.
            _flush_reasoning_buffer()
            if cancel_event.is_set():
                checkpoint.close()
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
                checkpoint.close()
                try:
                    import pathlib
                    pathlib.Path(s.path).unlink(missing_ok=True)
                except Exception:
                    pass
                return  # skip all normal persistence for ephemeral sessions
            checkpoint.close()
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
                            logger.debug(
                                "Failed to append cancelled turn journal event",
                                exc_info=True,
                            )
                        put("cancel", _cancel_event_payload("Cancelled by user"))
                        return
                    merged_result = merge_local_result(
                        s,
                        result,
                        previous_messages=_previous_messages,
                        previous_context_messages=_previous_context_messages,
                        message_text=msg_text,
                    )
                    result = merged_result.result
                    _result_messages = merged_result.messages
                    _tool_limit_reached = merged_result.tool_limit_reached
                compression = LocalCompressionOwner(
                    original_session_id=session_id,
                    profile_name=_resolved_profile_name,
                    agent=agent,
                    session_lock=_agent_lock,
                    logger=logger,
                )
                compression.rotate_if_needed(s)
                _compression_origin_session_id = compression.original_session_id
                _compression_continuation_session_id = (
                    compression.continuation_session_id
                )
                _compressed = compression.compressed

                failure = failure_owner.inspect_terminal_result(
                    result,
                    agent=agent,
                    tool_limit_reached=_tool_limit_reached,
                    captured_terminal_error=_captured_terminal_error,
                    compression_origin_id=_compression_origin_session_id,
                    compression_continuation_id=(
                        _compression_continuation_session_id
                    ),
                )
                if failure.handled:
                    return
                result = failure.result
                agent = failure.agent
                resolved_provider = failure.provider
                resolved_base_url = failure.base_url
                resolved_api_key = failure.api_key
                _tool_limit_reached = failure.tool_limit_reached

                compression.detect_compressor_change(
                    previous_count=_pre_compression_count
                )
                compression.project(
                    s,
                    previous_messages=_previous_messages,
                    system_message=workspace_system_msg,
                    publish=put,
                    usage_snapshot=_live_usage_snapshot,
                )
                _compressed = compression.compressed
                _compression_continuation_session_id = (
                    compression.continuation_session_id
                )

                success = LocalSuccessProjection.apply(
                    s,
                    agent=agent,
                    result=result,
                    route_model=model,
                    resolved_model=resolved_model,
                    resolved_provider=resolved_provider,
                    resolved_base_url=resolved_base_url,
                    resolved_api_key=resolved_api_key,
                    config=_cfg,
                    previous_messages=_previous_messages,
                    reasoning_segments=_reasoning_segments,
                    live_tool_calls=_live_tool_calls,
                    attachments=attachments,
                    message_text=msg_text,
                    turn_started_at=_turn_started_at,
                    stream_id=stream_id,
                )
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
                        from api.sessions import mark_turn_completed
                        mark_turn_completed(s.session_id, agent=agent)
                    except Exception:
                        logger.debug("Memory lifecycle mark failed for session %s", s.session_id, exc_info=True)
                with _stream_writeback_stage(_writeback_timings, "persistent_state_scan"):
                    publish_persistent_state_changes(
                        session=s,
                        session_id=session_id,
                        profile_home=_profile_home,
                        before=_persistent_state_before,
                        publish=put,
                        logger=logger,
                    )
            with _stream_writeback_stage(_writeback_timings, "state_sync"):
                sync_success_to_insights(s, agent=agent, model=model, logger=logger)
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
            publish_post_turn_controls(
                s,
                agent=agent,
                session_id=session_id,
                profile_home=_profile_home,
                goal_related=goal_related,
                publish=put,
                logger=logger,
            )
            success.publish_terminal(
                s,
                session_id=session_id,
                stream_id=stream_id,
                agent=agent,
                publish=put,
                payload_builder=_session_payload_with_full_messages,
                tool_limit_reached=_tool_limit_reached,
                maybe_schedule_title_refresh=_maybe_schedule_title_refresh,
                writeback_timings=_writeback_timings,
                writeback_started=_writeback_started,
                logger=logger,
            )
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
            interactions.close()
            run_environment.close()

    except Exception as e:
        print("[webui] stream error:\n" + traceback.format_exc(), flush=True)
        failure_owner.handle_exception(e, agent=agent)
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
        # Preserve the stop-before-recovery invariant at the public lifecycle seam.
        _checkpoint_stop = checkpoint.stop_event
        _checkpoint_stop.set()
        checkpoint.close()
        if (s is not None
                and getattr(s, 'active_stream_id', None) == stream_id
                and getattr(s, 'pending_user_message', None)):
            update_active_run(stream_id, phase="finalizing")
            _last_resort_sync_from_core(s, stream_id, _agent_lock)
        clear_thread_env()  # TD1: always clear thread-local context
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
