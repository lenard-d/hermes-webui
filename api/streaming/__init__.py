"""
Hermes Web UI -- SSE streaming engine and agent thread runner.
Includes Sprint 10 cancel support via CANCEL_FLAGS.
"""
# The package interface intentionally re-exports the established streaming API.
# ruff: noqa: F401
import contextlib
import contextvars
import json
import logging
import os
import queue
import random
import re
import sqlite3
import shlex  # noqa: F401 -- legacy package interface
import subprocess  # noqa: F401 -- legacy package interface
import threading
import time
import traceback  # noqa: F401 -- late-bound local-run facade seam
import copy
from pathlib import Path
from typing import Optional

from .agent_cache import (
    _adopt_session_db_for_cached_agent,
    _agent_cache_api_key_sig,
    _attempt_credential_self_heal,
    _build_session_db_for_stream,
    _cached_agent_matches_session,
    _cached_agent_session_identity,
    _close_cached_agent_entry_at_session_boundary,
    _close_evicted_agent_at_session_boundary,
    _last_resort_sync_from_core,
    _refresh_cached_agent_primary_runtime_snapshot,
    _refresh_cached_agent_runtime,
    _replace_session_db_in_kwargs,
    _session_db_is_open,
)
from .agent_loader import _clarify_timeout_seconds, _get_ai_agent, _prewarm_skill_tool_modules
from .attachments import (
    _IMAGE_MAGIC,
    _NATIVE_IMAGE_MAX_BYTES,
    _attachment_name,
    _is_valid_image,
    _explicit_text_signal,
    _resolve_image_input_mode,
    _build_native_multimodal_message,
)
from .compression_anchors import (
    _is_context_compression_marker,
    _compact_summary_text,
    _compression_anchor_message_key,
    _compression_summary_from_messages,
    _find_current_user_turn,
    _drop_checkpointed_current_user_from_context,
)
from .compression_snapshot import _preserve_pre_compression_snapshot
from .context_replay import (
    _session_context_messages,
    _message_identity,
    _messages_have_prefix,
    _message_replay_key,
    _strip_replayed_prefix,
    _looks_like_replayed_session_arc_summary,
    _strip_replayed_context_items,
    _dedupe_replayed_context_messages,
    _dedupe_replayed_active_context,
)
from .diagnostics import (
    _ENV_LOCK,
    _STREAMING_CRON_PROFILE_HOME,
    _install_streaming_cronjob_profile_wrapper,
    _log_stream_writeback_timings,
    _stream_writeback_diag_threshold_seconds,
    _stream_writeback_stage,
)
from .gateway_routing_metadata import (
    _clean_gateway_routing_scalar,
    _find_gateway_metadata_payload,
    _normalize_gateway_routing_metadata,
    _extract_gateway_routing_metadata,
)
from .live_controls import (
    _handle_chat_steer,
    cancel_stream,
)
from .message_sanitization import (
    _API_SAFE_MSG_KEYS,
    _OOB_USER_MESSAGE_BLOCK_RE,
    _strip_native_image_parts_from_content,
    _strip_oob_blocks,
    _content_has_reasoning_only_parts,
    _is_reasoning_only_assistant_message,
    _is_local_reasoning_replay_base_url,
    _should_strip_reasoning_content,
    _sanitize_messages_for_api,
    _api_safe_message_positions,
    _deduplicate_context_messages,
    _assign_stable_message_ids,
)
from .payloads import (
    _session_payload_with_full_messages,
    _compact_for_echo_compare,
    _strip_compact_echo_suffix,
    _redacted_session_payload_with_full_messages,
    _cancel_event_payload,
)
from .post_compression_context import (
    _POST_COMPRESSION_TOOL_RESULT_MARKER,
    _POST_COMPRESSION_TOOL_RESULT_MIN_SNIPPET_TOKENS,
    _POST_COMPRESSION_TOOL_RESULT_SUMMARY_FLAG,
    _POST_COMPRESSION_TOOL_RESULT_TOTAL_TOKENS,
    _ROUGH_TOKEN_CHARS,
    _positive_int_value,
    _rough_text_token_count,
    _post_compression_tool_result_budget,
    _compressed_context_tool_result_summary,
    _is_compressed_context_tool_result_summary_message,
    _hard_prune_post_compression_tool_results,
    _prune_context_tool_results_after_compression,
    _estimate_post_compression_context_tokens,
    _restore_reasoning_metadata,
    _restore_display_reasoning_metadata,
)
from .process_notifications import (
    _accept_pending_async_delegations,
    _drain_webui_process_notifications,
)
from .prompts import _WEBUI_PROGRESS_PROMPT, _webui_ephemeral_system_prompt, _webui_surface_context_prompt
from .provider_errors import (
    _is_quota_error_text,
    _provider_error_probe_text,
    _classify_provider_error,
    _provider_error_payload,
)
from .runtime_resolution import (
    _file_signature,
    _persistent_state_snapshot,
    _persistent_state_changes,
    _apply_profile_provider_context_to_streaming_model,
    _apply_profile_home_context_to_streaming_model,
    _resolve_custom_provider_runtime_overrides,
    _same_base_url_endpoint,
    _runtime_preferred_base_url,
    _is_fallback_lifecycle_message,
    _is_agent_compression_start_status,
)
from .stale_user_context import (
    _strip_workspace_prefixes_for_compare,
    _normalize_user_text,
    _raw_message_text,
    _stale_user_tail_candidate,
    _last_user_row,
    _stale_prefix_matches_prior_user_context,
    _detect_stale_user_merge,
    _strip_stale_user_merge_from_messages,
)
from .terminal_outcomes import (
    _MAX_ITERATION_SUMMARY_REQUEST,
    _is_synthetic_max_iteration_summary_request,
    _drop_synthetic_max_iteration_summary_requests,
    _is_synthetic_control_message,
    _drop_synthetic_control_messages,
    _agent_result_tool_limit_reached,
    _maybe_inject_max_iteration_summary_fallback,
    _mark_latest_assistant_tool_limit_status,
    _session_has_cancel_marker,
    _cancelled_turn_content,
    _persist_cancelled_turn,
    _cleanup_ephemeral_cancelled_turn,
    _finalize_cancelled_turn,
    _aiagent_import_error_detail,
)
from .terminal_copy import (
    _cancelled_turn_hint,
    _preferred_agent_display_name,
    _preferred_agent_display_name_for_session,
)
from .thinking_content import (
    _INLINE_THINKING_TAG_PAIRS,
    _inline_thinking_fence_marker_at,
    _next_inline_thinking_opener,
    _text_tail_is_partial_opener,
    _line_is_indented_code,
    _merge_inline_thinking_reasoning,
    _extract_inline_thinking_from_content,
    _split_thinking_from_content,
    _strip_thinking_markup,
    _strip_xml_tool_calls,
    _sanitize_generated_title,
    _looks_invalid_generated_title,
    _structured_visible_text,
    _message_content_part_text,
    _message_text,
    _assistant_content_part_is_tool_use,
    _assistant_message_has_final_visible_text,
)
from .title_generation import (
    _first_exchange_snippets,
    _latest_exchange_snippets,
    _count_exchanges,
    _get_title_refresh_interval,
    _is_provisional_title,
    _detect_title_language,
    _script_counts,
    _dominant_script,
    _title_prompt_language_rule,
    _title_language_mismatch,
    _title_prompts,
    _is_minimax_route,
    _route_rejects_reasoning_extra,
    _get_aux_title_config,
    _aux_title_configured,
    _aux_title_timeout,
    _title_completion_budget,
    _title_retry_completion_budget,
    _title_retry_status,
    _title_should_skip_remaining_attempts,
    _safe_obj_value,
    _safe_text_value,
    _extract_title_response,
    generate_title_raw_via_aux,
    generate_title_raw_via_agent,
    _generate_llm_session_title_for_agent,
    _generate_llm_session_title_via_aux,
    _put_title_status,
    _fallback_title_from_exchange,
    _is_generic_fallback_title,
    _run_background_title_update,
    _run_background_title_refresh,
    generate_session_title_for_session,
    _maybe_schedule_title_refresh,
)
from .turn_context import (
    _save_streaming_checkpoint,
    _normalize_fresh_chat_text,
    _is_casual_fresh_chat_message,
    _has_task_resume_compaction_marker,
    _new_turn_context_from_messages,
    _context_messages_for_new_turn,
    _stream_writeback_is_current,
    _stream_writeback_can_supersede_recovery_marker,
    _advance_truncation_watermark_after_commit,
)
from .tool_events import (
    _TOOL_ARG_CONTENT_CAP,
    _TOOL_ARG_CONTENT_KEYS,
    _extract_tool_calls_from_messages,
    _live_usage_session_snapshot,
    _partial_marker_already_present,
    _truncate_tool_args,
    _tool_result_snippet,
    live_usage_prompt_estimate_after_tool_delta,
)
from .transcript import (
    _agent_result_terminal_failure,
    _assistant_reply_added_after_current_turn,
    _build_partial_message,
    _has_new_assistant_reply,
    _materialize_pending_user_turn_before_error,
    _merge_display_messages_after_agent_result,
    _merged_transcript_lacks_final_assistant_answer,
    _session_lacks_final_assistant_answer,
    _snapshot_and_append_partial_on_error,
    _stamp_missing_message_timestamps,
    _turn_transcript_lacks_final_assistant_answer,
)
from .turn_identity import (
    _bind_turn_session_identity,
    _build_agent_thread_env,
    _reset_turn_session_identity,
    _set_turn_session_identity,
)
from .transport import (
    SSE_HEARTBEAT_INTERVAL_SECONDS,
    SSE_WRITE_DEADLINE_SECONDS,
    _SSE_HEARTBEAT_INTERVAL_SECONDS,
    _sse,
    _sse_set_write_deadline,
)
from api.workspace_context import (
    _LEGACY_WORKSPACE_PREFIX_ANY_RE,
    _WORKSPACE_PREFIX_ANY_RE,
    _looks_like_current_user_turn,
    _strip_workspace_prefix,
    _workspace_context_prefix,
)
from .webui_prefill import (
    PREFILL_CONTEXT_DEFAULT_MAX_CHARS as _PREFILL_CONTEXT_DEFAULT_MAX_CHARS,
    PREFILL_SCRIPT_OUTPUT_LIMIT as _PREFILL_SCRIPT_OUTPUT_LIMIT,
    SECRET_SHAPED_RE as _SECRET_SHAPED_RE,
    _redact_prefill_status_text,
    _valid_prefill_messages,
    _resolve_prefill_path,
    _prefill_context_max_chars,
    _prefill_context_char_count,
    _budget_compacted_prefill_context,
    _apply_prefill_context_budget,
    _prefill_not_configured,
    _load_prefill_messages_file,
    _prefill_script_timeout,
    _prefill_script_command,
    _messages_from_prefill_script_output,
    _load_prefill_messages_script,
    _load_webui_prefill_context,
    _public_prefill_context_status,
    _webui_delivery_context_prompt,
    _prefill_messages_with_webui_context,
    _normalize_prefill_messages_before_user_turn,
)

