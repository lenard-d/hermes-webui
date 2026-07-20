"""
Hermes Web UI -- SSE streaming engine and agent thread runner.
Includes Sprint 10 cancel support via CANCEL_FLAGS.
"""
import contextlib
import contextvars
import json
import logging
import os
import queue
import random
import re
import sqlite3
import shlex  # noqa: F401 -- late-bound streaming facade seam
import subprocess  # noqa: F401 -- late-bound streaming facade seam
import threading
import time
import traceback  # noqa: F401 -- late-bound local-run facade seam
import copy
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

from api.config import (  # noqa: F401 -- late-bound local-run facade seams
    get_config,
    STREAMS as STREAMS, STREAMS_LOCK as STREAMS_LOCK,
    CANCEL_FLAGS, AGENT_INSTANCES as AGENT_INSTANCES,
    STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT,
    STREAM_LIVE_TOOL_CALLS as STREAM_LIVE_TOOL_CALLS,
    PENDING_GOAL_CONTINUATION,
    LOCK, SESSIONS, SESSION_DIR,
    _get_session_agent_lock, _set_thread_env, _clear_thread_env,
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
from api.compression_anchor import is_context_compression_marker, visible_messages_for_anchor  # noqa: F401 -- late-bound local-run facade seams
from api.compression_recovery import stamp_compression_exhausted_recovery  # noqa: F401 -- late-bound local-run facade seam
from api.metering import meter  # noqa: F401 -- late-bound local-run facade seam
from api.todo_state import attach_todo_state, emit_todo_state  # noqa: F401
from api.turn_journal import append_turn_journal_event_for_stream  # noqa: F401 -- late-bound local-run facade seam
from api.turn_execution import TurnExecution  # noqa: F401 -- late-bound local-run facade seam
from api.usage import prompt_cache_hit_percent  # noqa: F401 -- late-bound local-run facade seam
from api.models import (  # noqa: F401 -- late-bound local-run facade seams
    _is_empty_partial_activity_message,  # noqa: F401 -- late-bound streaming facade seam
    _evict_sessions_over_cap,
    clear_process_wakeup_pause,
    get_state_db_session_messages,
    record_process_wakeup_provider_unavailable_pause,
    reconciled_state_db_messages_for_session,
)
# These names are consumed through the late-bound ``streaming_api()`` facade by
# ``streaming_parts.title_generation``. Keep them public here so existing
# monkeypatch seams continue to observe the canonical module.
from api.session_ops import (  # noqa: F401
    mark_session_title_generated,
    session_has_manual_title,
)
from api.session_repository import edit_session  # noqa: F401 -- late-bound streaming facade seam
from api.process_event_utils import (
    claim_async_delegation_delivery,
    complete_async_delegation_delivery,
    completion_delivery_id,
    release_async_delegation_delivery,
    requeue_async_delegation_event,
    schedule_async_delegation_claim_retry,
)
from api.streaming_parts import payloads as _streaming_payloads
from api.streaming_parts import attachments as _streaming_attachments
from api.streaming_parts import compression_anchors as _streaming_compression_anchors
from api.streaming_parts import context_replay as _streaming_context_replay
from api.streaming_parts import gateway_routing_metadata as _streaming_gateway_routing
from api.streaming_parts import live_controls as _streaming_live_controls
from api.streaming_parts import local_run as _streaming_local_run
from api.streaming_parts import message_sanitization as _streaming_message_sanitization
from api.streaming_parts import post_compression_context as _streaming_post_compression
from api.streaming_parts import provider_errors as _streaming_provider_errors
from api.streaming_parts import runtime_resolution as _streaming_runtime_resolution
from api.streaming_parts import stale_user_context as _streaming_stale_user_context
from api.streaming_parts import thinking_content as _streaming_thinking
from api.streaming_parts import terminal_outcomes as _streaming_terminal_outcomes
from api.streaming_parts import title_generation as _streaming_titles
from api.streaming_parts import turn_context as _streaming_turn_context
from api.streaming_parts import webui_prefill as _streaming_webui_prefill
from api.streaming_parts.bindings import streaming_api as _streaming_api


def _session_payload_with_full_messages(session, *, tool_calls=None):
    """Return compact session metadata plus the embedded full transcript.

    ``Session.compact()`` may intentionally use metadata-only counts from an
    index/sidebar load. A settled SSE payload that embeds ``session.messages``
    must report the count of that embedded transcript, otherwise completion and
    reconcile paths can mistake a complete payload for a stale short window.
    """
    return _streaming_payloads.session_payload_with_full_messages(
        _streaming_api(),
        session,
        tool_calls=tool_calls,
    )


def _compact_for_echo_compare(value: str) -> str:
    """Normalize visible stream text for duplicate echo detection."""
    return _streaming_payloads.compact_for_echo_compare(value)


def _strip_compact_echo_suffix(value: str, suffix: str, *, search_window: int = 4096) -> tuple[str, bool]:
    """Remove ``suffix`` from ``value`` when they match after whitespace folding."""
    return _streaming_payloads.strip_compact_echo_suffix(
        _streaming_api(),
        value,
        suffix,
        search_window=search_window,
    )


def _redacted_session_payload_with_full_messages(session, *, tool_calls=None) -> dict | None:
    """Best-effort terminal SSE session payload for already-persisted state."""
    return _streaming_payloads.redacted_session_payload_with_full_messages(
        _streaming_api(),
        session,
        tool_calls=tool_calls,
    )


def _cancel_event_payload(
    message: str = "Cancelled by user",
    *,
    session: dict | None = None,
) -> dict:
    """Return base cancel terminal event metadata."""
    return _streaming_payloads.cancel_event_payload(message, session=session)


# Global lock for os.environ writes. Per-session locks (_agent_lock) prevent
# concurrent runs of the SAME session, but two DIFFERENT sessions can still
# interleave their os.environ writes. This global lock serializes the env
# save/restore — held only briefly across the env-mutation critical section,
# NOT for the entire agent run. The agent runs outside the lock; the finally
# block re-acquires to atomically restore env vars. See narrow-lock pattern
# in _run_agent_streaming (line ~2719) and profile_env_for_background_worker
# (api/profiles.py:715).
_ENV_LOCK = threading.Lock()

_KEYLESS_CUSTOM_API_KEY = "dummy-key"
_STREAM_WRITEBACK_DIAG_DEFAULT_THRESHOLD_MS = 250.0

_STREAMING_CRON_PROFILE_HOME: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "webui_streaming_cron_profile_home",
    default=None,
)
_STREAMING_CRONJOB_WRAPPER_INSTALLED = False


def _stream_writeback_diag_threshold_seconds(environ=None):
    if environ is None:
        environ = os.environ
    raw = str(
        environ.get(
            "HERMES_WEBUI_STREAM_WRITEBACK_DIAG_MS",
            _STREAM_WRITEBACK_DIAG_DEFAULT_THRESHOLD_MS,
        )
    ).strip()
    try:
        threshold_ms = float(raw)
    except (TypeError, ValueError):
        threshold_ms = _STREAM_WRITEBACK_DIAG_DEFAULT_THRESHOLD_MS
    if threshold_ms < 0:
        return None
    return threshold_ms / 1000.0


@contextlib.contextmanager
def _stream_writeback_stage(timings, name, *, clock=time.perf_counter):
    started = clock()
    try:
        yield
    finally:
        try:
            timings.append((str(name), max(0.0, float(clock() - started))))
        except Exception:
            pass


def _log_stream_writeback_timings(
    session_id,
    stream_id,
    timings,
    started,
    *,
    clock=time.perf_counter,
    log=logger,
    environ=None,
):
    threshold = _stream_writeback_diag_threshold_seconds(environ=environ)
    if threshold is None:
        return False
    try:
        total_seconds = max(0.0, float(clock() - started))
    except Exception:
        return False
    if total_seconds < threshold:
        return False
    parts = []
    for name, elapsed in timings or []:
        try:
            parts.append(f"{name}={float(elapsed) * 1000.0:.1f}ms")
        except Exception:
            continue
    log.debug(
        "stream final writeback timing session=%s stream=%s total=%.1fms stages=%s",
        session_id,
        stream_id,
        total_seconds * 1000.0,
        " ".join(parts),
    )
    return True


def _install_streaming_cronjob_profile_wrapper() -> None:
    """Wrap the agent cronjob tool so calls run under the streaming profile.

    The in-chat agent run already binds per-turn contextvars for other
    session-scoped state. Cron jobs are special because ``cron.jobs`` snapshots
    path constants at import time, so the model-facing ``cronjob`` tool must
    enter the existing WebUI cron profile context at the tool-call boundary.
    That context uses the cron-specific lock and restores the module caches as
    soon as the single cron tool call returns, avoiding long-lived global path
    mutation for the whole agent turn.
    """
    global _STREAMING_CRONJOB_WRAPPER_INSTALLED
    if _STREAMING_CRONJOB_WRAPPER_INSTALLED:
        return
    try:
        from tools.registry import registry
    except Exception:
        logger.debug("streaming cronjob wrapper: tools registry unavailable", exc_info=True)
        return

    entry = registry.get_entry("cronjob")
    if entry is None:
        try:
            import tools.cronjob_tools  # noqa: F401
        except Exception:
            logger.debug("streaming cronjob wrapper: cronjob tool import failed", exc_info=True)
        entry = registry.get_entry("cronjob")
    if entry is None:
        logger.debug("streaming cronjob wrapper: cronjob tool not registered")
        return
    original_handler = entry.handler
    if getattr(original_handler, "_webui_streaming_profile_wrapper", False):
        _STREAMING_CRONJOB_WRAPPER_INSTALLED = True
        return

    # This relies on the agent tool executor's ``propagate_context_to_thread``
    # using an unfiltered ``contextvars.copy_context()`` so this WebUI-owned
    # contextvar reaches the sync cronjob handler even when the tool call runs
    # on the agent's ThreadPoolExecutor worker.
    def _profile_scoped_cronjob_handler(args, **kwargs):
        profile_home = _STREAMING_CRON_PROFILE_HOME.get()
        if not profile_home:
            return original_handler(args, **kwargs)
        from api.profiles import cron_profile_context_for_home
        with cron_profile_context_for_home(Path(profile_home)):
            return original_handler(args, **kwargs)

    _profile_scoped_cronjob_handler.__dict__["_webui_streaming_profile_wrapper"] = True
    _profile_scoped_cronjob_handler.__dict__["_webui_original_handler"] = original_handler
    registry.register(
        name=entry.name,
        toolset=entry.toolset,
        schema=entry.schema,
        handler=_profile_scoped_cronjob_handler,
        check_fn=entry.check_fn,
        requires_env=entry.requires_env,
        is_async=entry.is_async,
        description=entry.description,
        emoji=entry.emoji,
        max_result_size_chars=entry.max_result_size_chars,
        dynamic_schema_overrides=entry.dynamic_schema_overrides,
    )
    _STREAMING_CRONJOB_WRAPPER_INSTALLED = True


_PERSISTENT_MEMORY_FILES = (
    ("memory", ("memories", "MEMORY.md")),
    ("user", ("memories", "USER.md")),
    ("soul", ("SOUL.md",)),
)


def _file_signature(path: Path) -> tuple[int, int] | None:
    return _streaming_runtime_resolution.file_signature(_streaming_api(), path)


def _persistent_state_snapshot(profile_home: str | None) -> dict:
    """Capture lightweight memory/skill file signatures for save toasts."""
    return _streaming_runtime_resolution.persistent_state_snapshot(
        _streaming_api(),
        profile_home,
    )


def _persistent_state_changes(before: dict | None, after: dict | None) -> dict:
    return _streaming_runtime_resolution.persistent_state_changes(
        _streaming_api(),
        before,
        after,
    )


def _apply_profile_provider_context_to_streaming_model(
    model: str | None,
    provider_context: str | None,
    profile_provider: str | None,
    profile_default_model: str | None,
) -> tuple[str | None, str | None, bool]:
    """Attach profile provider context and repair stale cross-provider models."""
    return _streaming_runtime_resolution.apply_profile_provider_context_to_streaming_model(
        _streaming_api(),
        model,
        provider_context,
        profile_provider,
        profile_default_model,
    )


def _apply_profile_home_context_to_streaming_model(
    model: str | None,
    provider_context: str | None,
    profile_home: str | None,
    has_profile: bool,
) -> tuple[str | None, str | None, bool]:
    """Apply profile provider/model context from a profile config if present."""
    return _streaming_runtime_resolution.apply_profile_home_context_to_streaming_model(
        _streaming_api(),
        model,
        provider_context,
        profile_home,
        has_profile,
    )