logger = logging.getLogger(__name__)

from api.config import (  # noqa: F401 -- late-bound local-run facade seams
    get_config,
    STREAMS as STREAMS, STREAMS_LOCK as STREAMS_LOCK,
    CANCEL_FLAGS, AGENT_INSTANCES as AGENT_INSTANCES,
    STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT,
    STREAM_LIVE_TOOL_CALLS as STREAM_LIVE_TOOL_CALLS,
    PENDING_GOAL_CONTINUATION,
    LOCK, SESSIONS, SESSION_DIR,
    _get_session_agent_lock, set_thread_env, clear_thread_env,
    append_runtime_partial_text, append_runtime_reasoning_text,
    attach_runtime_agent, finish_runtime_tool_call,
    update_active_run,
    replace_runtime_reasoning_text,
    start_runtime_tool_call,
    alias_session_agent_lock,
    resolve_model_provider,
    resolve_custom_provider_connection,
    model_with_provider_context,
    warm_models_catalog_provenance_if_cold,
    load_settings,
    parse_reasoning_effort,
    coerce_reasoning_effort_for_model,
    _main_model_request_overrides,
    PROCESS_SESSION_INDEX, PROCESS_SESSION_INDEX_LOCK,
)
from api.helpers import redact_session_data, _redact_text  # noqa: F401
from api.compression_anchor import is_context_compression_marker, visible_messages_for_anchor  # noqa: F401
from api.compression_recovery import stamp_compression_exhausted_recovery  # noqa: F401
from api.metering import meter  # noqa: F401
from api.todo_state import attach_todo_state, emit_todo_state  # noqa: F401
from api.turn_journal import append_turn_journal_event_for_stream  # noqa: F401
from api.runs import TurnExecution  # noqa: F401
from api.usage import prompt_cache_hit_percent  # noqa: F401
from api.sessions.cache import _evict_sessions_over_cap
from api.sessions.external import get_state_db_session_messages
from api.sessions.operations import mark_session_title_generated, session_has_manual_title
from api.sessions.process_wakeup import (
    clear_process_wakeup_pause,
    record_process_wakeup_provider_unavailable_pause,
)
from api.sessions.reconciliation import reconciled_state_db_messages_for_session
from api.sessions.records import (
    _is_empty_partial_activity_message as _is_empty_partial_activity_message,
)
from api.sessions.repository import edit_session
from api.process_event_utils import (
    claim_async_delegation_delivery,
    complete_async_delegation_delivery,
    completion_delivery_id,
    release_async_delegation_delivery,
    requeue_async_delegation_event,
    schedule_async_delegation_claim_retry,
)
from api.model_context import (
    _context_length_lookup_inputs_for_model,
    _should_accept_session_context_length_refresh,
)
from api.runs import local as _streaming_local_run


# Global lock for os.environ writes. Per-session locks (_agent_lock) prevent
# concurrent runs of the SAME session, but two DIFFERENT sessions can still
# interleave their os.environ writes. This global lock serializes the env
# save/restore — held only briefly across the env-mutation critical section,
# NOT for the entire agent run. The agent runs outside the lock; the finally
# block re-acquires to atomically restore env vars. See narrow-lock pattern
# in _run_agent_streaming (line ~2719) and profile_env_for_background_worker
# (api/profiles.py:715).






_CANCEL_MARKER_PATTERNS = ('task cancelled', 'task canceled', 'response interrupted')




# Structured markers the Hermes Agent stamps on synthetic scaffolding turns that
# drive its internal verify-before-finish loop. The agent appends BOTH a
# synthetic assistant "premature done" answer AND a synthetic ``user`` nudge
# (e.g. "[System: You edited code in this turn, but the workspace does not have
# fresh passing verification evidence yet...]") to preserve role alternation for
# the next API turn, and flags each with one of these keys. They exist only to
# run the loop; they must never surface as visible user/assistant turns in the
# WebUI transcript. This mirrors ``run_agent._EPHEMERAL_SCAFFOLDING_FLAGS`` on
# the agent side (which keeps them out of the durable session store); WebUI
# honors the same markers when building the visible transcript. Keep roughly in
# sync with the agent set. (#5334; same class as #3320/#3821/#4373/#4875)
from api.sessions.store import get_session, title_from  # noqa: F401 -- late-bound local-run facade seam