def _resolve_custom_provider_runtime_overrides(
    resolved_provider: str | None,
    resolved_api_key: str | None,
    resolved_base_url: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Return provider/key/base_url overrides for ``custom:*`` endpoints.

    Hermes Agent treats named custom providers as routing hints around an
    OpenAI-compatible base URL.  Local OpenAI-compatible servers often run
    without authentication, so a missing key should not fail before the first
    request; pass a harmless placeholder to the SDK and let the endpoint accept
    it or return its own auth error.
    """
    return _streaming_runtime_resolution.resolve_custom_provider_runtime_overrides(
        _streaming_api(),
        resolved_provider,
        resolved_api_key,
        resolved_base_url,
    )


def _same_base_url_endpoint(url_a: str, url_b: str) -> bool:
    """True if two base URLs point at the same scheme+host+port endpoint.

    Used to decide whether a runtime base_url is just a normalized form of the
    configured one (e.g. OpenCode-Go's ``/v1`` de-duplication on the same host)
    versus a genuinely different endpoint (an explicit ``providers.<id>.base_url``
    override at a different host/port that must be preserved). Path/query are
    intentionally ignored — the normalization #3895 fixes is path-only.
    """
    return _streaming_runtime_resolution.same_base_url_endpoint(
        _streaming_api(),
        url_a,
        url_b,
    )


def _runtime_preferred_base_url(
    runtime_provider: dict | None,
    resolved_provider: str | None,
    configured_base_url: str | None,
) -> str | None:
    """Prefer the runtime-normalized base_url, but never override an explicit
    configured endpoint that points somewhere genuinely different.

    The #3895 bug was that WebUI used the *configured* base_url (which can carry a
    duplicated ``/v1``) instead of the runtime provider's per-model-normalized
    base_url, 404ing OpenCode-Go. But blindly preferring the runtime URL would
    clobber a legitimate ``providers.<id>.base_url`` override (e.g. LM Studio at a
    LAN IP, an OpenRouter mirror). So:
      - no runtime URL            -> keep configured
      - no configured URL         -> use runtime (all we have)
      - named ``custom:`` endpoint -> configured wins (then runtime as fallback)
      - same scheme+host+port     -> runtime wins (it's the normalized/corrected
                                      form of the same endpoint — the #3895 case)
      - different endpoint        -> configured override wins (no regression)
    """
    return _streaming_runtime_resolution.runtime_preferred_base_url(
        _streaming_api(),
        runtime_provider,
        resolved_provider,
        configured_base_url,
    )


def _is_fallback_lifecycle_message(kind: str, message: str) -> bool:
    """Return True if an agent lifecycle status should surface as a fallback warning."""
    return _streaming_runtime_resolution.is_fallback_lifecycle_message(
        _streaming_api(),
        kind,
        message,
    )


def _is_agent_compression_start_status(kind: str, message: str) -> bool:
    """Return True only for real Hermes context-compression start notices.

    WebUI bridges matching lifecycle statuses into an SSE ``compressing`` event
    and paints the live "Compressing context" worklog divider. The previous
    matcher used broad substrings such as ``'compressing' in message`` and
    ``'preflight compression' in message``, which can false-positive on skip /
    cooldown / unrelated notices and make brand-new low-token turns look like
    auto-compression.

    Positive markers below match the agent emitters in hermes-agent
    (``turn_context`` preflight, ``conversation_loop`` pre-API / 413 / too-large,
    ``conversation_compression`` compaction status). Explicitly reject skip /
    defer notices so "Skipping preflight compression…" never surfaces as a
    running compress divider.
    """
    return _streaming_runtime_resolution.is_agent_compression_start_status(
        _streaming_api(),
        kind,
        message,
    )


def _prewarm_skill_tool_modules():
    """Import tools.skills_tool and tools.skill_manager_tool outside any lock.

    First-time module imports can trigger heavy initialisation (disk I/O,
    transitive imports, plugin discovery).  Performing those imports while
    holding ``_ENV_LOCK`` serialises every concurrent session behind the
    slowest import.  Prewarming ensures the modules are already in
    ``sys.modules`` before the lock is acquired, so the lock body only
    does lightweight attribute patching.

    We cannot place these at module top-level because ``tools.*`` lives
    in the hermes-agent package which may not be on ``sys.path`` at
    import time (Docker volume-mount ordering).  A dedicated helper
    keeps the lazy-import try/except in one place and makes the intent
    explicit.
    """
    for _mod_name in ('tools.skills_tool', 'tools.skill_manager_tool'):
        try:
            __import__(_mod_name)
        except ImportError:
            pass


# Lazy import to avoid circular deps -- hermes-agent is on sys.path via api/config.py
from api.agent_runtime import ensure_agent_runtime_current, get_ai_agent_class


# Eagerly attempt the import at startup, matching the pre-guard behavior. If
# dependencies are not ready yet, _get_ai_agent() retries when a chat starts.
AIAgent = get_ai_agent_class()


def _get_ai_agent():
    """Return AIAgent class, retrying the import if the initial attempt failed.

    auto_install_agent_deps() in server.py may install missing packages after
    this module is first imported (common in Docker with a volume-mounted agent).
    Re-attempting the import here picks up the newly installed packages without
    requiring a server restart. The shared runtime guard also refuses to reuse
    cached Agent modules after the source checkout changes.
    """
    global AIAgent
    ensure_agent_runtime_current()
    if AIAgent is None:
        AIAgent = get_ai_agent_class()
    return AIAgent


def _is_quota_error_text(err_text: str) -> bool:
    """Return True when provider text looks like quota/usage exhaustion."""
    _err_lower = str(err_text or '').lower()
    return (
        'insufficient credit' in _err_lower
        or 'credit balance' in _err_lower
        or 'credits exhausted' in _err_lower
        or 'more credits' in _err_lower
        or 'can only afford' in _err_lower
        or 'fewer max_tokens' in _err_lower
        or 'quota_exceeded' in _err_lower
        or 'quota exceeded' in _err_lower
        or 'exceeded your current quota' in _err_lower
        # OpenAI Codex OAuth usage-exhaustion shapes (#1765).
        or 'plan limit reached' in _err_lower
        or 'usage_limit_exceeded' in _err_lower
        or 'usage limit exceeded' in _err_lower
        or 'reached the limit of messages' in _err_lower
        or 'used up your usage' in _err_lower
        or ('plan' in _err_lower and 'limit' in _err_lower and 'reached' in _err_lower)
    )


def _clarify_timeout_seconds(default: int = 120) -> int:
    """Resolve clarify timeout from config, with bounded fallback."""
    try:
        cfg = get_config()
        raw = cfg.get("clarify", {}).get("timeout", default)
        timeout_seconds = int(raw)
        if timeout_seconds <= 0:
            return default
        return timeout_seconds
    except Exception:
        return default


_CANCEL_MARKER_PATTERNS = ('task cancelled', 'task canceled', 'response interrupted')


_WEBUI_PROGRESS_PROMPT = """
WebUI progress guidance:
- Match the normal Hermes messaging style, but do not let long tool-running WebUI turns appear silent.
- For long multi-step work that uses tools, emit brief user-visible progress updates as normal assistant content, not only as hidden reasoning.
- Before the first tool batch in a long task, say what you are about to inspect.
- After each meaningful batch of tool calls, say what you just confirmed and what you will check next before continuing with more tools.
- Do not run many independent tool batches back-to-back without visible assistant text between them when the task is still ongoing.
- Do not keep progress only in reasoning, thinking, or tool-result channels; those are not a substitute for visible interim updates.
- Each update should say what you are about to check, what you just confirmed, or why the next tool call is needed.
- Keep updates concise, factual, and in the user's language. One or two short sentences are enough.
- Do not reveal hidden reasoning, chain-of-thought, private scratchpads, secrets, raw logs, or long tool output.
- Password, API-key, token, and secret fields are automatically redacted by the system. Treat masked values as intentional redaction, not placeholder text or user input errors, and do not tell the user a stored credential is wrong based on a masked value alone.
- Final visible assistant replies must be clear, user-facing, and in the user's language, not private planning notes.
- Do not include terse planning fragments or scratchpad shorthand in visible assistant text. Avoid fragments like "Need script", "Need check logs", "Need inspect email", or "maybe invite"; either omit them or rewrite them as clear user-facing progress.
- For direct answers or very short tasks, skip progress updates and answer normally.
""".strip()


def _webui_surface_context_prompt(surface_context: Optional[dict]) -> str:
    """Return safe WebUI session metadata for the agent's ephemeral context.

    Messaging gateways inject platform/channel context before each run. Browser
    sessions do not have a chat platform wrapper, so provide an explicit, small
    surface description here instead of relying on the model to infer where it
    is running from the transcript alone.
    """
    if not isinstance(surface_context, dict):
        return ""

    lines = [
        "WebUI session context:",
        "- This browser session is not the same live transcript as Telegram, Discord, Slack, or other messaging surfaces.",
        "- Use durable memory, saved sessions, and available tools for cross-surface recall instead of assuming those transcripts are in this browser chat.",
        "- Do not copy or dump this browser transcript into external notes or durable memory by default.",
        "- Write to external notes or durable memory only for explicit captures, durable user preferences, decisions, blockers/open issues, runbook-worthy workflows, or other clearly reusable signals; otherwise leave notes unchanged.",
        "- When you do write or update a durable note, briefly tell the user what note/section changed so the write is reviewable.",
    ]
    fields = (
        ("source", "Source"),
        ("session_id", "Session ID"),
        ("profile", "Profile"),
        ("workspace", "Workspace"),
    )
    for key, label in fields:
        raw = surface_context.get(key)
        value = str(raw).strip() if raw is not None else ""
        if value:
            lines.append(f"- {label}: {value}")
    return "\n".join(lines)


def _webui_ephemeral_system_prompt(
    personality_prompt: Optional[str],
    surface_context: Optional[dict] = None,
    config_data: Optional[dict] = None,
) -> str:
    """Build WebUI-only runtime instructions that are not persisted to history."""
    parts = []
    if personality_prompt:
        parts.append(str(personality_prompt).strip())
    surface_prompt = _webui_surface_context_prompt(surface_context)
    if surface_prompt:
        parts.append(surface_prompt)
    parts.append(_WEBUI_PROGRESS_PROMPT)
    delivery_prompt = _webui_delivery_context_prompt(config_data)
    if delivery_prompt:
        parts.append(delivery_prompt)
    return "\n\n".join(part for part in parts if part)


_SECRET_SHAPED_RE = _streaming_webui_prefill.SECRET_SHAPED_RE

def _redact_prefill_status_text(text: str) -> str:
    """Return a short, non-secret diagnostic string for prefill status."""
    return _streaming_webui_prefill.redact_prefill_status_text(_streaming_api(), text)


def _valid_prefill_messages(value) -> list[dict]:
    """Normalize a prefill payload to role/content messages."""
    return _streaming_webui_prefill.valid_prefill_messages(_streaming_api(), value)


def _resolve_prefill_path(raw: str) -> Path:
    return _streaming_webui_prefill.resolve_prefill_path(_streaming_api(), raw)


_PREFILL_SCRIPT_OUTPUT_LIMIT = _streaming_webui_prefill.PREFILL_SCRIPT_OUTPUT_LIMIT
_PREFILL_CONTEXT_DEFAULT_MAX_CHARS = (
    _streaming_webui_prefill.PREFILL_CONTEXT_DEFAULT_MAX_CHARS
)


def _prefill_context_max_chars(config_data: dict) -> int:
    return _streaming_webui_prefill.prefill_context_max_chars(
        _streaming_api(),
        config_data,
    )


def _prefill_context_char_count(messages: list[dict]) -> int:
    return _streaming_webui_prefill.prefill_context_char_count(
        _streaming_api(),
        messages,
    )


def _budget_compacted_prefill_context(context: dict, *, max_chars: int, char_count: int) -> dict:
    return _streaming_webui_prefill.budget_compacted_prefill_context(
        _streaming_api(),
        context,
        max_chars=max_chars,
        char_count=char_count,
    )


def _apply_prefill_context_budget(context: dict, config_data: dict) -> dict:
    return _streaming_webui_prefill.apply_prefill_context_budget(
        _streaming_api(),
        context,
        config_data,
    )


def _prefill_not_configured() -> dict:
    return _streaming_webui_prefill.prefill_not_configured(_streaming_api())


def _load_prefill_messages_file(file_raw: str, *, source: str = "file", status: str = "loaded") -> dict:
    return _streaming_webui_prefill.load_prefill_messages_file(
        _streaming_api(),
        file_raw,
        source=source,
        status=status,
    )


def _prefill_script_timeout(config_data: dict) -> float:
    return _streaming_webui_prefill.prefill_script_timeout(
        _streaming_api(),
        config_data,
    )


def _prefill_script_command(raw) -> list[str]:
    return _streaming_webui_prefill.prefill_script_command(_streaming_api(), raw)


def _messages_from_prefill_script_output(text: str) -> list[dict]:
    return _streaming_webui_prefill.messages_from_prefill_script_output(
        _streaming_api(),
        text,
    )


def _load_prefill_messages_script(config_data: dict) -> dict:
    return _streaming_webui_prefill.load_prefill_messages_script(
        _streaming_api(),
        config_data,
    )


def _load_webui_prefill_context(
    config_data: Optional[dict] = None,
) -> dict:
    """Load configured WebUI session prefill messages.

    Supports the same bounded JSON-file shape used by Hermes Agent.  WebUI also
    supports its own explicitly opt-in script hook so admins can bridge Joplin,
    Obsidian, Notion, llm-wiki, or another local notes source into ephemeral
    turn context without baking any one note provider into the WebUI.
    """
    return _streaming_webui_prefill.load_webui_prefill_context(
        _streaming_api(),
        config_data,
    )


def _public_prefill_context_status(prefill_context: dict) -> dict:
    """Strip message bodies before sending context status to the browser."""
    return _streaming_webui_prefill.public_prefill_context_status(
        _streaming_api(),
        prefill_context,
    )


def _webui_delivery_context_prompt(config_data: Optional[dict] = None) -> str:
    """Return platform/delivery context for the ephemeral system prompt.

    Connected platforms, home channels, and scheduled-task delivery hints
    are injected into the system prompt (safe for role alternation) rather
    than as a prefill ``user`` message, which strict chat templates (Mistral,
    Gemma) reject.

    NOTE: This function only covers platform/delivery info.  The session
    framing (\"Source: WebUI\", \"Session ID\", \"Profile\", \"Workspace\") is
    emitted by ``_webui_surface_context_prompt()``, which is called from
    ``_webui_ephemeral_system_prompt()`` before this helper.  If you
    refactor this area, keep that surface call in place — the two helpers
    together produce the full session context block.
    """
    return _streaming_webui_prefill.webui_delivery_context_prompt(
        _streaming_api(),
        config_data,
    )


def _prefill_messages_with_webui_context(prefill_context: dict, config_data: Optional[dict] = None) -> list[dict]:
    """Combine recall prefill with WebUI session context.

    The session context (connected platforms, delivery hints) is injected
    via ``_webui_ephemeral_system_prompt`` / ``ephemeral_system_prompt``
    instead of as a prefill ``user`` message.  Adding it as a user message
    creates two consecutive user turns (prefill + actual) which strict chat
    templates (Mistral, Gemma) reject with a Jinja 500.
    """
    return _streaming_webui_prefill.prefill_messages_with_webui_context(
        _streaming_api(),
        prefill_context,
        config_data,
    )


def _normalize_prefill_messages_before_user_turn(prefill_messages: list[dict]) -> list[dict]:
    """Ensure WebUI prefill does not end with user role before an appended turn.

    Some upstream prefill sources can end with `role: user` (for example,
    session context or recall snippets). WebUI always appends the current user
    turn after prefill in the streaming path, so a terminal user role creates an
    adjacent user/user sequence that strict chat templates (Gemma, Mistral/Jinja)
    reject.

    To keep behavior scoped, only consecutive terminal user messages are removed
    just before that boundary; earlier roles remain untouched.
    """
    return _streaming_webui_prefill.normalize_prefill_messages_before_user_turn(
        _streaming_api(),
        prefill_messages,
    )


def _has_new_assistant_reply(all_messages: list, prev_count: int) -> bool:
    """Return True if *new* messages (beyond ``prev_count``) contain an
    assistant message with non-empty content.

    ``all_messages`` is ``result.get('messages')`` which includes the full
    conversation history.  ``prev_count`` is ``len(_previous_context_messages)``
    — the number of messages present before the current turn started.  Only
    messages at index >= prev_count are inspected so that historical assistant
    replies don't mask a silent failure on the current turn.

    If ``len(all_messages) < prev_count`` (an edge-case shrink), there is no
    reliable new-message slice to inspect. Treat that as "no new assistant
    reply" so stale historical assistant replies cannot mask a silent failure.
    When ``len == prev_count``, there are no new messages and we return False.
    """
    if len(all_messages) > prev_count:
        # Normal case: new messages appended beyond the pre-turn history.
        candidates = all_messages[prev_count:]
    elif len(all_messages) < prev_count:
        return False
    else:
        # Same length. In production this means no new messages were appended.
        # However, some test fixtures replace the entire message list rather
        # than appending, so check whether the tail changed.
        return False
    return any(
        m.get('role') == 'assistant' and str(m.get('content') or '').strip()
        for m in candidates
    )


def _preferred_agent_display_name() -> str:
    """Return the configured assistant display name for user-facing copy."""
    try:
        name = str((load_settings() or {}).get('bot_name') or '').strip()
    except Exception:
        logger.debug("Failed to load bot_name for cancellation copy", exc_info=True)
        name = ''
    return name or 'Hermes'


def _preferred_agent_display_name_for_session(session) -> str:
    profile = str(getattr(session, 'profile', '') or '').strip()
    if profile and profile != 'default':
        return profile[:1].upper() + profile[1:]
    return _preferred_agent_display_name()


def _cancelled_turn_hint(agent_name: str | None = None) -> str:
    name = str(agent_name or _preferred_agent_display_name()).strip() or 'Hermes'
    return f'The run was cancelled by the user before {name} finished. No provider failure occurred.'


def _provider_error_probe_text(value) -> tuple[str, int | None]:
    return _streaming_provider_errors.provider_error_probe_text(
        _streaming_api(),
        value,
    )


def _classify_provider_error(err_str: str, exc=None, *, silent_failure: bool = False) -> dict:
    return _streaming_provider_errors.classify_provider_error(
        _streaming_api(),
        err_str,
        exc,
        silent_failure=silent_failure,
    )


def _provider_error_payload(message: str, err_type: str, hint: str = '') -> dict:
    return _streaming_provider_errors.provider_error_payload(
        _streaming_api(),
        message,
        err_type,
        hint,
    )


_MAX_ITERATION_SUMMARY_REQUEST = (
    "You've reached the maximum number of tool-calling iterations allowed. "
    "Please provide a final response summarizing what you've found and accomplished "
    "so far, without calling any more tools."
)


def _is_synthetic_max_iteration_summary_request(message) -> bool:
    return _streaming_terminal_outcomes.is_synthetic_max_iteration_summary_request(
        _streaming_api(),
        message,
    )


def _drop_synthetic_max_iteration_summary_requests(messages, *, enabled: bool = True):
    return _streaming_terminal_outcomes.drop_synthetic_max_iteration_summary_requests(
        _streaming_api(),
        messages,
        enabled=enabled,
    )


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
_SYNTHETIC_CONTROL_MESSAGE_FLAGS = (
    "_verification_stop_synthetic",
    "_pre_verify_synthetic",
)


def _is_synthetic_control_message(message) -> bool:
    return _streaming_terminal_outcomes.is_synthetic_control_message(
        _streaming_api(),
        message,
    )


def _drop_synthetic_control_messages(messages):
    return _streaming_terminal_outcomes.drop_synthetic_control_messages(
        _streaming_api(),
        messages,
    )


def _agent_result_tool_limit_reached(result) -> bool:
    return _streaming_terminal_outcomes.agent_result_tool_limit_reached(
        _streaming_api(),
        result,
    )


def _maybe_inject_max_iteration_summary_fallback(messages, result) -> list:
    return _streaming_terminal_outcomes.maybe_inject_max_iteration_summary_fallback(
        _streaming_api(),
        messages,
        result,
    )


def _mark_latest_assistant_tool_limit_status(messages) -> bool:
    return _streaming_terminal_outcomes.mark_latest_assistant_tool_limit_status(
        _streaming_api(),
        messages,
    )


def _session_has_cancel_marker(session) -> bool:
    return _streaming_terminal_outcomes.session_has_cancel_marker(
        _streaming_api(),
        session,
    )


def _cancelled_turn_content(
    message: str = 'Task cancelled.',
    agent_name: str | None = None,
) -> str:
    return _streaming_terminal_outcomes.cancelled_turn_content(
        _streaming_api(),
        message,
        agent_name,
    )


def _persist_cancelled_turn(session, *, message: str = 'Task cancelled.') -> None:
    _streaming_terminal_outcomes.persist_cancelled_turn(
        _streaming_api(),
        session,
        message=message,
    )


def _cleanup_ephemeral_cancelled_turn(session) -> None:
    _streaming_terminal_outcomes.cleanup_ephemeral_cancelled_turn(
        _streaming_api(),
        session,
    )


def _finalize_cancelled_turn(
    session,
    *,
    ephemeral: bool = False,
    message: str = 'Task cancelled.',
) -> None:
    _streaming_terminal_outcomes.finalize_cancelled_turn(
        _streaming_api(),
        session,
        ephemeral=ephemeral,
        message=message,
    )


def _aiagent_import_error_detail() -> str:
    return _streaming_terminal_outcomes.aiagent_import_error_detail(
        _streaming_api(),
    )
from api.models import get_session, title_from  # noqa: F401 -- late-bound local-run facade seam

# Fields that are safe to send to LLM provider APIs.
# Everything else (attachments, timestamp, _ts, etc.) is display-only
# metadata added by the webui and must be stripped before the API call.
# `reasoning_content` is provider-facing for reasoning-capable models. Display
# metadata such as `reasoning`, `thinking`, and `_reasoning` stays omitted here.
_API_SAFE_MSG_KEYS = {'role', 'content', 'tool_calls', 'tool_call_id', 'name', 'refusal', 'reasoning_content'}

_NATIVE_IMAGE_MAX_BYTES = 20 * 1024 * 1024


def _clean_gateway_routing_scalar(value):
    return _streaming_gateway_routing.clean_gateway_routing_scalar(value)


def _find_gateway_metadata_payload(payload):
    return _streaming_gateway_routing.find_gateway_metadata_payload(
        _streaming_api(),
        payload,
    )


def _normalize_gateway_routing_metadata(payload, requested_model=None, requested_provider=None):
    return _streaming_gateway_routing.normalize_gateway_routing_metadata(
        _streaming_api(),
        payload,
        requested_model=requested_model,
        requested_provider=requested_provider,
    )


def _extract_gateway_routing_metadata(agent, result, requested_model=None, requested_provider=None):
    return _streaming_gateway_routing.extract_gateway_routing_metadata(
        _streaming_api(),
        agent,
        result,
        requested_model=requested_model,
        requested_provider=requested_provider,
    )


def _build_agent_thread_env(profile_runtime_env: dict | None, workspace: str, session_id: str, profile_home: str) -> dict:
    """Build thread-local agent env with per-run values overriding profile defaults.

    Profile runtime env may include TERMINAL_CWD from config.yaml. Passing it as
    **kwargs alongside an explicit TERMINAL_CWD raises TypeError before the
    agent starts, so merge into one dict first and let the active workspace win.
    """
    env = dict(profile_runtime_env or {})
    env.update({
        'TERMINAL_CWD': str(workspace),
        'HERMES_EXEC_ASK': '1',
        'HERMES_SESSION_KEY': session_id,
        'HERMES_SESSION_ID': session_id,
        'HERMES_SESSION_PLATFORM': 'webui',
        # process_complete agent-wakeup wiring (ours-original, Option B): the
        # terminal_tool watcher routing gate (terminal_tool.py:~1940) reads
        # HERMES_SESSION_CHAT_ID to populate pending_watchers for WebUI
        # sessions so notify_on_complete completions enqueue and the agent
        # can be woken. HERMES_SESSION_ID/PLATFORM come from upstream #2279.
        'HERMES_SESSION_CHAT_ID': str(session_id),
        'HERMES_HOME': profile_home,
    })
    return env


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
def _set_turn_session_identity(session_id: str):
    """Bind THIS turn's session identity to the current (task/thread-local)
    context and return an opaque token for _reset_turn_session_identity.

    Binds three context-locals so every session-key / UI-owner consumer is
    covered without a race:
      * ``tools.approval._approval_session_key`` — checked FIRST by
        ``get_current_session_key`` (the exact call terminal_tool.py makes for
        a notify_on_complete background spawn: the bug path).
      * ``gateway.session_context._SESSION_KEY`` — read by direct
        ``get_session_env("HERMES_SESSION_KEY")`` consumers.
      * ``gateway.session_context._SESSION_UI_SESSION_ID`` — exact browser-tab
        return address stamped onto ProcessSession.origin_ui_session_id and
        completion events by modern hermes-agent builds. Authoritative for
        wakeup routing when present (see ``_resolve_completion_target``).

    It deliberately does NOT call ``gateway.session_context.set_session_vars``:
    that blanket setter also zeroes the platform/chat_id/user contextvars,
    flipping ``HERMES_SESSION_PLATFORM`` from its env fallback (``'webui'``,
    still written to os.environ at turn-start) to an explicit ``""`` — which
    would break the ``notify_on_complete`` watcher registration gate.
    """
    sid = str(session_id or "")
    tokens: dict = {}
    try:
        from tools.approval import set_current_session_key
        tokens["approval"] = set_current_session_key(sid)
    except Exception:
        logger.debug("per-turn approval session-key bind failed", exc_info=True)
    try:
        from gateway.session_context import _SESSION_KEY as _SK
        tokens["session_key"] = _SK.set(sid)
    except Exception:
        logger.debug("per-turn _SESSION_KEY bind failed", exc_info=True)
    try:
        from gateway.session_context import _SESSION_UI_SESSION_ID as _UI_SID
        tokens["ui_session_id"] = _UI_SID.set(sid)
    except Exception:
        logger.debug("per-turn _SESSION_UI_SESSION_ID bind failed", exc_info=True)
    return tokens


def _reset_turn_session_identity(tokens) -> None:
    """Restore the context-locals bound by ``_set_turn_session_identity`` via
    contextvars reset-token semantics.

    Reset-token (not a blanket clear) is the canonical idiom: it composes
    correctly under nesting and restores ``_UNSET`` for the top-level turn so
    a reused thread-pool worker leaks no identity and CLI/cron env fallback
    resumes. Order mirrors the bind in reverse.
    """
    if not tokens:
        return
    tok = tokens.get("ui_session_id")
    if tok is not None:
        try:
            from gateway.session_context import _SESSION_UI_SESSION_ID as _UI_SID
            _UI_SID.reset(tok)
        except Exception:
            logger.debug("per-turn _SESSION_UI_SESSION_ID reset failed", exc_info=True)
    tok = tokens.get("session_key")
    if tok is not None:
        try:
            from gateway.session_context import _SESSION_KEY as _SK
            _SK.reset(tok)
        except Exception:
            logger.debug("per-turn _SESSION_KEY reset failed", exc_info=True)
    tok = tokens.get("approval")
    if tok is not None:
        try:
            from tools.approval import reset_current_session_key
            reset_current_session_key(tok)
        except Exception:
            logger.debug("per-turn approval session-key reset failed", exc_info=True)


@contextlib.contextmanager
def _bind_turn_session_identity(session_id: str):
    """Context-manager form of the per-turn session-identity binding.

    The ``_run_agent_streaming`` worker uses the explicit ``_set``/``_reset``
    pair directly because its single ``try/finally`` already spans the whole
    turn (~2k lines) and the binding must cover every mid-turn background
    spawn; this wrapper is the canonical single-call API for other callers and
    for tests, and shares the exact same code path.
    """
    tokens = _set_turn_session_identity(session_id)
    try:
        yield
    finally:
        _reset_turn_session_identity(tokens)


def _stale_completion_max_age_seconds() -> float:
    """Max age (seconds) a background-process completion may sit in the queue
    before the WebUI drain treats it as stale and drops it instead of
    prepending it to the user's next turn.

    Completions older than this are silently consumed (not requeued) so a
    notification that finally fires long after the user moved on cannot
    contaminate an unrelated later turn. See nesquena/hermes-webui#4029.

    Configurable via HERMES_WEBUI_STALE_COMPLETION_MAX_AGE_SECONDS. A value of
    0 (or negative) disables age-gating and restores the legacy drain-all
    behavior. Defaults to 6 hours.
    """
    raw = os.environ.get("HERMES_WEBUI_STALE_COMPLETION_MAX_AGE_SECONDS")
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid HERMES_WEBUI_STALE_COMPLETION_MAX_AGE_SECONDS=%r; using default",
                raw,
            )
    return 6 * 60 * 60  # 6 hours


def _format_process_notification(evt: dict) -> str:
    """Format a completed background process notification for agent input."""
    if not isinstance(evt, dict):
        return ''
    if evt.get('type') == 'async_delegation':
        try:
            from tools.process_registry import format_process_notification

            return format_process_notification(evt) or ''
        except Exception:
            logger.debug("Failed to format async delegation notification", exc_info=True)
            return ''
    if evt.get('type') != 'completion':
        return ''
    _sid = evt.get('session_id', '')
    _cmd = evt.get('command', '')
    _exit = evt.get('exit_code', '')
    _out = evt.get('output') or ''
    if len(_out) > 4000:
        _out = _out[:4000] + '\n... (truncated)'
    return (
        f"[IMPORTANT: Background process {_sid} completed (exit code {_exit}).\n"
        f"Command: {_cmd}\n"
        f"Output:\n{_out}]"
    )


def _mark_process_completion_consumed(process_registry, process_id: str) -> None:
    """Best-effort bridge to the agent registry's private completion marker."""
    try:
        with process_registry._lock:
            process_registry._completion_consumed.add(process_id)
    except Exception:
        logger.debug("Failed to mark process completion consumed", exc_info=True)


def _completion_event_targets_webui_session(evt_session_key: str, session_id: str) -> bool:
    """Return whether a completion event belongs to this WebUI session.

    WebUI normally registers ``PROCESS_SESSION_INDEX[session_id] = session_id``.
    Gateway/agent session keys can differ, so match the direct WebUI case first
    and otherwise resolve through the same session-key index used by the
    background wakeup path.
    """
    if not evt_session_key or not session_id:
        return False
    if evt_session_key == session_id:
        return True
    try:
        with PROCESS_SESSION_INDEX_LOCK:
            return PROCESS_SESSION_INDEX.get(evt_session_key) == session_id
    except Exception:
        logger.debug("Failed to resolve completion event session key", exc_info=True)
        return False


def _drain_webui_process_notifications(
    session_id: str,
    *,
    pending_async_acceptances: list | None = None,
) -> list[str]:
    """Return completion notifications that belong to this WebUI session.

    The agent registry completion queue is process-wide and events do not carry
    the WebUI session key directly. Look up the live process session before
    delivery so completions from other tabs remain queued for their owners.
    """
    if not session_id:
        return []
    try:
        from tools.process_registry import process_registry
    except Exception:
        return []

    notifications: list[str] = []
    skipped_events: list[dict] = []
    async_retry_events: list[tuple[dict, bool]] = []
    completion_queue = getattr(process_registry, 'completion_queue', None)
    if completion_queue is None:
        return []

    # Computed once per drain (not per event): reads/validates the env cap a
    # single time so an invalid value logs at most one warning per drain.
    stale_completion_max_age = _stale_completion_max_age_seconds()

    while True:
        try:
            evt = completion_queue.get_nowait()
        except queue.Empty:
            break
        except Exception:
            logger.debug("Failed to drain process completion queue", exc_info=True)
            break

        evt_sid = completion_delivery_id(evt) if isinstance(evt, dict) else ''
        if not evt_sid:
            skipped_events.append(evt)
            continue
        is_async_delegation = (
            isinstance(evt, dict) and evt.get('type') == 'async_delegation'
        )
        try:
            if (
                not is_async_delegation
                and process_registry.is_completion_consumed(evt_sid)
            ):
                continue
            evt_session_key = str(evt.get('session_key') or '') if isinstance(evt, dict) else ''
            evt_origin_ui_session_id = (
                str(evt.get('origin_ui_session_id') or '') if isinstance(evt, dict) else ''
            )
            if not evt_session_key or not evt_origin_ui_session_id:
                proc = process_registry.get(evt_sid)
                if not evt_session_key:
                    evt_session_key = str(getattr(proc, 'session_key', '') or '')
                if not evt_origin_ui_session_id:
                    evt_origin_ui_session_id = (
                        str(getattr(proc, 'origin_ui_session_id', '') or '')
                        or str(getattr(proc, 'spawn_session_id', '') or '')
                    )
        except Exception:
            evt_session_key = ''
            evt_origin_ui_session_id = ''

        # origin_ui_session_id is the exact, immutable return address and is
        # authoritative over the mutable session-key index (mirrors the
        # background _process_one path via _resolve_completion_target). When it
        # is present, this drain claims/ACKs the event ONLY for the origin
        # session — otherwise the next-turn drain could win the shared-queue
        # race and deliver+ACK a completion to the wrong (session-key-index)
        # session, leaving the true origin empty. Fall back to the session-key
        # target check only for legacy events that carry no origin address.
        if evt_origin_ui_session_id:
            if evt_origin_ui_session_id != session_id:
                skipped_events.append(evt)
                continue
        elif not _completion_event_targets_webui_session(evt_session_key, session_id):
            skipped_events.append(evt)
            continue
        # Age-gate stale completions: a completion that fires long after the
        # user moved on must not be prepended to an unrelated later turn
        # (nesquena/hermes-webui#4029). Drop (consume, do not requeue) any
        # completion whose enqueue time is older than the configured cap.
        # Events without a 'completed_at' (older agent builds) are never
        # dropped here, preserving backward-compatible behavior.
        is_stale = False
        stale_age = 0.0
        if stale_completion_max_age > 0 and isinstance(evt, dict):
            completed_at = evt.get('completed_at')
            if isinstance(completed_at, (int, float)) and completed_at > 0:
                stale_age = time.time() - completed_at
                is_stale = stale_age > stale_completion_max_age

        if is_async_delegation:
            try:
                claim = claim_async_delegation_delivery(evt, "webui-next-turn")
            except Exception:
                skipped_events.append(evt)
                continue
            if claim is None:
                schedule_async_delegation_claim_retry(evt, completion_queue)
                continue
            notification_added = False
            try:
                if is_stale:
                    notification = ''
                else:
                    notification = _format_process_notification(evt)
                    if not notification:
                        raise ValueError(
                            "async delegation formatter returned an empty notification"
                        )
                if notification:
                    notifications.append(notification)
                    notification_added = True
                if is_stale:
                    # Stale async events are an explicit terminal disposition.
                    complete_async_delegation_delivery(evt, claim)
                elif pending_async_acceptances is not None:
                    pending_async_acceptances.append(
                        (evt, claim, notification, completion_queue)
                    )
                else:
                    # Direct callers without a live agent turn retain the
                    # historical synchronous acceptance behavior used by
                    # CLI-style drains.
                    complete_async_delegation_delivery(evt, claim)
            except Exception:
                if notification_added:
                    notifications.pop()
                release_async_delegation_delivery(evt, claim)
                async_retry_events.append(
                    (evt, bool(getattr(claim, "durable", False)))
                )
                logger.warning(
                    "Failed to accept async delegation completion for session %s",
                    session_id,
                    exc_info=True,
                )
                continue
            if is_stale:
                logger.info(
                    "Dropping stale async-delegation completion for session %s "
                    "(age %.0fs > cap %.0fs)",
                    evt_sid, stale_age, stale_completion_max_age,
                )
            continue

        if is_stale:
            logger.info(
                "Dropping stale background-process completion for "
                "session %s (age %.0fs > cap %.0fs)",
                evt_sid, stale_age, stale_completion_max_age,
            )
            _mark_process_completion_consumed(process_registry, evt_sid)
            continue

        notification = _format_process_notification(evt)
        if notification:
            notifications.append(notification)
        # Matched but unformattable process completions are consumed rather than
        # replayed forever on later turns.
        _mark_process_completion_consumed(process_registry, evt_sid)

    for evt, durable in async_retry_events:
        requeue_async_delegation_event(
            evt,
            completion_queue,
            durable=durable,
        )
    for evt in skipped_events:
        try:
            completion_queue.put(evt)
        except Exception:
            logger.debug("Failed to requeue process completion event", exc_info=True)
            break
    return notifications


def _accept_pending_async_delegations(
    pending_async_acceptances: list,
    *,
    session_id: str,
) -> list[str]:
    """ACK turn-bound delegation claims and return rejected notifications."""
    rejected_notifications: list[str] = []
    for evt, claim, notification, completion_queue in pending_async_acceptances:
        try:
            complete_async_delegation_delivery(evt, claim)
        except Exception:
            release_async_delegation_delivery(evt, claim)
            requeue_async_delegation_event(
                evt,
                completion_queue,
                durable=bool(getattr(claim, "durable", False)),
            )
            rejected_notifications.append(notification)
            logger.warning(
                "Async delegation was not accepted into session %s; retrying later",
                session_id,
                exc_info=True,
            )
    return rejected_notifications


def _attachment_name(att) -> str:
    return _streaming_attachments.attachment_name(_streaming_api(), att)


_IMAGE_MAGIC: dict[bytes | None, frozenset[str]] = {
    b'\x89PNG\r\n\x1a\n': frozenset({'image/png'}),
    b'\xff\xd8\xff': frozenset({'image/jpeg'}),
    b'GIF87a': frozenset({'image/gif'}),
    b'GIF89a': frozenset({'image/gif'}),
    b'RIFF': frozenset({'image/webp'}),
    b'BM': frozenset({'image/bmp'}),
    None: frozenset({'image/svg+xml'}),
}


def _is_valid_image(path: Path, mime: str) -> bool:
    return _streaming_attachments.is_valid_image(_streaming_api(), path, mime)


def _explicit_text_signal(cfg: dict) -> bool:
    return _streaming_attachments.explicit_text_signal(_streaming_api(), cfg)


def _resolve_image_input_mode(cfg: dict) -> str:
    return _streaming_attachments.resolve_image_input_mode(_streaming_api(), cfg)


def _build_native_multimodal_message(
    workspace_ctx: str,
    msg_text: str,
    attachments,
    workspace: str,
    *,
    cfg: dict = None,
):
    return _streaming_attachments.build_native_multimodal_message(
        _streaming_api(),
        workspace_ctx,
        msg_text,
        attachments,
        workspace,
        cfg=cfg,
    )


_INLINE_THINKING_TAG_PAIRS = (
    ('<think>', '</think>'),
    ('<|channel>thought\n', '<channel|>'),
    ('<|turn|>thinking\n', '<turn|>'),
)


def _inline_thinking_fence_marker_at(text, index):
    return _streaming_thinking.inline_thinking_fence_marker_at(
        _streaming_api(),
        text,
        index,
    )


def _next_inline_thinking_opener(text, start):
    return _streaming_thinking.next_inline_thinking_opener(
        _streaming_api(),
        text,
        start,
    )


def _text_tail_is_partial_opener(text):
    return _streaming_thinking.text_tail_is_partial_opener(
        _streaming_api(),
        text,
    )


def _line_is_indented_code(text, line_start):
    return _streaming_thinking.line_is_indented_code(
        _streaming_api(),
        text,
        line_start,
    )


def _merge_inline_thinking_reasoning(existing_reasoning, extracted_parts):
    return _streaming_thinking.merge_inline_thinking_reasoning(
        _streaming_api(),
        existing_reasoning,
        extracted_parts,
    )


def _extract_inline_thinking_from_content(
    raw_content,
    existing_reasoning='',
    *,
    streaming=False,
):
    return _streaming_thinking.extract_inline_thinking_from_content(
        _streaming_api(),
        raw_content,
        existing_reasoning,
        streaming=streaming,
    )


def _split_thinking_from_content(raw_content, existing_reasoning=''):
    return _streaming_thinking.split_thinking_from_content(
        _streaming_api(),
        raw_content,
        existing_reasoning,
    )


def _strip_thinking_markup(text: str) -> str:
    return _streaming_thinking.strip_thinking_markup(_streaming_api(), text)


def _strip_xml_tool_calls(text: str) -> str:
    return _streaming_thinking.strip_xml_tool_calls(_streaming_api(), text)


def _sanitize_generated_title(text: str) -> str:
    return _streaming_thinking.sanitize_generated_title(_streaming_api(), text)


def _looks_invalid_generated_title(text: str) -> bool:
    return _streaming_thinking.looks_invalid_generated_title(
        _streaming_api(),
        text,
    )


def _structured_visible_text(value, *, depth: int = 0) -> str:
    return _streaming_thinking.structured_visible_text(
        _streaming_api(),
        value,
        depth=depth,
    )


def _message_content_part_text(part) -> str:
    return _streaming_thinking.message_content_part_text(
        _streaming_api(),
        part,
    )


def _message_text(value) -> str:
    return _streaming_thinking.message_text(_streaming_api(), value)


def _assistant_content_part_is_tool_use(part) -> bool:
    return _streaming_thinking.assistant_content_part_is_tool_use(
        _streaming_api(),
        part,
    )


def _assistant_message_has_final_visible_text(message) -> bool:
    return _streaming_thinking.assistant_message_has_final_visible_text(
        _streaming_api(),
        message,
    )



_WORKSPACE_PREFIX_RE = re.compile(r'^\s*\[Workspace::v1:\s*(?:\\.|[^\]\\])+\]\s*')
_LEGACY_WORKSPACE_PREFIX_RE = re.compile(r'^\s*\[Workspace:[^\]]+\]\s*')
_WORKSPACE_PREFIX_ANY_RE = re.compile(r'\[Workspace::v1:\s*(?:\\.|[^\]\\])+\]\s*')
_LEGACY_WORKSPACE_PREFIX_ANY_RE = re.compile(r'\[Workspace:[^\]]+\]\s*')


def _escape_workspace_prefix_path(path: str) -> str:
    return str(path or '').replace('\\', '\\\\').replace(']', '\\]')


def _workspace_context_prefix(path: str) -> str:
    return f"[Workspace::v1: {_escape_workspace_prefix_path(path)}]\n"


def _strip_workspace_prefix(text: str, *, include_legacy: bool = False) -> str:
    """Remove WebUI-injected workspace tags without eating user-typed text."""
    value = str(text or '')
    stripped = _WORKSPACE_PREFIX_RE.sub('', value, count=1)
    if include_legacy and stripped == value:
        stripped = _LEGACY_WORKSPACE_PREFIX_RE.sub('', value, count=1)
    return stripped.strip()


def _looks_like_current_user_turn(msg, msg_text) -> bool:
    """Match the current human turn even if an internal workspace tag leaked mid-text.

    Normal model-facing messages start with the workspace sentinel. A failed
    retry/merge path can also return an optimistic draft followed by the
    sentinel and the real prompt. Only treat that shape as the current turn
    when the text after the sentinel exactly matches the submitted prompt.
    """
    if not isinstance(msg, dict) or msg.get('role') != 'user':
        return False
    needle = " ".join(str(msg_text or '').split())
    if not needle:
        return False
    text = _message_text(msg.get('content', ''))
    candidates = [_strip_workspace_prefix(text, include_legacy=True)]
    for pattern in (_WORKSPACE_PREFIX_ANY_RE, _LEGACY_WORKSPACE_PREFIX_ANY_RE):
        for match in pattern.finditer(text):
            candidates.append(text[match.end():])
    return any(" ".join(str(candidate or '').split()) == needle for candidate in candidates)


def _first_exchange_snippets(messages):
    return _streaming_titles.first_exchange_snippets(
        _streaming_api(),
        messages,
    )


def _latest_exchange_snippets(messages):
    return _streaming_titles.latest_exchange_snippets(
        _streaming_api(),
        messages,
    )


def _count_exchanges(messages):
    return _streaming_titles.count_exchanges(
        _streaming_api(),
        messages,
    )


def _get_title_refresh_interval() -> int:
    return _streaming_titles.get_title_refresh_interval(
        _streaming_api(),
    )


def _is_provisional_title(current_title: str, messages) -> bool:
    return _streaming_titles.is_provisional_title(
        _streaming_api(),
        current_title,
        messages,
    )


def _detect_title_language(text: str) -> str:
    return _streaming_titles.detect_title_language(
        _streaming_api(),
        text,
    )


def _script_counts(text: str) -> dict:
    return _streaming_titles.script_counts(
        _streaming_api(),
        text,
    )


def _dominant_script(text: str) -> str:
    return _streaming_titles.dominant_script(
        _streaming_api(),
        text,
    )


def _title_prompt_language_rule(user_text: str) -> str:
    return _streaming_titles.title_prompt_language_rule(
        _streaming_api(),
        user_text,
    )


def _title_language_mismatch(user_text: str, title: str) -> bool:
    return _streaming_titles.title_language_mismatch(
        _streaming_api(),
        user_text,
        title,
    )


def _title_prompts(user_text: str, assistant_text: str) -> tuple[str, list[str]]:
    return _streaming_titles.title_prompts(
        _streaming_api(),
        user_text,
        assistant_text,
    )


def _is_minimax_route(provider: str = '', model: str = '', base_url: str = '') -> bool:
    return _streaming_titles.is_minimax_route(
        _streaming_api(),
        provider,
        model,
        base_url,
    )


def _route_rejects_reasoning_extra(provider: str = '', model: str = '', base_url: str = '') -> bool:
    return _streaming_titles.route_rejects_reasoning_extra(
        _streaming_api(),
        provider,
        model,
        base_url,
    )


def _get_aux_title_config() -> dict:
    return _streaming_titles.get_aux_title_config(
        _streaming_api(),
    )


def _aux_title_configured() -> bool:
    return _streaming_titles.aux_title_configured(
        _streaming_api(),
    )

def _aux_title_timeout(default: float = 15.0) -> float:
    return _streaming_titles.aux_title_timeout(
        _streaming_api(),
        default,
    )

def _title_completion_budget(provider: str = '', model: str = '', base_url: str = '') -> int:
    return _streaming_titles.title_completion_budget(
        _streaming_api(),
        provider,
        model,
        base_url,
    )


def _title_retry_completion_budget(provider: str = '', model: str = '', base_url: str = '') -> int:
    return _streaming_titles.title_retry_completion_budget(
        _streaming_api(),
        provider,
        model,
        base_url,
    )


def _title_retry_status(status: str) -> bool:
    return _streaming_titles.title_retry_status(
        _streaming_api(),
        status,
    )


def _title_should_skip_remaining_attempts(status: str) -> bool:
    return _streaming_titles.title_should_skip_remaining_attempts(
        _streaming_api(),
        status,
    )


def _safe_obj_value(obj, key: str):
    return _streaming_titles.safe_obj_value(
        _streaming_api(),
        obj,
        key,
    )


def _safe_text_value(value) -> str:
    return _streaming_titles.safe_text_value(
        _streaming_api(),
        value,
    )


def _extract_title_response(resp, *, aux: bool = False) -> tuple[str, str]:
    return _streaming_titles.extract_title_response(
        _streaming_api(),
        resp,
        aux=aux,
    )


def generate_title_raw_via_aux(
    user_text: str,
    assistant_text: str,
    provider: str = '',
    model: str = '',
    base_url: str = '',
) -> tuple[Optional[str], str]:
    return _streaming_titles.generate_title_raw_via_aux(
        _streaming_api(),
        user_text,
        assistant_text,
        provider,
        model,
        base_url,
    )


def generate_title_raw_via_agent(agent, user_text: str, assistant_text: str) -> tuple[Optional[str], str]:
    return _streaming_titles.generate_title_raw_via_agent(
        _streaming_api(),
        agent,
        user_text,
        assistant_text,
    )


def _generate_llm_session_title_for_agent(agent, user_text: str, assistant_text: str) -> tuple[Optional[str], str, str]:
    return _streaming_titles.generate_llm_session_title_for_agent(
        _streaming_api(),
        agent,
        user_text,
        assistant_text,
    )


def _generate_llm_session_title_via_aux(
    user_text: str,
    assistant_text: str,
    agent=None,
    *,
    use_agent_model: bool = False,
) -> tuple[Optional[str], str, str]:
    return _streaming_titles.generate_llm_session_title_via_aux(
        _streaming_api(),
        user_text,
        assistant_text,
        agent,
        use_agent_model=use_agent_model,
    )


def _put_title_status(
    put_event,
    session_id: str,
    status: str,
    reason: str = '',
    title: str = '',
    raw_preview: str = '',
) -> None:
    return _streaming_titles.put_title_status(
        _streaming_api(),
        put_event,
        session_id,
        status,
        reason,
        title,
        raw_preview,
    )


def _fallback_title_from_exchange(user_text: str, assistant_text: str) -> Optional[str]:
    return _streaming_titles.fallback_title_from_exchange(
        _streaming_api(),
        user_text,
        assistant_text,
    )


def _is_generic_fallback_title(title: str) -> bool:
    return _streaming_titles.is_generic_fallback_title(
        _streaming_api(),
        title,
    )


def _run_background_title_update(session_id: str, user_text: str, assistant_text: str, placeholder_title: str, put_event, agent=None):
    return _streaming_titles.run_background_title_update(
        _streaming_api(),
        session_id,
        user_text,
        assistant_text,
        placeholder_title,
        put_event,
        agent,
    )


def _run_background_title_refresh(session_id: str, user_text: str, assistant_text: str, current_title: str, put_event, agent=None):
    return _streaming_titles.run_background_title_refresh(
        _streaming_api(),
        session_id,
        user_text,
        assistant_text,
        current_title,
        put_event,
        agent,
    )




def generate_session_title_for_session(
    session,
    *,
    prefer_latest: bool = False,
    agent=None,
) -> tuple[Optional[str], str, str]:
    return _streaming_titles.generate_session_title_for_session(
        _streaming_api(),
        session,
        prefer_latest=prefer_latest,
        agent=agent,
    )


def _preserve_pre_compression_snapshot(s, old_sid: str) -> None:
    """Persist old_sid as a read-only pre-compression snapshot.

    Context compression rotates the active WebUI session id from old_sid to the
    agent's new continuation id. The old JSON must remain on disk for lineage
    traversal, but it should not continue to appear as an active sidebar row.
    """
    old_path = SESSION_DIR / f'{old_sid}.json'
    if not old_path.exists():
        return
    try:
        existing_text = old_path.read_text(encoding='utf-8')
        try:
            existing = json.loads(existing_text)
            existing_msgs = len(existing.get('messages') or [])
            existing_snapshot = bool(existing.get('pre_compression_snapshot'))
        except (json.JSONDecodeError, ValueError):
            # Treat corrupt/malformed old JSON as missing history and rewrite it
            # from the in-memory pre-compression messages below. That is safer
            # than leaving an unreadable recovery snapshot behind.
            existing_msgs = -1
            existing_snapshot = False
        if len(s.messages) > existing_msgs:
            # In-memory messages are newer than the file; save the full old
            # snapshot from the current session object while preserving its
            # pre-existing parent_session_id lineage.
            saved_sid = s.session_id
            saved_snapshot = bool(getattr(s, 'pre_compression_snapshot', False))
            saved_pinned = bool(getattr(s, 'pinned', False))
            s.session_id = old_sid
            s.pre_compression_snapshot = True
            s.pinned = False
            # Stage-359 / PR #2295: clear runtime stream-state fields on the
            # archived snapshot so the sidebar does not reopen the parent as
            # a permanently-running session while the child already holds the
            # completed answer. The continuation session's live state is
            # restored from saved_* locals in the finally block.
            saved_active_stream_id = getattr(s, 'active_stream_id', None)
            saved_pending_user_message = getattr(s, 'pending_user_message', None)
            saved_pending_attachments = list(getattr(s, 'pending_attachments', []) or [])
            saved_pending_started_at = getattr(s, 'pending_started_at', None)
            saved_pending_user_source = getattr(s, 'pending_user_source', None)
            s.active_stream_id = None
            s.pending_user_message = None
            s.pending_attachments = []
            s.pending_started_at = None
            s.pending_user_source = None
            try:
                # skip_index=False so the snapshot appears in _index.json with
                # the pre_compression_snapshot marker. The sidebar projection
                # (#2285) reads that marker to hide the snapshot from active
                # rows while keeping the JSON discoverable for lineage traversal.
                s.save(touch_updated_at=False, skip_index=False)
                logger.info(
                    "Preserved pre-compression session %s (%d messages) to disk",
                    old_sid, len(s.messages),
                )
            finally:
                s.session_id = saved_sid
                s.pre_compression_snapshot = saved_snapshot
                s.pinned = saved_pinned
                s.active_stream_id = saved_active_stream_id
                s.pending_user_message = saved_pending_user_message
                s.pending_attachments = saved_pending_attachments
                s.pending_started_at = saved_pending_started_at
                s.pending_user_source = saved_pending_user_source
            return
        # Existing file is already at least as complete as memory; stamp only
        # the snapshot marker so index/sidebar projection can hide it without
        # rewriting a shorter messages array over a fuller transcript.
        from api.models import Session
        snapshot = Session.load(old_sid)
        if snapshot:
            snapshot.pre_compression_snapshot = True
            snapshot.pinned = False
            # Stage-359 Opus SHOULD-FIX: clear runtime fields on the loaded
            # snapshot too. If the disk snapshot was last persisted while the
            # parent was live, it could carry a stale active_stream_id /
            # pending_* over to disk. The sidebar projection filters snapshot
            # rows so this is latent today, but the contract should match the
            # primary branch above so future readers can trust snapshot files
            # to never contain live runtime state.
            snapshot.active_stream_id = None
            snapshot.pending_user_message = None
            snapshot.pending_attachments = []
            snapshot.pending_started_at = None
            snapshot.pending_user_source = None
            snapshot.save(touch_updated_at=False, skip_index=False)
            logger.info(
                "Marked pre-compression session %s as sidebar-hidden snapshot",
                old_sid,
            )
    except OSError:
        logger.debug("Could not read old session file before preservation")
    except Exception:
        logger.debug("Failed to preserve pre-compression session file", exc_info=True)


def _maybe_schedule_title_refresh(session, put_event, agent):
    return _streaming_titles.maybe_schedule_title_refresh(
        _streaming_api(),
        session,
        put_event,
        agent,
    )


def _strip_native_image_parts_from_content(content):
    return _streaming_message_sanitization.strip_native_image_parts_from_content(
        _streaming_api(),
        content,
    )


_OOB_USER_MESSAGE_BLOCK_RE = (
    _streaming_message_sanitization.OOB_USER_MESSAGE_BLOCK_RE
)


def _strip_oob_blocks(content):
    return _streaming_message_sanitization.strip_oob_blocks(
        _streaming_api(),
        content,
    )


def _content_has_reasoning_only_parts(content) -> bool:
    return _streaming_message_sanitization.content_has_reasoning_only_parts(
        _streaming_api(),
        content,
    )


def _is_reasoning_only_assistant_message(msg) -> bool:
    return _streaming_message_sanitization.is_reasoning_only_assistant_message(
        _streaming_api(),
        msg,
    )


def _is_local_reasoning_replay_base_url(base_url: str | None) -> bool:
    return _streaming_message_sanitization.is_local_reasoning_replay_base_url(
        _streaming_api(),
        base_url,
    )


def _should_strip_reasoning_content(
    cfg: dict | None,
    *,
    mode: str | None = None,
    effective_model: str | None = None,
    effective_provider: str | None = None,
    effective_base_url: str | None = None,
) -> bool:
    return _streaming_message_sanitization.should_strip_reasoning_content(
        _streaming_api(),
        cfg,
        mode=mode,
        effective_model=effective_model,
        effective_provider=effective_provider,
        effective_base_url=effective_base_url,
    )


def _sanitize_messages_for_api(
    messages,
    *,
    cfg: dict = None,
    effective_model: str | None = None,
    effective_provider: str | None = None,
    effective_base_url: str | None = None,
):
    return _streaming_message_sanitization.sanitize_messages_for_api(
        _streaming_api(),
        messages,
        cfg=cfg,
        effective_model=effective_model,
        effective_provider=effective_provider,
        effective_base_url=effective_base_url,
    )


def _api_safe_message_positions(messages):
    return _streaming_message_sanitization.api_safe_message_positions(
        _streaming_api(),
        messages,
    )


def _deduplicate_context_messages(messages):
    return _streaming_message_sanitization.deduplicate_context_messages(
        _streaming_api(),
        messages,
    )


def _assign_stable_message_ids(result_messages, *existing_arrays):
    return _streaming_message_sanitization.assign_stable_message_ids(
        _streaming_api(),
        result_messages,
        *existing_arrays,
    )


_POST_COMPRESSION_TOOL_RESULT_TOTAL_TOKENS = (
    _streaming_post_compression.POST_COMPRESSION_TOOL_RESULT_TOTAL_TOKENS
)
_POST_COMPRESSION_TOOL_RESULT_MIN_SNIPPET_TOKENS = (
    _streaming_post_compression.POST_COMPRESSION_TOOL_RESULT_MIN_SNIPPET_TOKENS
)
_POST_COMPRESSION_TOOL_RESULT_SUMMARY_FLAG = (
    _streaming_post_compression.POST_COMPRESSION_TOOL_RESULT_SUMMARY_FLAG
)
_POST_COMPRESSION_TOOL_RESULT_MARKER = (
    _streaming_post_compression.POST_COMPRESSION_TOOL_RESULT_MARKER
)
_ROUGH_TOKEN_CHARS = _streaming_post_compression.ROUGH_TOKEN_CHARS


def _positive_int_value(value, default: int = 0) -> int:
    return _streaming_post_compression.positive_int_value(_streaming_api(), value, default)


def _rough_text_token_count(text: str) -> int:
    return _streaming_post_compression.rough_text_token_count(_streaming_api(), text)


def _post_compression_tool_result_budget(compressor) -> int:
    return _streaming_post_compression.post_compression_tool_result_budget(
        _streaming_api(),
        compressor,
    )


def _compressed_context_tool_result_summary(text: str, *, original_tokens: int, keep_tokens: int) -> str:
    return _streaming_post_compression.compressed_context_tool_result_summary(
        _streaming_api(),
        text,
        original_tokens=original_tokens,
        keep_tokens=keep_tokens,
    )


def _is_compressed_context_tool_result_summary_message(msg) -> bool:
    return _streaming_post_compression.is_compressed_context_tool_result_summary_message(
        _streaming_api(),
        msg,
    )


def _hard_prune_post_compression_tool_results(messages, *, compressor=None):
    return _streaming_post_compression.hard_prune_post_compression_tool_results(
        _streaming_api(),
        messages,
        compressor=compressor,
    )


def _prune_context_tool_results_after_compression(agent, context_messages):
    """Run the active compressor's cheap tool-result pruning on model context.

    Auto-compression can happen mid-turn and then the agent may run more tools
    before producing the final answer. Those completed tail tool results are
    model-facing context, but they were produced after the compression pass and
    therefore did not go through the compressor's tool-output pruning. Apply the
    same cheap pruning once more after a confirmed compression event, then apply
    a WebUI hard cap to retained tool-result payloads. This keeps the visible
    transcript untouched while preventing the next turn from seeing raw
    post-compression tool dumps that the compressor protected as recent tail.
    """
    return _streaming_post_compression.prune_context_tool_results_after_compression(
        _streaming_api(),
        agent,
        context_messages,
    )


def _estimate_post_compression_context_tokens(agent, context_messages, system_message):
    return _streaming_post_compression.estimate_post_compression_context_tokens(
        _streaming_api(),
        agent,
        context_messages,
        system_message,
    )


def _restore_reasoning_metadata(previous_messages, updated_messages):
    """Carry forward display-only metadata lost during API-safe history sanitization.

    The provider-facing history strips WebUI-only fields like `reasoning`. When the
    agent returns its new full message history, prior assistant messages come back
    without that metadata unless we merge it back in by API-history position.

    This also preserves existing timestamps for unchanged historical messages.
    Without that, older turns that come back from the agent without `_ts` /
    `timestamp` can be re-stamped with the current time on every new assistant
    response, making prior messages appear to "move" in time.
    """
    return _streaming_post_compression.restore_reasoning_metadata(
        _streaming_api(),
        previous_messages,
        updated_messages,
    )


def _restore_display_reasoning_metadata(previous_messages, updated_messages):
    return _streaming_post_compression.restore_display_reasoning_metadata(
        _streaming_api(),
        previous_messages,
        updated_messages,
    )


def _session_context_messages(session):
    return _streaming_context_replay.session_context_messages(_streaming_api(), session)


def _message_identity(msg):
    return _streaming_context_replay.message_identity(_streaming_api(), msg)


def _messages_have_prefix(messages, prefix):
    return _streaming_context_replay.messages_have_prefix(_streaming_api(), messages, prefix)


def _message_replay_key(msg):
    return _streaming_context_replay.message_replay_key(_streaming_api(), msg)


def _strip_replayed_prefix(existing_messages, candidates):
    """Drop a candidate prefix that is already the suffix of existing_messages.

    Compression/continuation can replay the active tail from state.db after the
    previous WebUI context/display already contains it. Prefix-only merge logic
    then treats that replayed tail as a fresh delta and duplicates a whole turn.
    Strip the largest exact suffix/prefix overlap before appending.
    """
    return _streaming_context_replay.strip_replayed_prefix(
        _streaming_api(),
        existing_messages,
        candidates,
    )


def _looks_like_replayed_session_arc_summary(previous_msg, candidate_msg):
    """Return True for repeated LCM/session summaries with refreshed hints.

    LCM summary cards can be re-injected with the same long recovered context
    and a different tail such as an expand hint. Exact identity misses those,
    but appending both copies bloats every later model prompt.
    """
    return _streaming_context_replay.looks_like_replayed_session_arc_summary(
        _streaming_api(),
        previous_msg,
        candidate_msg,
    )


def _strip_replayed_context_items(existing_messages, candidates):
    return _streaming_context_replay.strip_replayed_context_items(
        _streaming_api(),
        existing_messages,
        candidates,
    )


def _dedupe_replayed_context_messages(previous_context, result_messages, msg_text=None):
    return _streaming_context_replay.dedupe_replayed_context_messages(
        _streaming_api(),
        previous_context,
        result_messages,
        msg_text,
    )


def _dedupe_replayed_active_context(previous_context, result_messages, msg_text=None):
    return _streaming_context_replay.dedupe_replayed_active_context(
        _streaming_api(),
        previous_context,
        result_messages,
        msg_text,
    )


def _is_context_compression_marker(msg):
    return _streaming_compression_anchors.is_context_compression_marker(
        _streaming_api(),
        msg,
    )


def _compact_summary_text(raw_text: str | None) -> str | None:
    """Normalize a text blob used in compression summary cards."""
    return _streaming_compression_anchors.compact_summary_text(
        _streaming_api(),
        raw_text,
    )


def _compression_anchor_message_key(message):
    return _streaming_compression_anchors.compression_anchor_message_key(
        _streaming_api(),
        message,
    )


def _compression_summary_from_messages(messages):
    return _streaming_compression_anchors.compression_summary_from_messages(
        _streaming_api(),
        messages,
    )


def _find_current_user_turn(messages, msg_text):
    return _streaming_compression_anchors.find_current_user_turn(
        _streaming_api(),
        messages,
        msg_text,
    )


def _drop_checkpointed_current_user_from_context(messages, msg_text):
    """Return model history without an eager-checkpointed current user turn."""
    return _streaming_compression_anchors.drop_checkpointed_current_user_from_context(
        _streaming_api(),
        messages,
        msg_text,
    )


def _strip_workspace_prefixes_for_compare(text: str) -> str:
    """Remove WebUI workspace sentinels anywhere before text comparison."""
    return _streaming_stale_user_context.strip_workspace_prefixes_for_compare(
        _streaming_api(),
        text,
    )


def _normalize_user_text(text):
    """Collapse whitespace and strip workspace sentinels for tail comparisons."""
    return _streaming_stale_user_context.normalize_user_text(_streaming_api(), text)


def _raw_message_text(value) -> str:
    """Extract text from a message content payload without stripping markup.

    Used for the stale-user-merge detector so the literal boundary between
    the prior tail and the current turn survives into the comparison. The
    thinking-markup strip in ``_message_text`` collapses newlines, which
    would defeat the boundary check.
    """
    return _streaming_stale_user_context.raw_message_text(_streaming_api(), value)


def _stale_user_tail_candidate(msg):
    """Return normalized text if msg is a user row that could be a stale tail."""
    return _streaming_stale_user_context.stale_user_tail_candidate(_streaming_api(), msg)


def _last_user_row(messages):
    """Return the last user-role row in `messages`, or None."""
    return _streaming_stale_user_context.last_user_row(_streaming_api(), messages)


def _stale_prefix_matches_prior_user_context(stale_prefix, stale_segments, previous_context):
    """Return True when a stale prefix is explainable by prior user context.

    First-generation repair usually produces segments matching consecutive
    prior user rows. Once a session is already contaminated, later repair can
    replay a stable stale prefix from an older polluted row even after newer
    clean user turns have moved the context tail forward. Handle both shapes
    while still requiring all evidence to come from prior user-role rows.
    """
    return _streaming_stale_user_context.stale_prefix_matches_prior_user_context(
        _streaming_api(),
        stale_prefix,
        stale_segments,
        previous_context,
    )


def _detect_stale_user_merge(message, msg_text, previous_user_tail, previous_context=None):
    """Return True if `message` is the current user turn with a stale prefix merged in.

    The agent's defensive repair path can concatenate prior user context with
    the submitted current turn as ``<stale>\\n\\n<current>``. The stale portion
    can be either the immediate prior user tail or a replayed prefix from an
    older already-polluted user row. The literal ``\\n\\n`` boundary must survive
    into the comparison; a single-newline or space-only join is not the repair
    shape and must not match. Workspace sentinels may be present on either or
    both halves and are stripped before comparison.
    """
    return _streaming_stale_user_context.detect_stale_user_merge(
        _streaming_api(),
        message,
        msg_text,
        previous_user_tail,
        previous_context,
    )


def _strip_stale_user_merge_from_messages(
    messages,
    msg_text,
    previous_user_tail,
    previous_context=None,
):
    """Return messages with stale-prefixed current user turns replaced by clean ones.

    Both context-merge (model-facing) and display-merge (visible transcript)
    callers funnel through this so a single detection rule governs persistence.
    The current user row is replaced with a clean copy using `msg_text` so the
    displayed bubble matches what the human submitted, never the polluted pair.
    """
    return _streaming_stale_user_context.strip_stale_user_merge_from_messages(
        _streaming_api(),
        messages,
        msg_text,
        previous_user_tail,
        previous_context,
    )


def _save_streaming_checkpoint(session):
    """Persist a streaming checkpoint under the session's profile context."""
    return _streaming_turn_context.save_streaming_checkpoint(_streaming_api(), session)


def _normalize_fresh_chat_text(text):
    return _streaming_turn_context.normalize_fresh_chat_text(_streaming_api(), text)


def _is_casual_fresh_chat_message(msg_text):
    """Return True for short opener messages that should not resume old tasks."""
    return _streaming_turn_context.is_casual_fresh_chat_message(
        _streaming_api(),
        msg_text,
    )


def _has_task_resume_compaction_marker(messages):
    """Detect compacted model context that tells the agent to resume an old task."""
    return _streaming_turn_context.has_task_resume_compaction_marker(
        _streaming_api(),
        messages,
    )


def _new_turn_context_from_messages(messages, msg_text):
    """Return provider-facing history for a new user turn from a message list."""
    return _streaming_turn_context.new_turn_context_from_messages(
        _streaming_api(),
        messages,
        msg_text,
    )


def _context_messages_for_new_turn(session, msg_text):
    """Return provider-facing history for a new user turn.

    Compacted agent sessions can carry a hidden "resume the active task" summary
    in context_messages. If the user starts a fresh casual greeting in that old
    session, do not feed that stale active-task summary back to the model.
    """
    return _streaming_turn_context.context_messages_for_new_turn(
        _streaming_api(),
        session,
        msg_text,
    )


def _stream_writeback_is_current(session, stream_id):
    """Return True only while a worker still owns the session writeback.

    cancel_stream() intentionally clears ``active_stream_id`` early so the UI can
    accept a follow-up turn while the old worker is unwinding. That old worker
    must not later persist its stale result over the newer transcript.
    """
    return _streaming_turn_context.stream_writeback_is_current(
        _streaming_api(),
        session,
        stream_id,
    )


def _stream_writeback_can_supersede_recovery_marker(session, msg_text):
    """Allow a finishing worker to replace its own stale-repair marker.

    The stale-pending repair path can occasionally run while the original worker
    is still alive but temporarily missing from the in-memory stream registry. It
    clears ``active_stream_id`` and appends a "Response interrupted" marker. If
    the original worker later finishes, treating ``active_stream_id is None`` as
    stale drops the real answer and leaves the misleading marker visible.

    This is intentionally narrow: only a session with no active/pending turn and
    whose last visible row is the recovery marker for this exact user prompt may
    be superseded. If a newer turn has appended anything after the marker, the
    normal stale-writeback guard still wins.
    """
    return _streaming_turn_context.stream_writeback_can_supersede_recovery_marker(
        _streaming_api(),
        session,
        msg_text,
    )


def _advance_truncation_watermark_after_commit(session) -> None:
    """Advance a positive truncation watermark once a new user turn is committed
    to ``session.messages`` (#3831).

    retry/undo/Edit set a positive watermark to suppress the *replaced* tail from
    the append-only state.db merge; Session.save() deliberately never auto-clears
    it (#2914). Once the new turn is durably in messages we advance the watermark
    to the newest user message timestamp so that state.db rows newer than the
    watermark are still merged in, while the replaced pre-edit tail remains
    filtered. Never 0.0 (the truncate-to-empty sentinel that must keep blocking
    replay, #2914).
    """
    return _streaming_turn_context.advance_truncation_watermark_after_commit(
        _streaming_api(),
        session,
    )


def _merge_display_messages_after_agent_result(previous_display, previous_context, result_messages, msg_text, source: str = "webui"):
    """Keep UI transcript durable while allowing model context to compact.

    If Hermes Agent returns a normal append-only history, append that delta to
    the UI transcript. If the model/context history was compacted and no longer
    has the prior context as a prefix, keep the previous UI transcript and append
    the current user turn onward. Synthetic compaction/reference markers remain
    internal recovery material and must not become visible user/assistant turns.
    """
    previous_display = [
        m for m in list(previous_display or [])
        if not _is_context_compression_marker(m)
        and not _is_compressed_context_tool_result_summary_message(m)
    ]
    # Drop Hermes Agent internal verify-loop scaffolding (synthetic "premature
    # done" answer + the "[System: ...verification evidence...]" nudge) before
    # it can become a visible user/assistant turn. The agent flags these with
    # structured markers (_verification_stop_synthetic / _pre_verify_synthetic)
    # and already keeps them out of its own durable store; honor the same
    # markers here so they never leak into the WebUI transcript. Filter all
    # three inputs consistently so prefix/delta detection below stays aligned.
    # (#5334; same internal-control-message class as #3320/#3821/#4373/#4875)
    previous_display = _drop_synthetic_control_messages(previous_display)
    # Deduplicate stale _partial messages that accumulated in previous_display.
    # A bug in cancel_stream() could insert multiple identical _partial messages
    # when _stripped was empty but _has_reasoning/_has_tools was True. The
    # merge's _message_identity previously returned None for empty _partial
    # messages, so the seen-set couldn't catch them — they doubled each turn.
    # Scan backwards and keep only the LAST occurrence of each unique _partial
    # identity, then reverse back to original order.
    _partial_seen = set()
    _deduped_rev = []
    for m in reversed(previous_display):
        if isinstance(m, dict) and m.get('_partial'):
            key = _message_identity(m)
            if key is not None:
                if key in _partial_seen:
                    continue
                _partial_seen.add(key)
        _deduped_rev.append(m)
    _deduped = list(reversed(_deduped_rev))
    if len(_deduped) < len(previous_display):
        logger.debug(
            "Deduplicated %d stale _partial messages from previous_display (was %d, now %d)",
            len(previous_display) - len(_deduped), len(previous_display), len(_deduped),
        )
    previous_display = _deduped
    previous_context = list(previous_context or [])
    result_messages = list(result_messages or [])
    # Same marker filter for the model-history inputs: the synthetic verify-loop
    # answer/nudge live in the agent's returned messages and prior context, and
    # would otherwise slip into the merged transcript as a real delta. (#5334)
    previous_context = _drop_synthetic_control_messages(previous_context)
    result_messages = _drop_synthetic_control_messages(result_messages)
    if not result_messages:
        return previous_display
    previous_user_tail = _stale_user_tail_candidate(_last_user_row(previous_context))

    # ── Backfill normal turns from previous_context that are missing from
    # previous_display.  After context compression recovery, previous_context
    # can contain user/assistant turns that were never rendered in the visible
    # transcript (they were behind a compression marker). On the next
    # append-only merge those turns sit inside the shared prefix and get
    # stripped, leaving them permanently invisible.  Reinsert them now.
    #
    # Use display as the backbone to preserve visible order. Walk display in
    # order and for each display message search for its identity in context
    # at/after a cursor. Any context messages between the cursor and that
    # match are context-only gaps that get spliced in before the display msg.
    if previous_display and previous_context:
        _display_id_set = {_message_identity(m) for m in previous_display}
        _context_id_set = {
            _message_identity(m)
            for m in previous_context
            if not _is_context_compression_marker(m)
            and not _is_compressed_context_tool_result_summary_message(m)
        }
        _has_context_only_turns = bool(_context_id_set - _display_id_set)
        if _has_context_only_turns:
            context_keys = [_message_identity(m) for m in previous_context]
            # Precompute display keys once; avoids repeated json.dumps calls inside
            # the inner any() loop (was O(D²·C) — see perf fix below).
            _display_keys = [_message_identity(m) for m in previous_display]
            # Multiset mirror of context_keys[_cursor:] kept in sync as _cursor
            # advances. Enables O(1) membership tests in the any() check instead
            # of an O(N) list scan, while preserving EXACT list-slice semantics:
            # _message_identity intentionally returns duplicate keys for
            # identical-content turns (and None for empty rows), so a plain set
            # would drop a key still present later in the slice. A count-keyed
            # dict (including None) matches `in context_keys[_cursor:]` exactly.
            _remaining_ck_counts = {}
            for _ck in context_keys:
                _remaining_ck_counts[_ck] = _remaining_ck_counts.get(_ck, 0) + 1
            _backfilled = []
            # #3300 fix: track ONLY context rows we splice in, so the
            # visible-display backbone is never suppressed. Sharing one set
            # between context inserts and display rows (and _message_identity
            # ignoring timestamps) dropped a legitimate second identical visible
            # user turn. Display rows are always appended in order; a context
            # row is backfilled only if it isn't already a display row and
            # hasn't already been inserted.
            _context_inserted = set()
            _cursor = 0
            for _display_idx, _dmsg in enumerate(previous_display):
                _dkey = _display_keys[_display_idx]
                if _dkey is not None:
                    _j = _cursor
                    while _j < len(context_keys) and context_keys[_j] != _dkey:
                        _j += 1
                    if _j < len(context_keys):
                        for _k in range(_cursor, _j):
                            _ckey = context_keys[_k]
                            _cmsg = previous_context[_k]
                            if (
                                _ckey is not None
                                and _ckey not in _context_inserted
                                and _ckey not in _display_id_set
                                and not _is_context_compression_marker(_cmsg)
                                and not _is_compressed_context_tool_result_summary_message(_cmsg)
                            ):
                                _backfilled.append(copy.deepcopy(_cmsg))
                                _context_inserted.add(_ckey)
                        # Sync multiset: decrement keys consumed by advancing
                        # the cursor to _j+1 (delete at zero so membership matches
                        # the list slice exactly).
                        for _k in range(_cursor, _j + 1):
                            _consumed_ck = context_keys[_k]
                            _ck_n = _remaining_ck_counts.get(_consumed_ck, 0) - 1
                            if _ck_n <= 0:
                                _remaining_ck_counts.pop(_consumed_ck, None)
                            else:
                                _remaining_ck_counts[_consumed_ck] = _ck_n
                        _cursor = _j + 1
                    elif not any(
                        _display_keys[_fi] in _remaining_ck_counts
                        for _fi in range(_display_idx + 1, len(_display_keys))
                    ):
                        for _k in range(_cursor, len(context_keys)):
                            _ckey = context_keys[_k]
                            _cmsg = previous_context[_k]
                            if (
                                _ckey is not None
                                and _ckey not in _context_inserted
                                and _ckey not in _display_id_set
                                and not _is_context_compression_marker(_cmsg)
                                and not _is_compressed_context_tool_result_summary_message(_cmsg)
                            ):
                                _backfilled.append(copy.deepcopy(_cmsg))
                                _context_inserted.add(_ckey)
                        _cursor = len(context_keys)
                        _remaining_ck_counts.clear()
                # The display row is the visible backbone — always preserve it,
                # in order, even when an earlier (identical-content) turn or a
                # backfilled context row shares its timestamp-less identity.
                _backfilled.append(_dmsg)
            while _cursor < len(context_keys):
                _ckey = context_keys[_cursor]
                _cmsg = previous_context[_cursor]
                _cursor += 1
                if (
                    _ckey is not None
                    and _ckey not in _context_inserted
                    and _ckey not in _display_id_set
                    and not _is_context_compression_marker(_cmsg)
                    and not _is_compressed_context_tool_result_summary_message(_cmsg)
                ):
                    _backfilled.append(copy.deepcopy(_cmsg))
                    _context_inserted.add(_ckey)
            if len(_backfilled) > len(previous_display):
                logger.debug(
                    "Backfilled %d context-only turns into previous_display (was %d, now %d)",
                    len(_backfilled) - len(previous_display),
                    len(previous_display),
                    len(_backfilled),
                )
                previous_display = _backfilled

    if _messages_have_prefix(result_messages, previous_context):
        candidates = result_messages[len(previous_context):]
        # Normalize stale merges only in the new-turn slice; never rewrite
        # historical rows in the already-committed previous_context prefix.
        if msg_text and previous_user_tail:
            candidates = _strip_stale_user_merge_from_messages(
                candidates,
                msg_text,
                previous_user_tail,
                previous_context=previous_context,
            )
        current_user_key = _message_identity({'role': 'user', 'content': msg_text})
        current_user_in_candidates = any(
            _message_identity(m) == current_user_key or _looks_like_current_user_turn(m, msg_text)
            for m in candidates
        )
        assistant_or_tool_only_candidates = bool(candidates) and all(
            _is_context_compression_marker(m)
            or (
                isinstance(m, dict)
                and m.get('role') in ('assistant', 'tool')
            )
            for m in candidates
        )
        if not (assistant_or_tool_only_candidates and not current_user_in_candidates):
            candidates = _strip_replayed_prefix(previous_display, candidates)
            candidates = _strip_replayed_prefix(previous_context, candidates)
    else:
        current_user_idx = _find_current_user_turn(result_messages, msg_text)
        turn_candidates = result_messages[current_user_idx:] if current_user_idx is not None else []
        # Normalize stale merges only in the current-turn slice.
        if msg_text and previous_user_tail:
            turn_candidates = _strip_stale_user_merge_from_messages(
                turn_candidates,
                msg_text,
                previous_user_tail,
                previous_context=previous_context,
            )
        candidates = turn_candidates

    merged = previous_display[:]
    seen = {_message_identity(m) for m in merged}
    current_user_key = _message_identity({'role': 'user', 'content': msg_text})
    current_user_in_candidates = any(
        _message_identity(m) == current_user_key or _looks_like_current_user_turn(m, msg_text)
        for m in candidates
    )
    current_user_already_checkpointed = bool(
        merged
        and (
            _message_identity(merged[-1]) == current_user_key
            or _looks_like_current_user_turn(merged[-1], msg_text)
        )
    )
    if (
        current_user_key is not None
        and not current_user_in_candidates
        and not current_user_already_checkpointed
        and any(
            isinstance(m, dict) and m.get('role') in ('assistant', 'tool')
            for m in candidates
        )
    ):
        # Some provider retry/fallback paths can return an assistant/tool delta
        # without echoing the current user turn. In deferred session-save mode
        # the prompt exists only in pending_user_message, so appending that delta
        # directly would make the assistant bubble appear attached to the prior
        # exchange and then clear the pending prompt. Materialize the current
        # turn at the transcript boundary before the assistant/tool response.
        current_user_msg = {'role': 'user', 'content': msg_text}
        if source and source != 'webui':
            current_user_msg['_source'] = source
        insert_at = 0
        while insert_at < len(candidates) and _is_context_compression_marker(candidates[insert_at]):
            insert_at += 1
        candidates = candidates[:insert_at] + [current_user_msg] + candidates[insert_at:]

    for msg in candidates:
        if (
            _is_context_compression_marker(msg)
            or _is_compressed_context_tool_result_summary_message(msg)
        ):
            continue
        key = _message_identity(msg)
        is_current_user_turn = _looks_like_current_user_turn(msg, msg_text)
        if (
            ((key is not None and key == current_user_key) or is_current_user_turn)
            and merged
            and (
                _message_identity(merged[-1]) == current_user_key
                or _looks_like_current_user_turn(merged[-1], msg_text)
            )
        ):
            # Eager session-save mode can checkpoint the current user turn
            # before the agent runs. When the agent returns that same user turn
            # in result_messages, keep the durable checkpoint and append only
            # the assistant/tool delta.
            # The eager checkpoint was written before _assign_stable_message_ids
            # stamped the result rows, so it has no `id` while the context copy
            # does — which would silently defeat id-based fork/truncate alignment
            # for eager-mode users. Carry the minted id onto the kept checkpoint
            # so display and context share it (#5564).
            if (
                isinstance(msg, dict)
                and msg.get('id') is not None
                and isinstance(merged[-1], dict)
                and merged[-1].get('id') is None
            ):
                merged[-1]['id'] = msg['id']
            continue
        if (
            key is not None
            and isinstance(msg, dict)
            and msg.get('role') == 'assistant'
            and merged
            and _message_identity(merged[-1]) == key
        ):
            # Some provider/result replay paths can include the same assistant
            # message twice in the current delta. Treat only adjacent identity
            # matches as replay duplicates so identical answers in separate
            # user turns remain visible.
            continue
        if _is_context_compression_marker(msg) and key is not None and key in seen:
            continue
        display_msg = msg
        if (
            ((key is not None and key == current_user_key) or is_current_user_turn)
            and isinstance(msg, dict)
            and msg.get('role') == 'user'
        ):
            display_msg = copy.deepcopy(msg)
            display_msg['content'] = msg_text
            if source and source != 'webui':
                display_msg['_source'] = source
        merged.append(copy.deepcopy(display_msg))
        if key is not None:
            seen.add(key)
    return merged


def _stamp_missing_message_timestamps(messages, *, now: float | None = None) -> int:
    """Stamp missing message timestamps without collapsing transcript order.

    Compacted/reconciled rows can arrive without timestamps. Assigning one
    integer seconds value to the whole batch makes later timestamp-based display
    merges unstable; use a subsecond sequence instead.
    """
    base = time.time() if now is None else float(now)
    stamped = 0
    for msg in messages or []:
        if isinstance(msg, dict) and not msg.get('timestamp') and not msg.get('_ts'):
            msg['timestamp'] = base + (stamped * 0.000001)
            stamped += 1
    return stamped


def _assistant_reply_added_after_current_turn(result_messages, previous_context, msg_text) -> bool:
    """Return True only when the just-finished turn produced assistant text."""
    result_messages = list(result_messages or [])
    previous_context = list(previous_context or [])
    if _messages_have_prefix(result_messages, previous_context):
        candidates = result_messages[len(previous_context):]
    else:
        current_user_idx = _find_current_user_turn(result_messages, msg_text)
        candidates = result_messages[current_user_idx + 1:] if current_user_idx is not None else result_messages
    return any(
        isinstance(m, dict)
        and m.get('role') == 'assistant'
        and not m.get('_error')
        and _assistant_message_has_final_visible_text(m)
        for m in candidates
    )


def _session_lacks_final_assistant_answer(messages) -> bool:
    """Return True when the persisted transcript ends before a final answer."""
    for msg in reversed(list(messages or [])):
        if not isinstance(msg, dict):
            continue
        if msg.get('_error'):
            return False
        if _is_context_compression_marker(msg):
            continue
        role = msg.get('role')
        if role == 'tool':
            return True
        if role == 'assistant':
            if _assistant_message_has_final_visible_text(msg):
                return False
            continue
        if role == 'user':
            return True
    return True


def _turn_transcript_lacks_final_assistant_answer(
    merged_messages,
    previous_display,
    msg_text,
    source: str = "webui",
    drop_replayed_assistant: bool = False,
) -> bool:
    """Return True when an already-merged transcript still lacks a final assistant answer."""
    merged_messages = list(merged_messages or [])
    previous_display = list(previous_display or [])
    current_user_idx = _find_current_user_turn(merged_messages, msg_text)
    if current_user_idx is None or current_user_idx < len(previous_display):
        # The active turn lives after the durable transcript boundary. If the
        # merged display only exposes an older user row, materialize the pending
        # prompt so a replayed assistant row cannot satisfy the wrong turn.
        pending_user = {
            'role': 'user',
            'content': msg_text,
        }
        if source and source != 'webui':
            pending_user['_source'] = source
        merged_messages.append(pending_user)
        current_user_idx = len(merged_messages) - 1

    current_user_key = _message_identity(merged_messages[current_user_idx])
    filtered_messages = merged_messages[:current_user_idx + 1]
    if drop_replayed_assistant:
        prior_id_set = {
            _message_identity(msg)
            for msg in merged_messages[:current_user_idx]
            if isinstance(msg, dict)
        }
        for msg in merged_messages[current_user_idx + 1:]:
            if not isinstance(msg, dict):
                filtered_messages.append(msg)
                continue
            if msg.get('role') == 'assistant':
                key = _message_identity(msg)
                if key is not None and key in prior_id_set:
                    continue
            filtered_messages.append(msg)
    else:
        filtered_messages.extend(merged_messages[current_user_idx + 1:])
    if current_user_key is not None:
        filtered_messages = [
            msg for msg in filtered_messages
            if _message_identity(msg) != current_user_key or msg is merged_messages[current_user_idx]
        ]
    return _session_lacks_final_assistant_answer(filtered_messages)


def _merged_transcript_lacks_final_assistant_answer(
    previous_display,
    previous_context,
    result_messages,
    msg_text,
    source: str = "webui",
    drop_replayed_assistant: bool = False,
) -> bool:
    """Return True when the current turn still lacks a final assistant answer."""
    previous_display = list(previous_display or [])
    merged_messages = _merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        _restore_reasoning_metadata(previous_display, result_messages),
        msg_text,
        source=source,
    )
    return _turn_transcript_lacks_final_assistant_answer(
        merged_messages,
        previous_display,
        msg_text,
        source=source,
        drop_replayed_assistant=drop_replayed_assistant,
    )


def _agent_result_terminal_failure(result) -> bool:
    """Return True for agent results that must not be finalized as done."""
    if not isinstance(result, dict):
        return False
    status = str(result.get('status') or result.get('state') or '').strip().lower()
    if status in {'failed', 'error', 'partial', 'compression_exhausted'}:
        return True
    if result.get('compression_exhausted'):
        return True
    if result.get('failed') or result.get('partial'):
        return True
    return False


_TOOL_RESULT_SNIPPET_MAX = 4000

# Tool-arg keys whose values are card content / diff-reconstruction inputs.
# These must not be capped to the short incidental-arg limit (#4928), or long
# commands/paths get cut and recovery-rebuilt diffs (built from old_string/
# new_string/patch) break. Matched case-insensitively against the arg key.
_TOOL_ARG_CONTENT_KEYS = frozenset({
    'command', 'cmd', 'script', 'code', 'patch', 'diff',
    'old_string', 'new_string', 'content', 'path', 'file_path',
})
_TOOL_ARG_CONTENT_CAP = _TOOL_RESULT_SNIPPET_MAX


_LIVE_TOOL_PROMPT_DELTA_MAX = 12_000
_LIVE_TOOL_PROMPT_TURN_MAX = 24_000


def _bounded_live_tool_prompt_delta(messages, *, cap: int = _LIVE_TOOL_PROMPT_DELTA_MAX) -> int:
    """Return a bounded rough token delta for live tool metering.

    Tool-result callbacks can fire before the agent's next exact prompt accounting
    is available. The live usage ring should show a conservative in-flight hint,
    not replay a full large tool payload into `last_prompt_tokens`.
    """
    if not messages:
        return 0
    try:
        from agent.model_metadata import estimate_messages_tokens_rough
        delta = int(estimate_messages_tokens_rough(messages) or 0)
    except Exception:
        delta = 0
    if delta <= 0:
        return 0
    return min(delta, int(cap or 0))


def live_usage_prompt_estimate_after_tool_delta(
    *,
    base_prompt_tokens: int,
    exact_prompt_tokens: int = 0,
    messages=None,
    cap: int = _LIVE_TOOL_PROMPT_DELTA_MAX,
    turn_tool_prompt_tokens: int = 0,
    turn_cap: int = _LIVE_TOOL_PROMPT_TURN_MAX,
) -> dict:
    """Compute the live `last_prompt_tokens` estimate after a tool update.

    Exact compressor/provider prompt accounting wins. When no newer exact prompt
    is available, add only bounded live tool deltas to the persisted base.
    """
    base = int(base_prompt_tokens or 0)
    exact = int(exact_prompt_tokens or 0)
    if exact and exact != base:
        return {
            'last_prompt_tokens': exact,
            'estimated': False,
            'turn_tool_prompt_tokens': 0,
        }
    prior_turn_delta = max(0, int(turn_tool_prompt_tokens or 0))
    turn_ceiling = max(0, int(turn_cap or 0))
    next_turn_delta = min(
        prior_turn_delta + _bounded_live_tool_prompt_delta(messages, cap=cap),
        turn_ceiling,
    )
    return {
        'last_prompt_tokens': base + next_turn_delta,
        'estimated': True,
        'turn_tool_prompt_tokens': next_turn_delta,
    }


def _live_usage_session_snapshot(session_id, current_session, cache_ref, *, loader=get_session):
    """Return a session object for hot live-metering paths without repeated loads."""
    if current_session is not None:
        try:
            cache_ref[0] = current_session
        except Exception:
            pass
        return current_session
    try:
        cached = cache_ref[0]
    except Exception:
        cached = None
    if cached is not None:
        return cached
    try:
        loaded = loader(session_id)
    except Exception:
        return None
    try:
        cache_ref[0] = loaded
    except Exception:
        pass
    return loaded


def _tool_result_snippet(raw, limit: int = _TOOL_RESULT_SNIPPET_MAX) -> str:
    """Extract a bounded result preview from a stored tool message payload."""
    if limit <= 0:
        return ''
    text = str(raw or '')
    try:
        data = raw if isinstance(raw, dict) else json.loads(text)
        if isinstance(data, dict):
            preview = data.get('output') or data.get('result') or data.get('error') or text
            text = str(preview)
    except Exception:
        pass
    return text[:limit]


def _truncate_tool_args(args, limit: int = 6) -> dict:
    """Truncate tool args for compact session persistence.

    Incidental args keep a short 120-char cap, but content/diff-bearing keys
    (the command, code, and patch fields that tool cards and recovery-rebuilt
    diffs are reconstructed from) get a much larger cap so a long command, file
    path, or reconstructed diff is not silently corrupted (#4928). A hard cap is
    still applied for storage safety, aligned with the result snippet cap.
    """
    out = {}
    if not isinstance(args, dict):
        return out
    for k, v in list(args.items())[:limit]:
        s = str(v)
        cap = _TOOL_ARG_CONTENT_CAP if str(k).lower() in _TOOL_ARG_CONTENT_KEYS else 120
        out[k] = s[:cap] + ('...' if len(s) > cap else '')
    return out


def _nearest_assistant_msg_idx(messages, msg_idx: int) -> int:
    """Find the closest preceding assistant message index for a tool result."""
    for idx in range(msg_idx - 1, -1, -1):
        msg = messages[idx]
        if isinstance(msg, dict) and msg.get('role') == 'assistant':
            return idx
    return -1


def _extract_tool_calls_from_messages(messages, live_tool_calls=None):
    """Build persisted tool-call summaries from final messages plus live progress fallback."""
    tool_calls = []
    pending_names = {}
    pending_args = {}
    pending_asst_idx = {}
    tool_msg_sequence = []

    for msg_idx, m in enumerate(messages or []):
        if not isinstance(m, dict):
            continue
        role = m.get('role')
        if role == 'assistant':
            content = m.get('content', '')
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get('type') == 'tool_use':
                        tid = part.get('id', '')
                        if tid:
                            pending_names[tid] = part.get('name', '')
                            pending_args[tid] = part.get('input', {})
                            pending_asst_idx[tid] = msg_idx
            for tc in m.get('tool_calls', []):
                if not isinstance(tc, dict):
                    continue
                tid = tc.get('id', '') or tc.get('call_id', '')
                fn = tc.get('function', {})
                name = fn.get('name', '')
                try:
                    args = json.loads(fn.get('arguments', '{}') or '{}')
                except Exception:
                    args = {}
                if tid and name:
                    pending_names[tid] = name
                    pending_args[tid] = args
                    pending_asst_idx[tid] = msg_idx
        elif role == 'tool':
            tid = m.get('tool_call_id') or m.get('tool_use_id', '')
            raw = m.get('content', '')
            seq = {'msg_idx': msg_idx, 'raw': raw, 'resolved': False}
            if tid:
                name = pending_names.get(tid, '')
                if name and name != 'tool':
                    tool_calls.append({
                        'name': name,
                        'snippet': _tool_result_snippet(raw),
                        'tid': tid,
                        'assistant_msg_idx': pending_asst_idx.get(tid, -1),
                        'args': _truncate_tool_args(pending_args.get(tid, {})),
                    })
                    seq['resolved'] = True
            tool_msg_sequence.append(seq)

    live = [tc for tc in (live_tool_calls or []) if isinstance(tc, dict) and tc.get('name') and tc.get('name') != 'clarify']
    if live:
        for seq_idx, seq in enumerate(tool_msg_sequence):
            if seq.get('resolved'):
                continue
            if seq_idx >= len(live):
                break
            live_tc = live[seq_idx]
            tool_calls.append({
                'name': live_tc.get('name', 'tool'),
                'snippet': _tool_result_snippet(seq.get('raw', '')),
                'tid': live_tc.get('tid', '') or '',
                'assistant_msg_idx': _nearest_assistant_msg_idx(messages, seq.get('msg_idx', -1)),
                'args': _truncate_tool_args(live_tc.get('args', {}), limit=4),
            })

    return tool_calls


def _partial_message_signature(message: dict) -> tuple:
    """Return a stable identity for a persisted partial assistant marker."""
    if not isinstance(message, dict):
        return ('', '', ())
    tool_sig = []
    for tool_call in message.get('_partial_tool_calls') or []:
        if not isinstance(tool_call, dict):
            continue
        try:
            args_sig = json.dumps(
                tool_call.get('args') or {},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        except Exception:
            args_sig = str(tool_call.get('args') or '')
        tool_sig.append((
            str(tool_call.get('name') or ''),
            args_sig,
            bool(tool_call.get('done', False)),
            bool(tool_call.get('is_error', False)),
            str(tool_call.get('preview') or tool_call.get('snippet') or ''),
        ))
    return (
        str(message.get('content') or '').strip(),
        str(message.get('reasoning') or '').strip(),
        tuple(tool_sig),
    )


def _partial_marker_already_present(messages, candidate: dict, *, before_idx: int | None = None) -> bool:
    """Check for an equivalent partial marker in the current user turn only."""
    if not isinstance(messages, list) or not isinstance(candidate, dict):
        return False
    end = before_idx if isinstance(before_idx, int) else len(messages)
    end = max(0, min(end, len(messages)))
    start = 0
    for idx in range(end - 1, -1, -1):
        msg = messages[idx]
        if isinstance(msg, dict) and msg.get('role') == 'user':
            start = idx + 1
            break
    candidate_sig = _partial_message_signature(candidate)
    for msg in messages[start:end]:
        if isinstance(msg, dict) and msg.get('_partial') and _partial_message_signature(msg) == candidate_sig:
            return True
    return False


def _sse(handler, event, data):
    """Write one SSE event to the response stream."""
    payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    handler.wfile.write(payload.encode('utf-8'))
    handler.wfile.flush()


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
try:
    _raw_deadline = os.getenv("HERMES_WEBUI_SSE_WRITE_DEADLINE") or os.getenv("HERMES_SSE_WRITE_DEADLINE")
    SSE_WRITE_DEADLINE_SECONDS = float(_raw_deadline or "20.0")
except (TypeError, ValueError):
    SSE_WRITE_DEADLINE_SECONDS = 20.0
if SSE_WRITE_DEADLINE_SECONDS <= 0:
    SSE_WRITE_DEADLINE_SECONDS = 20.0


def _sse_set_write_deadline(handler, seconds=None):
    """Best-effort: arm a socket write deadline on an SSE handler.

    Call once, right after end_headers(), in every long-lived SSE endpoint.
    Never raises — an unusual/missing transport just keeps the pre-fix
    (no-deadline) behaviour for that single connection rather than breaking
    the stream setup.
    """
    if seconds is None:
        seconds = SSE_WRITE_DEADLINE_SECONDS
    try:
        conn = getattr(handler, "connection", None)
        if conn is not None and hasattr(conn, "settimeout"):
            conn.settimeout(seconds)
    except Exception:
        logger.debug("Failed to arm SSE write deadline", exc_info=True)


def _materialize_pending_user_turn_before_error(session) -> bool:
    """Persist the pending user prompt before clearing runtime stream state.

    Error paths often clear ``pending_user_message`` before appending an assistant
    error marker. In deferred session-save mode that pending field can be the
    only durable copy of the user's current turn, so clearing it makes the user
    bubble disappear on reload/reconcile. Return True when a recovered user turn
    was appended.
    """
    pending_text = str(getattr(session, 'pending_user_message', None) or '')
    if not pending_text:
        return False
    recovered_ts = int(time.time())
    pending_started_at = getattr(session, 'pending_started_at', None)
    if isinstance(pending_started_at, (int, float)) and pending_started_at > 0:
        recovered_ts = int(pending_started_at)
    pending_source = getattr(session, 'pending_user_source', None) or 'webui'
    pending_attachments = list(getattr(session, 'pending_attachments', None) or [])

    def is_exact_checkpoint(messages):
        if not isinstance(messages, list) or not messages:
            return False
        existing = messages[-1]
        if not isinstance(existing, dict) or existing.get('role') != 'user':
            return False
        existing_source = existing.get('_source') or 'webui'
        try:
            existing_ts = int(existing.get('timestamp'))
        except (TypeError, ValueError):
            return False
        return (
            _normalize_user_text(existing.get('content')) == _normalize_user_text(pending_text)
            and existing_ts == recovered_ts
            and existing_source == pending_source
            and list(existing.get('attachments') or []) == pending_attachments
        )

    if is_exact_checkpoint(getattr(session, 'messages', None)):
        return False
    recovered = {
        'role': 'user',
        'content': pending_text,
        'timestamp': recovered_ts,
        '_recovered': True,
    }
    if pending_source != 'webui':
        recovered['_source'] = pending_source
    if pending_attachments:
        recovered['attachments'] = pending_attachments
    session.messages.append(recovered)
    # Mirror to context_messages so the _recovered flag survives the state.db
    # round-trip (#4283).  state.db has no _recovered column, so without this
    # mirror the next turn's reconciled_state_db_messages_for_session(
    # prefer_context=True) finds the recovered user as a flagless state.db
    # delta and _sanitize_messages_for_api cannot filter it — causing the
    # interrupted turn's prompt to be prepended to every subsequent turn.
    # Placing the mirror here (rather than in _persist_cancelled_turn) covers
    # all three callers: cancel, provider-error, and exception paths.
    ctx = getattr(session, 'context_messages', None)
    if isinstance(ctx, list) and ctx and not is_exact_checkpoint(ctx):
        ctx.append(dict(recovered))
    # The new user turn is now committed to messages (#3831): advance a positive
    # truncation watermark left over from a prior retry/undo/edit so that
    # merge_session_messages_append_only() still filters out replaced pre-edit
    # rows from state.db. The merge's sidecar_advanced_past_watermark guard
    # allows state.db rows newer than the watermark, so post-edit turns are not
    # dropped. Never 0.0 (the truncate-to-empty sentinel, #2914).
    if getattr(session, 'truncation_watermark', None):
        session.truncation_watermark = float(recovered_ts)
    return True


def _build_partial_message(content_text, reasoning_text, tool_calls) -> dict | None:
    """Build a _partial assistant message from raw streaming buffers.

    Shared by cancel_stream() and _snapshot_and_append_partial_on_error().
    Strips thinking/reasoning markup, builds the dict, returns None when
    there is nothing meaningful to preserve.
    """
    import re as _re
    partial_text = (content_text or '').strip()
    _stripped = ''
    if partial_text:
        # First pass: remove complete <thinking>...</thinking> blocks.
        _stripped = _re.sub(r'<think(?:ing)?\b[^>]*>.*?</think(?:ing)?>',
                            '', partial_text,
                            flags=_re.DOTALL | _re.IGNORECASE).strip()
        # Second pass: strip trailing UNCLOSED think/thinking block (the common
        # cancel/error case — user stops mid-reasoning before the close tag appears).
        _stripped = _re.sub(r'<think(?:ing)?\b[^>]*>.*',
                            '', _stripped,
                            flags=_re.DOTALL | _re.IGNORECASE).strip()
    _has_reasoning = bool(reasoning_text and reasoning_text.strip())
    _has_tools = bool(tool_calls)
    if not (_stripped or _has_reasoning or _has_tools):
        return None
    _msg: dict = {
        'role': 'assistant',
        'content': _stripped,  # may be empty for reasoning/tool-only turns
        '_partial': True,
        'timestamp': int(time.time()),
    }
    if _has_reasoning:
        _msg['reasoning'] = reasoning_text.strip()
    if _has_tools:
        _msg['_partial_tool_calls'] = list(tool_calls)
    return _msg


def _snapshot_and_append_partial_on_error(session, stream_id) -> dict | None:
    """Snapshot runtime-owned progress and append a _partial message.

    Uses _build_partial_message() for the shared thinking-strip + dict-build logic.
    """
    from api import config as _live_config

    progress = _live_config.runtime_progress_snapshot(stream_id)

    _partial_msg = _build_partial_message(
        progress.partial_text,
        progress.reasoning_text,
        progress.live_tool_calls,
    )
    if _partial_msg is None:
        return None
    if not isinstance(session.messages, list):
        session.messages = []
    if not _partial_marker_already_present(session.messages, _partial_msg):
        session.messages.append(_partial_msg)
        return _partial_msg
    return None


def _last_resort_sync_from_core(session, stream_id, agent_lock):
    """Final-exit guard: if the stream exits with pending_user_message still set,
    sync messages from the core transcript or add an error marker.
    Called from the outer finally block of _run_agent_streaming.
    Must never raise.
    """
    from api.models import _get_profile_home, _apply_core_sync_or_error_marker
    try:
        # Guard: if a cancel was already requested, bail out — cancel_stream() has
        # already saved partial content and we must not double-append error markers.
        if stream_id in CANCEL_FLAGS and CANCEL_FLAGS[stream_id].is_set():
            return

        profile_home = _get_profile_home(session.profile)
        core_path = profile_home / 'sessions' / f'session_{session.session_id}.json'

        _lock_ctx = agent_lock if agent_lock is not None else contextlib.nullcontext()
        with _lock_ctx:
            _apply_core_sync_or_error_marker(
                session,
                core_path,
                stream_id_for_recheck=stream_id,
                require_stream_dead=False,
            )
    except Exception:
        logger.exception(
            "_last_resort_sync_from_core failed for session %s",
            getattr(session, 'session_id', '?'),
        )


def _session_db_is_open(session_db) -> bool:
    """True when *session_db* still has a live sqlite connection.

    SessionDB.close() sets ``_conn = None``. Subagents capture the parent's
    SessionDB object by reference at spawn time (delegate_tool), so closing
    that object mid-parent-turn makes every subsequent child
    ``append_message`` fail with
    ``'NoneType' object has no attribute 'execute'``.
    """
    if session_db is None:
        return False
    return getattr(session_db, "_conn", None) is not None


def _adopt_session_db_for_cached_agent(agent, new_session_db):
    """Attach a SessionDB to a reused cached agent without breaking subagents.

    Historical behaviour (PR #1421 FD-leak fix): create a fresh SessionDB every
    stream request and close the previous handle before replacing
    ``agent._session_db``. That stops EMFILE growth, but a server-side wakeup
    / new turn for the same parent session will close the shared handle while
    background subagents are still writing into it.

    Policy now:
    - If the cached agent already holds an *open* SessionDB, keep it and close
      the unused new handle (no FD leak; live subagents keep working).
    - If the existing handle is missing or already closed, adopt *new_session_db*.
    - If *new_session_db* is None, leave the existing handle alone.
    """
    if agent is None:
        return new_session_db
    existing = getattr(agent, "_session_db", None)
    if new_session_db is None:
        return existing
    if existing is new_session_db:
        return existing
    if _session_db_is_open(existing):
        try:
            new_session_db.close()
        except Exception:
            # Same observability as _replace_session_db_in_kwargs: a failed
            # close here reintroduces the EMFILE pressure PR #1421 fixed.
            logger.debug(
                "Failed to close unused session_db handle in adopt helper",
                exc_info=True,
            )
        return existing
    if existing is not None:
        try:
            existing.close()
        except Exception:
            logger.debug(
                "Failed to close previous session_db handle in adopt helper",
                exc_info=True,
            )
    agent._session_db = new_session_db
    return new_session_db


def _build_session_db_for_stream(state_db_path):
    """Build a per-request SessionDB handle for WebUI session search.

    Returns ``None`` if the helper module or constructor fails so callers can
    continue without session_search rather than propagating a hard failure.
    """
    try:
        from hermes_state import SessionDB
        _attempts = 3
        _last_error = None
        for _attempt in range(_attempts):
            try:
                return SessionDB(db_path=state_db_path)
            except sqlite3.OperationalError as _db_err:
                _db_err_text = str(_db_err).lower()
                if not (
                    "locked" in _db_err_text or "busy" in _db_err_text
                ):
                    raise
                _last_error = _db_err
                if _attempt < _attempts - 1:
                    print(
                        f"[webui] WARNING: SessionDB init attempt {_attempt + 1}/{_attempts} failed, retrying: {_db_err}",
                        flush=True,
                    )
                    time.sleep(0.05 * (2 ** _attempt) + random.uniform(0, 0.05))
        raise _last_error or RuntimeError("SessionDB construction exhausted all attempts")
    except Exception as _db_err:
        print(f"[webui] WARNING: SessionDB init failed - session_search will be unavailable: {_db_err}", flush=True)
        return None


def _replace_session_db_in_kwargs(agent_kwargs, state_db_path):
    """Build a fresh SessionDB and replace ``agent_kwargs['session_db']`` safely.

    Does not close an existing open handle that may still be shared with live
    subagents; only replaces when the prior handle is missing or already closed.
    """
    if not isinstance(agent_kwargs, dict):
        return None

    _old_session_db = agent_kwargs.get("session_db")
    _next_session_db = _build_session_db_for_stream(state_db_path)
    if _next_session_db is None:
        # Replacement construction failed. Keep the prior handle only if it is
        # still open (live subagents may hold it by reference); otherwise
        # degrade cleanly to None — as master did — so the rebuilt agent lazily
        # reinitialises its SessionDB instead of reusing a closed handle and
        # failing every persist/search with
        # "'NoneType' object has no attribute 'execute'".
        if _session_db_is_open(_old_session_db):
            return _old_session_db
        agent_kwargs["session_db"] = None
        return None
    if _session_db_is_open(_old_session_db):
        # Keep the live handle; discard the unused new one.
        try:
            if _next_session_db is not _old_session_db:
                _next_session_db.close()
        except Exception:
            logger.debug("Failed to close unused session_db handle during self-heal")
        agent_kwargs["session_db"] = _old_session_db
        return _old_session_db
    if _old_session_db is not None and _old_session_db is not _next_session_db:
        try:
            _old_session_db.close()
        except Exception:
            logger.debug("Failed to close previous session_db handle during self-heal")
    agent_kwargs["session_db"] = _next_session_db
    return _next_session_db


def _attempt_credential_self_heal(
    provider_id, session_id, _agent_lock_ref, *, target_model=None,
):
    """Try to silently refresh credentials after a 401/auth error (#1401).

    Returns a new ``(agent, rt_dict)`` tuple on success so the caller can
    retry the conversation.  Returns ``None`` when self-heal is not
    applicable (e.g. auth.json unchanged, provider unresolvable).

    Steps:
    1. Re-read ``~/.hermes/auth.json`` to pick up fresh credentials that
       may have been written by a concurrent ``hermes model`` CLI invocation.
    2. Evict the session's cached agent so it is rebuilt with fresh keys.
    3. Evict the provider's credential-pool cache entry.
    4. Re-resolve the runtime provider.
    5. Return a new agent + resolved-provider dict (the caller must
       re-invoke ``run_conversation`` with these).
    """
    try:
        from api.oauth import (
            read_auth_json,
            resolve_runtime_provider_with_anthropic_env_lock,
        )
        from api.config import (
            SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK,
            invalidate_credential_pool_cache,
        )
        from hermes_cli.runtime_provider import resolve_runtime_provider

        # 1. Re-read auth.json (triggers a fresh credential scan)
        _fresh_auth = read_auth_json()
        if not _fresh_auth:
            logger.debug('[webui] self-heal: auth.json empty or missing, skipping')
            return None

        # 2. Evict the cached agent for this session
        _evicted_entry = None
        with SESSION_AGENT_CACHE_LOCK:
            _evicted_entry = SESSION_AGENT_CACHE.pop(session_id, None)
        if _evicted_entry is not None:
            _close_cached_agent_entry_at_session_boundary(session_id, _evicted_entry)

        # 3. Invalidate the credential pool for this provider
        invalidate_credential_pool_cache(provider_id)

        # 4. Re-resolve runtime provider with fresh credentials
        _new_rt = resolve_runtime_provider_with_anthropic_env_lock(
            resolve_runtime_provider,
            requested=provider_id,
            target_model=target_model,
        )

        logger.info(
            '[webui] self-heal: credential refresh succeeded for provider=%s session=%s',
            provider_id, session_id,
        )
        return _new_rt
    except Exception as _heal_err:
        logger.warning(
            '[webui] self-heal: failed for provider=%s session=%s: %s',
            provider_id, session_id, _heal_err,
        )
        return None


def _agent_cache_api_key_sig(resolved_api_key, credential_pool) -> str:
    """Return the cache-signature component for runtime credentials.

    Credential-pool providers can legitimately hand WebUI a different runtime
    token on each request (round-robin pools, OAuth refresh, auth self-heal).
    The AIAgent object is also where cross-turn memory-provider state lives, so
    using the volatile token itself in the cache signature silently defeats the
    per-session agent cache and drops warmed Hindsight prefetch results.
    """
    if credential_pool is not None:
        return 'credential-pool'
    import hashlib as _hashlib
    return _hashlib.sha256((resolved_api_key or '').encode()).hexdigest()[:16]


def _lifecycle_commit_session_memory(session_id: str, *, agent=None, wait: bool = False) -> bool:
    from api.session_lifecycle import commit_session_memory

    return commit_session_memory(session_id, agent=agent, wait=wait)


def _lifecycle_has_uncommitted_work(session_id: str) -> bool:
    from api.session_lifecycle import has_uncommitted_work

    return has_uncommitted_work(session_id)


def _lifecycle_unregister_agent(session_id: str) -> None:
    from api.session_lifecycle import unregister_agent

    unregister_agent(session_id)


def _lifecycle_discard_session(session_id: str) -> bool:
    from api.session_lifecycle import discard_session

    return discard_session(session_id)


def _close_evicted_agent_at_session_boundary(session_id: str, agent) -> bool:
    """Commit and tear down an evicted cached agent at a WebUI session boundary.

    WebUI keeps AIAgent instances in an LRU cache so memory providers can carry
    state across turns. When an agent is evicted, commit pending memory first;
    if the lifecycle entry is clean afterwards, unregister and call
    shutdown_memory_provider(messages) so provider-owned clients such as
    Hindsight's aiohttp session are closed instead of being garbage-collected
    later. Passing the cached transcript mirrors gateway cleanup semantics for
    providers that use on_session_end(messages) during shutdown.
    """
    if agent is None:
        return True

    should_close_evicted_agent = True
    try:
        _lifecycle_commit_session_memory(session_id, agent=agent, wait=True)
        if not _lifecycle_has_uncommitted_work(session_id):
            _lifecycle_unregister_agent(session_id)
            # Drop the lifecycle dict entry now that the LRU-evicted agent is
            # gone and no uncommitted work remains, so the dict tracks only live
            # sessions instead of growing unbounded (issue #3506).
            _lifecycle_discard_session(session_id)
        else:
            should_close_evicted_agent = False
    except Exception:
        should_close_evicted_agent = False
        logger.debug("Lifecycle commit on eviction failed for %s", session_id, exc_info=True)

    if not should_close_evicted_agent:
        return False

    try:
        shutdown_memory_provider = getattr(agent, 'shutdown_memory_provider', None)
        if callable(shutdown_memory_provider):
            session_messages = vars(agent).get('_session_messages', [])
            shutdown_memory_provider(session_messages)
    except Exception:
        logger.debug("Failed to shut down evicted agent memory provider for session %s", session_id, exc_info=True)

    try:
        session_db = getattr(agent, '_session_db', None)
        if session_db is not None:
            session_db.close()
    except Exception:
        logger.debug("Failed to close evicted agent session DB for session %s", session_id, exc_info=True)
    return True


def _close_cached_agent_entry_at_session_boundary(session_id: str, cache_entry) -> bool:
    """Commit and tear down a popped SESSION_AGENT_CACHE entry outside the cache lock."""
    agent = cache_entry[0] if isinstance(cache_entry, tuple) else None
    return _close_evicted_agent_at_session_boundary(session_id, agent)


def _refresh_cached_agent_runtime(agent, agent_kwargs: dict) -> bool:
    """Refresh volatile runtime credentials on a reused cached AIAgent.

    The cache key intentionally ignores credential-pool token churn, but the
    cached agent's LLM client still needs the latest selected/refreshed runtime
    key. Keep long-lived provider/session state (memory prefetch, turn counters,
    tool state) while swapping only the runtime credential/client.
    """
    if agent is None or not isinstance(agent_kwargs, dict):
        return False

    new_pool = agent_kwargs.get('credential_pool')
    if new_pool is not None:
        try:
            agent._credential_pool = new_pool
        except Exception:
            pass

    new_key = agent_kwargs.get('api_key') or ''
    if not new_key:
        return True

    new_base = agent_kwargs.get('base_url') or getattr(agent, 'base_url', '') or ''
    if getattr(agent, '_fallback_activated', False):
        # Avoid mixing a refreshed primary credential into a live fallback
        # runtime. Rebuilding is safer than mutating a fallback-active agent
        # whose restore/cooldown state has not run yet for this turn.
        return False

    if new_key == (getattr(agent, 'api_key', '') or ''):
        _refresh_cached_agent_primary_runtime_snapshot(agent)
        return True

    try:
        if getattr(agent, 'api_mode', None) == 'anthropic_messages':
            # Native Anthropic-style clients have their own construction path;
            # switch_model() already handles token/client refresh there.
            if hasattr(agent, 'switch_model'):
                agent.switch_model(
                    agent_kwargs.get('model') or getattr(agent, 'model', None),
                    agent_kwargs.get('provider') or getattr(agent, 'provider', None),
                    api_key=new_key,
                    base_url=new_base,
                    api_mode=agent_kwargs.get('api_mode') or getattr(agent, 'api_mode', ''),
                )
                return True
            return False

        if not hasattr(agent, '_client_kwargs') or not hasattr(agent, '_replace_primary_openai_client'):
            # Test/fake-agent fallback: keep metadata accurate even if no real
            # OpenAI client exists to rebuild.
            agent.api_key = new_key
            if new_base:
                agent.base_url = new_base
            _refresh_cached_agent_primary_runtime_snapshot(agent)
            return True

        client_kwargs = dict(getattr(agent, '_client_kwargs', {}) or {})
        client_kwargs['api_key'] = new_key
        if new_base:
            client_kwargs['base_url'] = new_base
        agent._client_kwargs = client_kwargs
        agent.api_key = new_key
        if new_base:
            agent.base_url = new_base
        if hasattr(agent, '_apply_client_headers_for_base_url'):
            agent._apply_client_headers_for_base_url(agent.base_url)
        rebuilt = bool(agent._replace_primary_openai_client(reason='webui_credential_refresh'))
        if rebuilt:
            _refresh_cached_agent_primary_runtime_snapshot(agent)
        return rebuilt
    except Exception:
        logger.debug('[webui] Failed to refresh cached agent runtime credentials', exc_info=True)
        return False


def _cached_agent_session_identity(agent) -> str | None:
    """Best-effort session id carried by a cached AIAgent.

    The cache key is only safe when it agrees with the object's own session
    identity. Some old/fake agents may not expose an identity; keep those
    backwards-compatible and treat them as unverifiable rather than mismatched.
    """
    if agent is None:
        return None
    for attr in ('session_id', '_session_id'):
        value = getattr(agent, attr, None)
        if isinstance(value, str) and value:
            return value
    session_db = getattr(agent, '_session_db', None)
    if session_db is not None:
        for attr in ('session_id', '_session_id'):
            value = getattr(session_db, attr, None)
            if isinstance(value, str) and value:
                return value
    return None


def _cached_agent_matches_session(agent, session_id: str) -> bool:
    identity = _cached_agent_session_identity(agent)
    return identity is None or identity == str(session_id)


def _refresh_cached_agent_primary_runtime_snapshot(agent) -> None:
    """Keep AIAgent's primary-runtime snapshot aligned with refreshed creds.

    Long-lived AIAgent instances use `_primary_runtime` to restore the preferred
    provider after fallback/transport recovery. If WebUI refreshes a cached
    agent's runtime token but leaves that snapshot stale, a later restore can
    resurrect the old credential and undo the refresh.
    """
    rt = getattr(agent, '_primary_runtime', None)
    if not isinstance(rt, dict):
        return

    base_url = getattr(agent, 'base_url', rt.get('base_url'))
    api_key = getattr(agent, 'api_key', rt.get('api_key', ''))
    client_kwargs = dict(getattr(agent, '_client_kwargs', None) or rt.get('client_kwargs', {}) or {})

    rt['base_url'] = base_url
    rt['api_key'] = api_key
    rt['client_kwargs'] = client_kwargs

    # The default context compressor usually tracks the primary runtime too;
    # keep both the live compressor fields and the fallback-restoration
    # snapshot aligned when those attributes exist.
    cc = getattr(agent, 'context_compressor', None)
    if cc is not None:
        if hasattr(cc, 'base_url'):
            cc.base_url = base_url
        if hasattr(cc, 'api_key'):
            cc.api_key = api_key
        if 'compressor_base_url' in rt:
            rt['compressor_base_url'] = getattr(cc, 'base_url', base_url)
        if 'compressor_api_key' in rt:
            rt['compressor_api_key'] = getattr(cc, 'api_key', api_key)
    else:
        if 'compressor_base_url' in rt:
            rt['compressor_base_url'] = base_url
        if 'compressor_api_key' in rt:
            rt['compressor_api_key'] = api_key

    if getattr(agent, 'api_mode', None) == 'anthropic_messages':
        if hasattr(agent, '_anthropic_api_key'):
            rt['anthropic_api_key'] = getattr(agent, '_anthropic_api_key')
        if hasattr(agent, '_anthropic_base_url'):
            rt['anthropic_base_url'] = getattr(agent, '_anthropic_base_url')
        if hasattr(agent, '_is_anthropic_oauth'):
            rt['is_anthropic_oauth'] = getattr(agent, '_is_anthropic_oauth')


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
    """Run one complete local-agent lifecycle behind the compatibility facade."""
    return _streaming_local_run.run_agent_streaming(
        _streaming_api(),
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
    )

# ============================================================
# SECTION: HTTP Request Handler
# do_GET: read-only API endpoints + SSE stream + static HTML
# do_POST: mutating endpoints (session CRUD, chat, upload, approval)
# Routing is a flat if/elif chain. See ARCHITECTURE.md section 4.1.
# ============================================================


def _handle_chat_steer(handler, body: dict) -> bool:
    """Inject active-run guidance without interrupting the owning stream."""
    return _streaming_live_controls.handle_chat_steer(
        _streaming_api(),
        handler,
        body,
    )


def cancel_stream(stream_id: str) -> bool:
    """Cancel a run while preserving recoverable progress and terminal state."""
    return _streaming_live_controls.cancel_stream(_streaming_api(), stream_id)