# Fields that are safe to send to LLM provider APIs.
# Everything else (attachments, timestamp, _ts, etc.) is display-only
# metadata added by the webui and must be stripped before the API call.
# `reasoning_content` is provider-facing for reasoning-capable models. Display
# metadata such as `reasoning`, `thinking`, and `_reasoning` stays omitted here.



# ── Per-turn session identity (xsession wakeup misroute root fix — Option 1) ─
# WebUI bound per-turn session identity ONLY to the process-global
# os.environ['HERMES_SESSION_KEY'] (turn-start, line ~3263) and released the
# env lock BEFORE the agent ran. WebUI never called any contextvar setter, so
# gateway.session_context._SESSION_KEY stayed _UNSET and
# tools.approval.get_current_session_key (the EXACT call a
# notify_on_complete background spawn makes in terminal_tool.py:~1928) fell
# back to that racy process-global slot. Two concurrent WebUI turns therefore
# raced on one slot: session A's spawn could capture session B's id, and at
# completion the server-side wakeup turn started for the WRONG session
# (RCA t_f62ff1e8, agent.log:6632). The agent worker runs synchronously inside
# the _run_agent_streaming thread (concurrent tool batches use
# contextvars.copy_context() so children inherit this binding); binding the
# context-local here makes the capture task/thread-local and race-immune.














# Tool-arg keys whose values are card content / diff-reconstruction inputs.
# These must not be capped to the short incidental-arg limit (#4928), or long
# commands/paths get cut and recovery-rebuilt diffs (built from old_string/
# new_string/patch) break. Matched case-insensitively against the arg key.




# ── SSE write deadline (Defect A: per-connection thread exhaustion) ─────────
# server.py runs QuietHTTPServer(ThreadingHTTPServer): one OS thread per
# connection, no pool cap (request_queue_size=64). Every SSE endpoint holds
# its thread for the connection's whole lifetime. If a tab is slow or
# backgrounded its TCP receive window fills; the next handler.wfile.write()/
# flush() then blocks *indefinitely* (sockets have no write timeout by
# default). That thread is pinned forever — it never reaches its
# `finally: unsubscribe`, so the SessionChannel reaper can never reclaim the
# channel either. N such tabs * M sessions pile threads up until new
# requests queue past request_queue_size and the UI shows "streaming
# pending".
#
# Fix: arm a socket-level timeout on the connection. A genuinely healthy
# keepalive/event write completes in well under a millisecond, so a
# multi-second deadline never trips for a live tab; only a backpressured
# (stuck) socket blocks past it. When it trips, the write raises
# socket.timeout — which on Python 3.10+ *is* TimeoutError, already a member
# of api.routes._CLIENT_DISCONNECT_ERRORS — so each SSE handler's existing
# `except _CLIENT_DISCONNECT_ERRORS:` breaks the loop, `finally` drops the
# subscriber, the browser's EventSource auto-reconnects, and the OS thread
# is released. SessionChannel already supports reconnect + offline buffer,
# so no events are lost for a tab that comes back. Operators behind unusual
# proxies can tune the deadline without code changes.


def _run_agent_streaming(
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
    """Run one complete local-agent streaming lifecycle."""
    return _streaming_local_run.run_agent_streaming(
        session_id,
        msg_text,
        model,
        workspace,
        stream_id,
        attachments,
        ephemeral=ephemeral,
        model_provider=model_provider,
        goal_related=goal_related,
        moa_config=moa_config,
        dependencies=_streaming_local_run.LocalRunDependencies(
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
        ),
    )

# ============================================================
# SECTION: HTTP Request Handler
# do_GET: read-only API endpoints + SSE stream + static HTML
# do_POST: mutating endpoints (session CRUD, chat, upload, approval)
# Routing is a flat if/elif chain. See ARCHITECTURE.md section 4.1.
# ============================================================
