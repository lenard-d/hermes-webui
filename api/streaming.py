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
import shlex
import subprocess
import threading
import time
import traceback
import copy
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

from api.config import (
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
from api.compression_anchor import is_context_compression_marker, visible_messages_for_anchor
from api.compression_recovery import stamp_compression_exhausted_recovery
from api.metering import meter
from api.todo_state import attach_todo_state, emit_todo_state  # noqa: F401
from api.turn_journal import append_turn_journal_event_for_stream
from api.turn_execution import TurnExecution
from api.usage import prompt_cache_hit_percent
from api.models import (
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
from api.session_repository import edit_session
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
from api.streaming_parts import context_replay as _streaming_context_replay
from api.streaming_parts import message_sanitization as _streaming_message_sanitization
from api.streaming_parts import post_compression_context as _streaming_post_compression
from api.streaming_parts import provider_errors as _streaming_provider_errors
from api.streaming_parts import stale_user_context as _streaming_stale_user_context
from api.streaming_parts import thinking_content as _streaming_thinking
from api.streaming_parts import terminal_outcomes as _streaming_terminal_outcomes
from api.streaming_parts import title_generation as _streaming_titles
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
    try:
        st = path.stat()
        return (int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return None


def _persistent_state_snapshot(profile_home: str | None) -> dict:
    """Capture lightweight memory/skill file signatures for save toasts."""
    if not profile_home:
        return {"memory": {}, "skills": {}}
    root = Path(profile_home)
    memory = {}
    for key, parts in _PERSISTENT_MEMORY_FILES:
        sig = _file_signature(root.joinpath(*parts))
        if sig is not None:
            memory[key] = sig
    skills = {}
    skills_dir = root / "skills"
    try:
        for skill_md in skills_dir.rglob("SKILL.md"):
            try:
                rel = str(skill_md.relative_to(skills_dir)).replace("\\", "/")
            except ValueError:
                rel = str(skill_md)
            sig = _file_signature(skill_md)
            if sig is not None:
                skills[rel] = sig
    except OSError:
        pass
    return {"memory": memory, "skills": skills}


def _persistent_state_changes(before: dict | None, after: dict | None) -> dict:
    before = before or {"memory": {}, "skills": {}}
    after = after or {"memory": {}, "skills": {}}
    memory_before = before.get("memory") or {}
    memory_after = after.get("memory") or {}
    skills_before = before.get("skills") or {}
    skills_after = after.get("skills") or {}
    memory_changed = any(memory_before.get(key) != sig for key, sig in memory_after.items())
    skills = []
    for rel, sig in skills_after.items():
        old_sig = skills_before.get(rel)
        if old_sig == sig:
            continue
        name = Path(rel).parent.name or Path(rel).stem
        skills.append({
            "name": name,
            "path": rel,
            "action": "created" if old_sig is None else "updated",
        })
    return {"memory_saved": memory_changed, "skills": skills[:10]}


def _apply_profile_provider_context_to_streaming_model(
    model: str | None,
    provider_context: str | None,
    profile_provider: str | None,
    profile_default_model: str | None,
) -> tuple[str | None, str | None, bool]:
    """Attach profile provider context and repair stale cross-provider models."""
    if provider_context or not profile_provider:
        return model, provider_context, False

    provider_context = profile_provider.lower()
    if not profile_default_model:
        return model, provider_context, False

    from api.routes import _normalize_provider_id

    profile_provider_normalized = _normalize_provider_id(profile_provider)
    model_lower = (model or "").lower()
    # Only run the bare-prefix family match on un-namespaced model ids. A custom
    # namespace like "gemini_cli/..." or "claude-relay/..." merely *starts with* a
    # first-party token; matching it here would clobber the model to the profile
    # default on the send path (the #4278 collision — the slash-qualified branch
    # below routes through the fixed _normalize_provider_id instead).
    if "/" not in model_lower:
        for prefix in ("gpt", "claude", "gemini"):
            if model_lower.startswith(prefix):
                if _normalize_provider_id(prefix) != profile_provider_normalized:
                    return profile_default_model, provider_context, True
                return model, provider_context, False

    if "/" in model_lower:
        slash_prefix = model_lower.split("/", 1)[0]
        if provider_context == "openai-codex" and slash_prefix == "openai":
            return profile_default_model, provider_context, True

        slash_provider = _normalize_provider_id(slash_prefix)
        if (
            slash_provider
            and slash_provider != profile_provider_normalized
            and profile_provider_normalized not in {"openrouter", "custom", ""}
        ):
            return profile_default_model, provider_context, True

    return model, provider_context, False


def _apply_profile_home_context_to_streaming_model(
    model: str | None,
    provider_context: str | None,
    profile_home: str | None,
    has_profile: bool,
) -> tuple[str | None, str | None, bool]:
    """Apply profile provider/model context from a profile config if present."""
    if not (profile_home and has_profile and not provider_context):
        return model, provider_context, False

    try:
        import yaml as _yaml_pp

        _pp_cfg_path = Path(profile_home) / "config.yaml"
        if not _pp_cfg_path.is_file():
            return model, provider_context, False

        _pp_cfg = _yaml_pp.safe_load(_pp_cfg_path.read_text(encoding="utf-8")) or {}
        if not isinstance(_pp_cfg, dict):
            return model, provider_context, False

        _pp = (_pp_cfg.get("model", {}).get("provider") or "").strip()
        if not _pp:
            return model, provider_context, False

        _pp_default = (_pp_cfg.get("model", {}).get("default") or "").strip()
        return _apply_profile_provider_context_to_streaming_model(
            model,
            provider_context,
            _pp,
            _pp_default,
        )
    except Exception:
        logger.warning("profile provider read failed", exc_info=True)
        return model, provider_context, False


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
    if not (isinstance(resolved_provider, str) and resolved_provider.startswith("custom:")):
        return resolved_provider, resolved_api_key, resolved_base_url

    _cp_key, _cp_base = resolve_custom_provider_connection(resolved_provider)
    if not resolved_api_key and _cp_key:
        resolved_api_key = _cp_key
    if not resolved_base_url and _cp_base:
        resolved_base_url = _cp_base
    if resolved_base_url:
        # Route through the generic custom OpenAI-compatible client once the
        # named provider has supplied the concrete endpoint. Keeping the
        # provider as custom:<slug> would make Agent init synthesize invalid
        # env-var hints like CUSTOM:SOMETHING-8000_API_KEY on keyless setups.
        resolved_provider = "custom"
        if not resolved_api_key:
            resolved_api_key = _KEYLESS_CUSTOM_API_KEY
    return resolved_provider, resolved_api_key, resolved_base_url


def _same_base_url_endpoint(url_a: str, url_b: str) -> bool:
    """True if two base URLs point at the same scheme+host+port endpoint.

    Used to decide whether a runtime base_url is just a normalized form of the
    configured one (e.g. OpenCode-Go's ``/v1`` de-duplication on the same host)
    versus a genuinely different endpoint (an explicit ``providers.<id>.base_url``
    override at a different host/port that must be preserved). Path/query are
    intentionally ignored — the normalization #3895 fixes is path-only.
    """
    from urllib.parse import urlsplit
    try:
        a = urlsplit((url_a or "").strip())
        b = urlsplit((url_b or "").strip())
    except Exception:
        return False
    _default_port = {"http": 80, "https": 443}
    a_host = (a.hostname or "").lower()
    b_host = (b.hostname or "").lower()
    a_scheme = (a.scheme or "").lower()
    b_scheme = (b.scheme or "").lower()
    a_port = a.port or _default_port.get(a_scheme)
    b_port = b.port or _default_port.get(b_scheme)
    return bool(a_host) and a_host == b_host and a_scheme == b_scheme and a_port == b_port


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
    runtime_base_url = None
    if isinstance(runtime_provider, dict):
        runtime_base_url = runtime_provider.get("base_url")
    if not runtime_base_url:
        return configured_base_url
    if not configured_base_url:
        return runtime_base_url

    provider_id = str(
        resolved_provider
        or (runtime_provider or {}).get("provider")
        or ""
    ).strip().lower()
    if provider_id.startswith("custom:"):
        return configured_base_url or runtime_base_url

    # An explicit configured override at a DIFFERENT endpoint must be preserved;
    # only prefer the runtime URL when it's the same endpoint (path-normalized).
    if _same_base_url_endpoint(configured_base_url, runtime_base_url):
        return runtime_base_url
    return configured_base_url


def _is_fallback_lifecycle_message(kind: str, message: str) -> bool:
    """Return True if an agent lifecycle status should surface as a fallback warning."""
    k = str(kind or '').strip().lower()
    m = str(message or '').strip().lower()
    return (
        k == 'lifecycle'
        and (
            'rate limited' in m
            or 'switching to fallback' in m
            or 'falling back' in m
            or 'fallback activated' in m
            or 'trying fallback' in m
        )
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
    k = str(kind or '').strip().lower()
    m = str(message or '').strip().lower()
    if k != 'lifecycle' or not m:
        return False
    # Skip / cooldown / defer logs must never look like a live compression start.
    if (
        'skipping' in m
        or 'defer' in m
        or 'cooldown' in m
        or 'will not start' in m
    ):
        return False
    # Post-compress retry chatter is not a start event.
    if 'compressed' in m and 'compressing' not in m and 'compression attempt' not in m:
        return False
    return (
        'preflight compression:' in m
        or 'pre-api compression:' in m
        or 'compacting context' in m
        or 'context too large' in m
        or '— compressing (' in m
        or '- compressing (' in m
        or 'compression attempt' in m
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


_SECRET_SHAPED_RE = re.compile(
    r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s]+|"
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b|"
    r"[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"
)

def _redact_prefill_status_text(text: str) -> str:
    """Return a short, non-secret diagnostic string for prefill status."""
    clean = _SECRET_SHAPED_RE.sub("[REDACTED]", str(text or ""))
    return " ".join(clean.split())[:240]


def _valid_prefill_messages(value) -> list[dict]:
    """Normalize a prefill payload to role/content messages."""
    if not isinstance(value, list):
        return []
    messages: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in {"system", "user", "assistant"} or not isinstance(content, str) or not content.strip():
            continue
        messages.append({"role": role, "content": content})
    return messages


def _resolve_prefill_path(raw: str) -> Path:
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        try:
            from api.config import _get_config_path
            path = _get_config_path().parent / path
        except Exception:
            path = Path.cwd() / path
    return path


_PREFILL_SCRIPT_OUTPUT_LIMIT = 262_144
_PREFILL_CONTEXT_DEFAULT_MAX_CHARS = 12_000


def _prefill_context_max_chars(config_data: dict) -> int:
    raw = os.getenv("HERMES_WEBUI_PREFILL_CONTEXT_MAX_CHARS", "") or str(
        config_data.get("webui_prefill_context_max_chars") or ""
    )
    try:
        value = int(raw or _PREFILL_CONTEXT_DEFAULT_MAX_CHARS)
    except Exception:
        value = _PREFILL_CONTEXT_DEFAULT_MAX_CHARS
    return max(0, min(value, _PREFILL_SCRIPT_OUTPUT_LIMIT))


def _prefill_context_char_count(messages: list[dict]) -> int:
    return sum(len(str(message.get("content") or "")) for message in messages if isinstance(message, dict))


def _budget_compacted_prefill_context(context: dict, *, max_chars: int, char_count: int) -> dict:
    label = str(context.get("label") or "prefill context")
    message = (
        "A configured WebUI startup prefill source was available, but it exceeded "
        f"the WebUI prefill context budget ({char_count} chars > {max_chars} chars), "
        "so the note/body payload was omitted from this new chat. If the user's "
        "request depends on prior decisions, durable notes, runbooks, current "
        "context, or open issues, use the available retrieval/search/note tools "
        "to fetch only the relevant details before answering."
    )
    return {
        "status": "loaded",
        "source": "budget_compacted",
        "label": label,
        "messages": [{"role": "user", "content": message}],
        "message_count": 1,
        "compacted": True,
        "original_source": context.get("source", ""),
        "original_message_count": int(context.get("message_count") or 0),
        "original_char_count": char_count,
        "max_chars": max_chars,
    }


def _apply_prefill_context_budget(context: dict, config_data: dict) -> dict:
    if context.get("status") != "loaded":
        return context
    max_chars = _prefill_context_max_chars(config_data)
    if max_chars <= 0:
        return context
    messages = context.get("messages") or []
    char_count = _prefill_context_char_count(messages if isinstance(messages, list) else [])
    if char_count <= max_chars:
        return context

    file_raw = os.getenv("HERMES_PREFILL_MESSAGES_FILE", "") or str(config_data.get("prefill_messages_file") or "")
    if context.get("source") == "script" and file_raw:
        fallback = _load_prefill_messages_file(file_raw, source="file_budget_fallback")
        fallback_messages = fallback.get("messages") if isinstance(fallback, dict) else []
        fallback_chars = _prefill_context_char_count(fallback_messages if isinstance(fallback_messages, list) else [])
        if fallback.get("status") == "loaded" and fallback_chars <= max_chars:
            fallback["compacted"] = True
            fallback["original_source"] = context.get("source", "")
            fallback["original_label"] = context.get("label", "")
            fallback["original_message_count"] = int(context.get("message_count") or 0)
            fallback["original_char_count"] = char_count
            fallback["max_chars"] = max_chars
            return fallback

    return _budget_compacted_prefill_context(context, max_chars=max_chars, char_count=char_count)


def _prefill_not_configured() -> dict:
    return {"status": "not_configured", "source": "none", "label": "", "messages": [], "message_count": 0}


def _load_prefill_messages_file(file_raw: str, *, source: str = "file", status: str = "loaded") -> dict:
    path = _resolve_prefill_path(file_raw)
    label = path.name or "prefill file"
    if not path.exists():
        return {"status": "error", "source": source, "label": label, "messages": [], "message_count": 0, "error": "prefill file not found"}
    try:
        messages = _valid_prefill_messages(json.loads(path.read_text(encoding="utf-8")))
        return {"status": status, "source": source, "label": label, "messages": messages, "message_count": len(messages)}
    except Exception as exc:
        return {"status": "error", "source": source, "label": label, "messages": [], "message_count": 0, "error": _redact_prefill_status_text(str(exc))}


def _prefill_script_timeout(config_data: dict) -> float:
    raw = os.getenv("HERMES_WEBUI_PREFILL_MESSAGES_SCRIPT_TIMEOUT", "") or str(config_data.get("webui_prefill_messages_script_timeout") or "")
    try:
        return max(0.1, min(float(raw or 5), 30.0))
    except Exception:
        return 5.0


def _prefill_script_command(raw) -> list[str]:
    if isinstance(raw, (list, tuple)):
        return [str(part) for part in raw if str(part)]
    parts = shlex.split(str(raw or ""))
    if not parts:
        return []
    # A single script path mirrors prefill_messages_file path resolution.  More
    # complex commands keep their argv untouched so admins can pass arguments.
    if len(parts) == 1:
        parts[0] = str(_resolve_prefill_path(parts[0]))
    return parts


def _messages_from_prefill_script_output(text: str) -> list[dict]:
    stripped = str(text or "").strip()
    if not stripped:
        return []
    try:
        payload = json.loads(stripped)
    except Exception:
        payload = None
    if isinstance(payload, dict):
        payload = payload.get("messages")
    messages = _valid_prefill_messages(payload)
    if messages:
        return messages
    return [{"role": "user", "content": stripped}]


def _load_prefill_messages_script(config_data: dict) -> dict:
    script_raw = os.getenv("HERMES_WEBUI_PREFILL_MESSAGES_SCRIPT", "") or config_data.get("webui_prefill_messages_script")
    if not script_raw:
        return _prefill_not_configured()
    command = _prefill_script_command(script_raw)
    label = Path(command[0]).name if command else "prefill script"
    if not command:
        return {"status": "error", "source": "script", "label": label, "messages": [], "message_count": 0, "error": "prefill script is empty"}
    try:
        proc = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_prefill_script_timeout(config_data),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "source": "script", "label": label, "messages": [], "message_count": 0, "error": "prefill script timed out"}
    except Exception as exc:
        return {"status": "error", "source": "script", "label": label, "messages": [], "message_count": 0, "error": _redact_prefill_status_text(str(exc))}
    if proc.returncode != 0:
        err = _redact_prefill_status_text(proc.stderr or proc.stdout or f"prefill script exited {proc.returncode}")
        return {"status": "error", "source": "script", "label": label, "messages": [], "message_count": 0, "error": err}
    if len(proc.stdout.encode("utf-8")) > _PREFILL_SCRIPT_OUTPUT_LIMIT:
        return {
            "status": "error",
            "source": "script",
            "label": label,
            "messages": [],
            "message_count": 0,
            "error": f"prefill script output exceeded {_PREFILL_SCRIPT_OUTPUT_LIMIT} bytes",
        }
    messages = _messages_from_prefill_script_output(proc.stdout)
    return {"status": "loaded", "source": "script", "label": label, "messages": messages, "message_count": len(messages)}


def _load_webui_prefill_context(
    config_data: Optional[dict] = None,
) -> dict:
    """Load configured WebUI session prefill messages.

    Supports the same bounded JSON-file shape used by Hermes Agent.  WebUI also
    supports its own explicitly opt-in script hook so admins can bridge Joplin,
    Obsidian, Notion, llm-wiki, or another local notes source into ephemeral
    turn context without baking any one note provider into the WebUI.
    """
    cfg = config_data if isinstance(config_data, dict) else get_config()
    script_context = _load_prefill_messages_script(cfg)
    file_raw = os.getenv("HERMES_PREFILL_MESSAGES_FILE", "") or str(cfg.get("prefill_messages_file") or "")
    if script_context.get("status") == "not_configured":
        if file_raw:
            return _apply_prefill_context_budget(_load_prefill_messages_file(file_raw), cfg)
        return _prefill_not_configured()
    if script_context.get("status") == "error" and file_raw:
        file_context = _load_prefill_messages_file(file_raw, source="file_fallback")
        if file_context.get("status") == "loaded":
            file_context["script_error"] = script_context.get("error", "")
            return _apply_prefill_context_budget(file_context, cfg)
    return _apply_prefill_context_budget(script_context, cfg)


def _public_prefill_context_status(prefill_context: dict) -> dict:
    """Strip message bodies before sending context status to the browser."""
    return {
        "status": prefill_context.get("status", "not_configured"),
        "source": prefill_context.get("source", "none"),
        "label": prefill_context.get("label", ""),
        "message_count": int(prefill_context.get("message_count") or 0),
        **({"error": prefill_context.get("error", "")} if prefill_context.get("error") else {}),
        **({"compacted": True} if prefill_context.get("compacted") else {}),
        **({"original_source": prefill_context.get("original_source", "")} if prefill_context.get("original_source") else {}),
        **({"original_message_count": int(prefill_context.get("original_message_count") or 0)} if prefill_context.get("original_message_count") else {}),
        **({"original_char_count": int(prefill_context.get("original_char_count") or 0)} if prefill_context.get("original_char_count") else {}),
        **({"max_chars": int(prefill_context.get("max_chars") or 0)} if prefill_context.get("max_chars") else {}),
    }


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
    cfg = config_data if isinstance(config_data, dict) else get_config()
    lines: list[str] = []

    display_hermes_home = None
    try:
        from hermes_constants import get_hermes_home, display_hermes_home as _dh
        display_hermes_home = _dh
    except Exception:
        get_hermes_home = None  # type: ignore[assignment]

    connected = ["local (files on this machine)"]
    try:
        if get_hermes_home is not None:
            state_path = get_hermes_home() / "gateway_state.json"
            if state_path.exists():
                raw_state = json.loads(state_path.read_text(encoding="utf-8"))
                platforms = raw_state.get("platforms") if isinstance(raw_state, dict) else {}
                if isinstance(platforms, dict):
                    for name in sorted(platforms):
                        pdata = platforms.get(name) or {}
                        if isinstance(pdata, dict) and pdata.get("state") == "connected" and name != "local":
                            connected.append(f"{name}: Connected ✓")
    except Exception:
        pass
    lines.append(f"**Connected Platforms:** {', '.join(connected)}")

    home_channels = {}
    try:
        platforms_cfg = cfg.get("platforms", {}) if isinstance(cfg, dict) else {}
        if isinstance(platforms_cfg, dict):
            for name, pdata in platforms_cfg.items():
                if not isinstance(pdata, dict):
                    continue
                if pdata.get("enabled") is False:
                    continue
                home = pdata.get("home_channel")
                if isinstance(home, dict):
                    home_channels[str(name)] = str(home.get("name") or name)
    except Exception:
        home_channels = {}

    if home_channels:
        lines.append("")
        lines.append("**Home Channels (default destinations):**")
        for platform, label in sorted(home_channels.items()):
            lines.append(f"  - {platform}: {label}")

    lines.append("")
    lines.append("**Delivery options for scheduled tasks:**")
    lines.append("- `\"origin\"` → Back to this WebUI/browser session when the WebUI runtime supports origin delivery; otherwise prefer an explicit platform target.")
    try:
        home_display = display_hermes_home() if display_hermes_home else "~/.hermes"
    except Exception:
        home_display = "~/.hermes"
    lines.append(f"- `\"local\"` → Save to local files only ({home_display}/cron/output/)")
    for platform, label in sorted(home_channels.items()):
        lines.append(f"- `\"{platform}\"` → Home channel ({label})")
    lines.append("")
    lines.append("*For explicit targeting, use `\"platform:chat_id\"` format if the user provides a specific chat ID. Do not invent private IDs.*")

    return "\n".join(lines)


def _prefill_messages_with_webui_context(prefill_context: dict, config_data: Optional[dict] = None) -> list[dict]:
    """Combine recall prefill with WebUI session context.

    The session context (connected platforms, delivery hints) is injected
    via ``_webui_ephemeral_system_prompt`` / ``ephemeral_system_prompt``
    instead of as a prefill ``user`` message.  Adding it as a user message
    creates two consecutive user turns (prefill + actual) which strict chat
    templates (Mistral, Gemma) reject with a Jinja 500.
    """
    return list(prefill_context.get("messages") or [])


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
    sanitized = list(prefill_messages or [])
    n_dropped = 0
    while sanitized:
        last_message = sanitized[-1]
        if not isinstance(last_message, dict):
            break
        if str(last_message.get("role") or "").strip().lower() != "user":
            break
        sanitized.pop()
        n_dropped += 1
    if n_dropped:
        logger.debug("Dropped %d trailing user message(s) from prefill", n_dropped)
    return sanitized


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
from api.models import get_session, title_from

# Fields that are safe to send to LLM provider APIs.
# Everything else (attachments, timestamp, _ts, etc.) is display-only
# metadata added by the webui and must be stripped before the API call.
# `reasoning_content` is provider-facing for reasoning-capable models. Display
# metadata such as `reasoning`, `thinking`, and `_reasoning` stays omitted here.
_API_SAFE_MSG_KEYS = {'role', 'content', 'tool_calls', 'tool_call_id', 'name', 'refusal', 'reasoning_content'}

_NATIVE_IMAGE_MAX_BYTES = 20 * 1024 * 1024

_GATEWAY_ROUTING_TOP_LEVEL_KEYS = {
    'used_provider',
    'used_model',
    'requested_provider',
    'requested_model',
}
_GATEWAY_ROUTING_CONTAINER_KEYS = (
    'llm_gateway',
    'gateway',
    'metadata',
    'response_metadata',
    'routing_metadata',
    'usage',
)
_GATEWAY_ROUTING_ATTEMPT_KEYS = {
    'provider', 'model', 'status', 'reason', 'selection_reason', 'score',
    'latency_ms', 'error', 'timestamp', 'selected', 'attempt', 'attempt_index',
}


def _clean_gateway_routing_scalar(value):
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        text = str(value).strip()
        if not text:
            return None
        return value if isinstance(value, (int, float, bool)) else text[:240]
    return None


def _find_gateway_metadata_payload(payload):
    if not isinstance(payload, dict):
        return None
    if any(k in payload for k in _GATEWAY_ROUTING_TOP_LEVEL_KEYS) or isinstance(payload.get('routing'), list):
        return payload
    for key in _GATEWAY_ROUTING_CONTAINER_KEYS:
        nested = payload.get(key)
        found = _find_gateway_metadata_payload(nested)
        if found:
            return found
    return None


def _normalize_gateway_routing_metadata(payload, requested_model=None, requested_provider=None):
    """Return safe LLM Gateway routing metadata, or None when absent.

    LLM Gateway response metadata can contain provider/model routing details,
    but WebUI must only persist display-safe scalars and a bounded routing list.
    Secrets or provider-specific request objects are deliberately ignored.
    """
    src = _find_gateway_metadata_payload(payload)
    if not src:
        return None

    normalized = {}
    for key in _GATEWAY_ROUTING_TOP_LEVEL_KEYS:
        value = _clean_gateway_routing_scalar(src.get(key))
        if value is not None:
            normalized[key] = value

    if 'requested_model' not in normalized:
        fallback_model = _clean_gateway_routing_scalar(requested_model)
        if fallback_model is not None:
            normalized['requested_model'] = fallback_model
    if 'requested_provider' not in normalized:
        fallback_provider = _clean_gateway_routing_scalar(requested_provider)
        if fallback_provider is not None:
            normalized['requested_provider'] = fallback_provider

    routing = []
    raw_routing = src.get('routing')
    if isinstance(raw_routing, list):
        for attempt in raw_routing[:12]:
            if not isinstance(attempt, dict):
                continue
            clean_attempt = {}
            for key in _GATEWAY_ROUTING_ATTEMPT_KEYS:
                value = _clean_gateway_routing_scalar(attempt.get(key))
                if value is not None:
                    clean_attempt[key] = value
            if clean_attempt:
                routing.append(clean_attempt)
    if routing:
        normalized['routing'] = routing

    used_provider = str(normalized.get('used_provider') or '').strip().lower()
    requested_provider_norm = str(normalized.get('requested_provider') or '').strip().lower()
    used_model = str(normalized.get('used_model') or '').strip().lower()
    requested_model_norm = str(normalized.get('requested_model') or '').strip().lower()
    provider_changed = bool(used_provider and requested_provider_norm and used_provider != requested_provider_norm)
    model_changed = bool(used_model and requested_model_norm and used_model != requested_model_norm)
    attempted_providers = [
        str(a.get('provider') or '').strip().lower()
        for a in routing
        if a.get('provider')
    ]
    distinct_attempted_providers = {p for p in attempted_providers if p}
    failed_before_selection = any(
        str(a.get('status') or '').strip().lower() in {'failed', 'error', 'timeout', 'rejected'}
        for a in routing
    )
    has_failover = bool(provider_changed or len(distinct_attempted_providers) > 1 or failed_before_selection)

    if not (
        normalized.get('used_provider') or normalized.get('used_model') or routing or provider_changed or model_changed
    ):
        return None
    normalized['provider_changed'] = provider_changed
    normalized['model_changed'] = model_changed
    normalized['has_failover'] = has_failover
    return normalized


def _extract_gateway_routing_metadata(agent, result, requested_model=None, requested_provider=None):
    candidates = []
    if isinstance(result, dict):
        candidates.extend([
            result.get('llm_gateway'),
            result.get('gateway'),
            result.get('metadata'),
            result.get('response_metadata'),
            result.get('routing_metadata'),
            result.get('usage'),
            result,
        ])
    for attr in (
        'llm_gateway_metadata',
        'gateway_metadata',
        'last_response_metadata',
        'response_metadata',
        'routing_metadata',
        'last_usage',
    ):
        if agent is not None:
            candidates.append(getattr(agent, attr, None))
    for candidate in candidates:
        normalized = _normalize_gateway_routing_metadata(
            candidate,
            requested_model=requested_model,
            requested_provider=requested_provider,
        )
        if normalized:
            return normalized
    return None


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
    return is_context_compression_marker(msg)


def _compact_summary_text(raw_text: str | None) -> str | None:
    """Normalize a text blob used in compression summary cards."""
    if not isinstance(raw_text, str):
        return None
    txt = raw_text.strip()
    if not txt:
        return None
    return re.sub(r"\s+", " ", txt).strip()


def _compression_anchor_message_key(message):
    if not isinstance(message, dict):
        return None
    role = str(message.get('role') or '')
    if not role or role == 'tool':
        return None
    content = message.get('content', '')
    text = _message_text(content)
    if len(text) > 160:
        text = text[:160]
    ts = message.get('_ts') or message.get('timestamp')
    attachments = message.get('attachments')
    attach_count = len(attachments) if isinstance(attachments, list) else 0
    if not text and not attach_count and not ts:
        return None
    return {'role': role, 'ts': ts, 'text': text, 'attachments': attach_count}


def _compression_summary_from_messages(messages):
    for m in reversed(messages or []):
        if not isinstance(m, dict):
            continue
        if not _is_context_compression_marker(m):
            continue
        text = _message_text(m.get('content'))
        if text:
            return text
    return None


def _find_current_user_turn(messages, msg_text):
    needle = " ".join(str(msg_text or '').split())
    last_strong_match = None  # _looks_like_current_user_turn (high confidence)
    last_weak_match = None    # needle substring match (lower confidence)
    fallback = None
    for idx, msg in enumerate(messages or []):
        if not isinstance(msg, dict) or msg.get('role') != 'user':
            continue
        fallback = idx
        if _looks_like_current_user_turn(msg, msg_text):
            last_strong_match = idx
            continue
        text = " ".join(
            _strip_workspace_prefix(
                _message_text(msg.get('content', '')),
                include_legacy=True,
            ).split()
        )
        if needle and (needle in text or text in needle):
            last_weak_match = idx
    # Return the LAST matching user turn. After context compression the agent's
    # result_messages contain the full conversation history; if the user asked a
    # similar question in an earlier turn, first-match would return that old
    # index, causing the merge to replay the entire history from that point.
    # Last-match anchors on the current turn instead.
    #
    # Prefer the last STRONG match (an exact `_looks_like_current_user_turn`
    # hit) over the last WEAK substring match. The agent loop appends synthetic
    # `role:"user"` continuation prompts (e.g. "Continue", empty-recovery nudges
    # — see conversation_loop.py) AFTER the real user turn; those can weak-match
    # `msg_text` and, if weak matches were allowed to win, would anchor the merge
    # PAST the real turn and drop the assistant/tool output in between. The real
    # current turn is the last strong match, so it must take priority.
    if last_strong_match is not None:
        return last_strong_match
    if last_weak_match is not None:
        return last_weak_match
    return fallback


def _drop_checkpointed_current_user_from_context(messages, msg_text):
    """Return model history without an eager-checkpointed current user turn."""
    history = list(messages or [])
    if not history:
        return history
    current_user_key = _message_identity({'role': 'user', 'content': msg_text})
    if current_user_key and _message_identity(history[-1]) == current_user_key:
        return history[:-1]
    return history


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
    from api import profiles as profiles_api

    with profiles_api.profile_env_for_background_worker(
        session,
        "streaming checkpoint",
        logger_override=logger,
    ):
        session.save(skip_index=True)


def _normalize_fresh_chat_text(text):
    text = _strip_workspace_prefix(str(text or ''), include_legacy=True)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text.strip(" \t\r\n.!?。！？,，~～")


def _is_casual_fresh_chat_message(msg_text):
    """Return True for short opener messages that should not resume old tasks."""
    text = _normalize_fresh_chat_text(msg_text)
    if not text or len(text) > 24:
        return False
    continuation_terms = (
        "continue",
        "resume",
        "carry on",
        "go on",
        # CJK continuation terms (zh-CN): jixu, jiezhe, wangxia, xiayibu.
        # Encoded as Python escape sequences (not literal CJK) so api/streaming.py
        # passes tests/test_title_sanitization.py::test_title_generation_source_has_no_cjk_literals,
        # which scans this file for any U+4E00-U+9FFF code points. Runtime
        # comparisons still use the real CJK strings — Python decodes the
        # escapes at compile time.
        "\u7ee7\u7eed",
        "\u63a5\u7740",
        "\u5f80\u4e0b",
        "\u4e0b\u4e00\u6b65",
    )
    if any(term in text for term in continuation_terms):
        return False
    return text in {
        "hi",
        "hello",
        "hey",
        "hello there",
        "hi there",
        # CJK greetings (zh-CN): nihao, ninhao, hai, haluo, zaima, zaime.
        # Same escape-sequence rationale as the continuation block above.
        "\u4f60\u597d",         # nihao
        "\u60a8\u597d",         # ninhao
        "\u55e8",               # hai (was \u5616 = "click of tongue", not a greeting)
        "\u54c8\u55bd",         # haluo (was \u54c8\u5582 = uncommon "ha-wei" variant)
        "\u5728\u5417",         # zaima
        "\u5728\u4e48",         # zaime
    }


def _has_task_resume_compaction_marker(messages):
    """Detect compacted model context that tells the agent to resume an old task."""
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        text = _message_text(msg.get('content', '')).lower()
        if not text:
            continue
        if "context compaction" not in text and "context compression" not in text:
            continue
        if (
            "active task" in text
            or "resume exactly" in text
            or "current task" in text
            or "task list was preserved" in text
            or "in_progress" in text
        ):
            return True
    return False


def _new_turn_context_from_messages(messages, msg_text):
    """Return provider-facing history for a new user turn from a message list."""
    history = _drop_checkpointed_current_user_from_context(messages, msg_text)
    if _is_casual_fresh_chat_message(msg_text) and _has_task_resume_compaction_marker(history):
        return []
    return history


def _context_messages_for_new_turn(session, msg_text):
    """Return provider-facing history for a new user turn.

    Compacted agent sessions can carry a hidden "resume the active task" summary
    in context_messages. If the user starts a fresh casual greeting in that old
    session, do not feed that stale active-task summary back to the model.
    """
    return _new_turn_context_from_messages(_session_context_messages(session), msg_text)


def _stream_writeback_is_current(session, stream_id):
    """Return True only while a worker still owns the session writeback.

    cancel_stream() intentionally clears ``active_stream_id`` early so the UI can
    accept a follow-up turn while the old worker is unwinding. That old worker
    must not later persist its stale result over the newer transcript.
    """
    return bool(stream_id) and getattr(session, 'active_stream_id', None) == stream_id


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
    if getattr(session, 'active_stream_id', None):
        return False
    if getattr(session, 'pending_user_message', None):
        return False
    if getattr(session, 'pending_attachments', None):
        return False
    messages = list(getattr(session, 'messages', None) or [])
    if len(messages) < 2:
        return False
    last = messages[-1]
    if not isinstance(last, dict) or not last.get('_error'):
        return False
    if last.get('type') != 'interrupted':
        return False
    content = str(last.get('content') or '')
    if 'Response interrupted' not in content or 'before this turn finished' not in content:
        return False

    expected = ' '.join(str(msg_text or '').split())
    if not expected:
        return False
    for msg in reversed(messages[:-1]):
        if not isinstance(msg, dict):
            continue
        if msg.get('_error'):
            continue
        if msg.get('role') != 'user':
            continue
        actual = ' '.join(str(msg.get('content') or '').split())
        return actual == expected
    return False


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
    if not getattr(session, 'truncation_watermark', None):
        return
    messages = getattr(session, 'messages', None) or []
    # Walk backwards to find the newest user message timestamp
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get('role') == 'user':
            ts = msg.get('timestamp')
            if isinstance(ts, (int, float)) and ts > 0:
                session.truncation_watermark = float(ts)
                return
    session.truncation_watermark = time.time()


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
    """Run agent in background thread, writing SSE events to STREAMS[stream_id].

    When ephemeral=True, session mutations are skipped — used by /btw to get
    a streaming answer without persisting to the parent session.
    """
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
                                    from api.routes import (
                                        _context_length_lookup_inputs_for_model as _cli_u,
                                        _should_accept_session_context_length_refresh as _accept_u,
                                    )
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
                                        from api.routes import (
                                            _should_accept_session_context_length_refresh as _accept2_u,
                                        )
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
                        from api.routes import (
                            _context_length_lookup_inputs_for_model as _cli_cc,
                            _should_accept_session_context_length_refresh as _accept_cc,
                        )
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
                        from api.routes import _context_length_lookup_inputs_for_model
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
                    from api.routes import (
                        _context_length_lookup_inputs_for_model as _cli_sse,
                        _should_accept_session_context_length_refresh as _accept_sse,
                    )
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
                    from api.routes import _context_length_lookup_inputs_for_model
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

# ============================================================
# SECTION: HTTP Request Handler
# do_GET: read-only API endpoints + SSE stream + static HTML
# do_POST: mutating endpoints (session CRUD, chat, upload, approval)
# Routing is a flat if/elif chain. See ARCHITECTURE.md section 4.1.
# ============================================================


def _handle_chat_steer(handler, body: dict) -> bool:
    """Inject a /steer payload into the active agent for a session.

    Mirrors the CLI's `/steer <text>` command (cli.py:6140-6155):
      - Look up the cached AIAgent for the session (PR #1051's
        SESSION_AGENT_CACHE).
      - Verify a stream is currently active for this session.
      - Call agent.steer(text) — thread-safe, stashes text in
        _pending_steer for application at the next tool-result boundary.

    The agent's loop calls _apply_pending_steer_to_tool_results() at the
    end of every tool batch and appends the steer text to the last tool
    result's content with a marker, so the model sees the steer as part
    of the tool output on its next iteration. The user's stream is NOT
    interrupted.

    If no agent is cached, the agent is too old to support steer, or no
    stream is active, return {"accepted": False, "fallback": "<reason>"}.
    The frontend must surface that failure without cancelling the active run;
    Steer is active-run guidance, not implicit permission to Queue, Interrupt,
    or Stop-and-send.

    Returns 200 with {"accepted": bool, "fallback": str|None,
    "stream_id": str|None}.
    """
    from api.helpers import j, bad
    from api import config as _cfg

    sid = str((body or {}).get("session_id", "") or "").strip()
    text = str((body or {}).get("text", "") or "").strip()
    if not sid:
        return bad(handler, "session_id required")
    if not text:
        return bad(handler, "text required")

    evicted_cached_entry = None
    with _cfg.SESSION_AGENT_CACHE_LOCK:
        cached = _cfg.SESSION_AGENT_CACHE.get(sid)
        if cached:
            agent = cached[0]
            if not _cached_agent_matches_session(agent, sid):
                evicted_cached_entry = _cfg.SESSION_AGENT_CACHE.pop(sid, None)
                logger.warning(
                    '[webui] Evicted cached agent before steer due to mismatched session identity: cache_key=%s agent_session_id=%s',
                    sid,
                    _cached_agent_session_identity(agent),
                )
                cached = None
    if evicted_cached_entry is not None:
        try:
            _close_cached_agent_entry_at_session_boundary(sid, evicted_cached_entry)
        except Exception:
            logger.debug("Failed to close steer identity-mismatched cached agent for session %s", sid, exc_info=True)
    if not cached:
        try:
            s = get_session(sid)
            active_stream_id = getattr(s, "active_stream_id", None) or None
        except KeyError:
            active_stream_id = None
        if active_stream_id:
            with _cfg.STREAMS_LOCK:
                stream_alive = active_stream_id in _cfg.STREAMS
            if stream_alive:
                try:
                    with _cfg.ACTIVE_RUNS_LOCK:
                        active_run = dict((_cfg.ACTIVE_RUNS or {}).get(str(active_stream_id)) or {})
                    if active_run.get("backend") == "gateway":
                        return j(handler, {"accepted": False, "fallback": "gateway_steer_queued",
                                           "stream_id": active_stream_id})
                except Exception:
                    logger.warning(
                        "Gateway ownership lookup failed before steer fallback for session=%s stream_id=%s",
                        sid,
                        active_stream_id,
                        exc_info=True,
                    )
        # No active local agent for this session — caller surfaces a steer failure
        # without cancelling the active run.
        return j(handler, {"accepted": False, "fallback": "no_cached_agent",
                           "stream_id": None})
    agent = cached[0]
    if not hasattr(agent, "steer"):
        # Older hermes-agent that pre-dates the steer() method
        return j(handler, {"accepted": False, "fallback": "agent_lacks_steer",
                           "stream_id": None})

    # Verify the agent is currently running. Use the session's
    # active_stream_id rather than calling load_session_locked() which
    # would block on the streaming thread's lock.
    try:
        s = get_session(sid)
    except KeyError:
        return j(handler, {"accepted": False, "fallback": "session_not_found",
                           "stream_id": None})
    active_stream_id = getattr(s, "active_stream_id", None) or None
    if not active_stream_id:
        return j(handler, {"accepted": False, "fallback": "not_running",
                           "stream_id": None})
    with _cfg.STREAMS_LOCK:
        stream_alive = active_stream_id in _cfg.STREAMS
    if not stream_alive:
        # Active stream id is stale — stream has ended; caller falls back
        return j(handler, {"accepted": False, "fallback": "stream_dead",
                           "stream_id": None})

    try:
        accepted = bool(agent.steer(text))
    except Exception as exc:
        logger.debug("agent.steer() raised for session=%s: %s", sid, exc)
        return j(handler, {"accepted": False, "fallback": "steer_error",
                           "stream_id": active_stream_id})

    return j(handler, {"accepted": accepted, "fallback": None,
                       "stream_id": active_stream_id})


def cancel_stream(stream_id: str) -> bool:
    """Signal an in-flight stream to cancel. Returns True if work was found.

    The runtime owner eagerly releases transport admission state; this function
    interrupts the agent and persists the cancelled session so a successor
    /api/chat/start can be admitted while the old worker unwinds.

    Session cleanup runs outside process-local locks to preserve lock ordering
    (streaming thread does LOCK → STREAMS_LOCK; inverting would deadlock).
    """
    from api import config as _live_config

    # The runtime owner atomically snapshots progress, marks the worker as
    # cancelling, signals its cancel event, and releases the transport values
    # that block successor admission. Session persistence remains below and
    # deliberately runs after the process-local locks have been released.
    cancellation = _live_config.begin_runtime_cancel(stream_id)
    if cancellation is None:
        return False

    stream_present = cancellation.had_transport
    active_run_session_id = cancellation.session_id
    agent = cancellation.agent
    q = cancellation.channel
    _cancel_partial_text = cancellation.partial_text
    _cancel_reasoning = cancellation.reasoning_text
    _cancel_tool_calls = list(cancellation.live_tool_calls)
    _cancel_session_payload = None

    # Interrupt the AIAgent instance to stop tool execution. Use the
    # lock-snapshot agent when the stream was present; otherwise fall back to
    # the session agent cache via the active-run session id.
    if agent is None and active_run_session_id:
        try:
            with _live_config.SESSION_AGENT_CACHE_LOCK:
                cached = _live_config.SESSION_AGENT_CACHE.get(active_run_session_id)
            if cached and _cached_agent_matches_session(cached[0], active_run_session_id):
                agent = cached[0]
        except Exception:
            pass
    if agent:
        try:
            agent.interrupt("Cancelled by user")
        except Exception as e:
            # Log but don't block the cancel flow
            import logging
            logging.getLogger(__name__).debug(
                f"Failed to interrupt agent for stream {stream_id}: {e}"
            )
    elif stream_present:
        # Agent not yet stored - cancel_event flag will be checked by agent thread
        import logging
        logging.getLogger(__name__).debug(
            f"Cancel requested for stream {stream_id} before agent ready - "
            f"cancel_event flag set, will be checked on agent startup"
        )

    # Clear any pending clarify prompt so the blocked tool call can unwind.
    try:
        from api.clarify import clear_pending as _clear_clarify_pending

        _clarify_session_id = getattr(agent, "session_id", None) if agent else active_run_session_id
        if _clarify_session_id:
            _clear_clarify_pending(_clarify_session_id)
    except Exception:
        logger.debug("Failed to clear clarify prompt during cancel")

    # Capture the queue while the stream still exists, but do not emit the
    # terminal cancel event until the session cleanup below confirms the turn
    # is still active. Otherwise a late Stop click can race with a successful
    # worker save and show cancel in the client while persistence says done.
    _emit_cancel_event = True

    # Resolve the cancel session id from the runtime snapshot or agent handle.
    # Session cleanup (get_session + save) must happen OUTSIDE the lock —
    # get_session() acquires LOCK, and the streaming thread does LOCK first
    # then STREAMS_LOCK, so inverting the order here would cause deadlock.
    _cancel_session_id = getattr(agent, 'session_id', None) if agent else None
    if not _cancel_session_id and active_run_session_id:
        _cancel_session_id = active_run_session_id

    # Session cleanup stays outside STREAMS_LOCK to preserve lock ordering. The
    # repository reloads the authoritative full session under its per-session
    # owner, so cancellation races neither checkpoint/undo/retry writers nor a
    # cached metadata projection.
    if _cancel_session_id:
        _cancel_persisted = False
        try:
            with edit_session(
                _cancel_session_id,
                save_when=lambda _current: _cancel_persisted,
            ) as _cs:
                if not isinstance(getattr(_cs, 'messages', None), list):
                    _cs.messages = []
                if not _stream_writeback_is_current(_cs, stream_id):
                    # The stream has rotated to a different stream id (newer
                    # turn started, or the worker already finalized this one).
                    # Skip the cancel-marker append AND suppress the terminal
                    # cancel event so we don't contradict a possibly-already-
                    # delivered done payload (#2151 + #2154 / PR #2136).
                    logger.info(
                        "Skipping stale cancel writeback for session %s stream %s; active_stream_id=%s",
                        _cancel_session_id,
                        stream_id,
                        getattr(_cs, 'active_stream_id', None),
                    )
                    _emit_cancel_event = False
                    return True
                # ── Preserve the user's typed message before clearing pending state (#1298) ──
                # The agent's internal messages list (where the user message was appended at
                # the start of run_conversation()) may not have been merged back into
                # _cs.messages yet — cancel_stream() races with the streaming thread's final
                # _merge_display_messages_after_agent_result() call. Without this guard, the
                # user's message is lost: pending_user_message gets cleared below, and
                # _cs.messages still only contains messages from prior turns. The reporter
                # of #1298 sees their typed text vanish from chat after clicking Stop.
                #
                # Recovery rule: if pending_user_message is set AND the latest message in
                # _cs.messages isn't already a matching user turn, synthesize one. The
                # match check guards against double-append when the streaming thread DID
                # reach its merge step before cancel_stream() got the session lock.
                #
                # Wrapped in its own try/except so an unexpected _cs.messages shape (e.g.
                # in unit tests using Mock sessions) cannot escape and skip the rest of
                # the cleanup.
                try:
                    _pending_user = getattr(_cs, 'pending_user_message', None)
                    _pending_source = getattr(_cs, 'pending_user_source', None)
                    _pending_atts_raw = getattr(_cs, 'pending_attachments', None)
                    _pending_atts = list(_pending_atts_raw) if isinstance(_pending_atts_raw, (list, tuple)) else []
                    _pending_started = getattr(_cs, 'pending_started_at', None) or 0
                    _msgs_for_recovery = _cs.messages if isinstance(_cs.messages, list) else None
                    if _pending_user and _msgs_for_recovery is not None:
                        _last_user = None
                        for _m in reversed(_msgs_for_recovery):
                            if isinstance(_m, dict) and _m.get('role') == 'user':
                                _last_user = _m
                                break
                        _already_persisted = False
                        if _last_user is not None:
                            _last_content = _last_user.get('content')
                            _last_ts = _last_user.get('timestamp') or 0
                            # Only treat as already-persisted if the latest user turn
                            # was created AT OR AFTER the current turn's pending_started_at.
                            # An earlier turn whose content happens to be a substring
                            # (e.g. prior reply was "ok", user now types "ok please continue")
                            # must NOT short-circuit synthesis — that would re-introduce
                            # the data-loss bug this guard is supposed to prevent.
                            if isinstance(_last_content, str) and _last_ts >= _pending_started:
                                # Tolerate the workspace prefix the streaming thread prepends.
                                if _pending_user == _last_content or _pending_user in _last_content:
                                    _already_persisted = True
                        if not _already_persisted:
                            _recovered_ts = int(time.time())
                            if isinstance(_pending_started, (int, float)) and _pending_started > 0:
                                _recovered_ts = int(_pending_started)
                            _user_turn: dict = {
                                'role': 'user',
                                'content': _pending_user,
                                'timestamp': _recovered_ts,
                            }
                            if _pending_source and _pending_source != 'webui':
                                _user_turn['_source'] = _pending_source
                            if _pending_atts:
                                _user_turn['attachments'] = _pending_atts
                            _msgs_for_recovery.append(_user_turn)
                except Exception:
                    logger.debug(
                        "Failed to recover pending user message on cancel for %s",
                        _cancel_session_id,
                    )
                _cs.active_stream_id = None
                _cs.pending_user_message = None
                _cs.pending_attachments = []
                _cs.pending_started_at = None
                _cs.pending_user_source = None
                # Persist any partial assistant text that was streamed before cancel (#893).
                # Preserving partial content means the user sees what the agent had
                # produced rather than losing it entirely.  The marker is _partial=True
                # (for session/UI identification only) — NOT _error=True — so the partial
                # content IS kept in the history sent to the agent on the next user
                # message, letting the model continue from where it was cut off.
                # See the inner comment on the append call below for the rationale.
                #
                # #1361: Also persist reasoning trace and live tool calls that were
                # accumulated in thread-local variables but invisible to the cancel path.
                # This prevents paid-token data loss when cancelling mid-reasoning or
                # mid-tool-execution.
                # NOTE on _partial_tool_calls: the captured entries use the WebUI
                # internal shape {name, args, done, duration, is_error} — they do
                # NOT carry the OpenAI/Anthropic API id + function: {name, arguments}
                # envelope. Storing under 'tool_calls' would cause
                # _sanitize_messages_for_api to forward them to the next-turn LLM
                # call and strict providers would 400 on the malformed entries.
                # The underscore-prefixed key is not in the whitelist, so sanitize
                # strips it. The UI reads it via static/messages.js. (v0.50.251.)
                _partial_msg = _build_partial_message(
                    _cancel_partial_text, _cancel_reasoning, _cancel_tool_calls,
                )
                _cancel_marker_exists = _session_has_cancel_marker(_cs)
                _cancel_marker_idx = len(_cs.messages)
                if _cancel_marker_exists:
                    for _idx in range(len(_cs.messages) - 1, -1, -1):
                        _m = _cs.messages[_idx]
                        if not isinstance(_m, dict) or _m.get('role') != 'assistant':
                            continue
                        _content = str(_m.get('content') or '').strip().lower()
                        if any(pattern in _content for pattern in _CANCEL_MARKER_PATTERNS):
                            _cancel_marker_idx = _idx
                            break
                if _partial_msg is not None:
                    # Deduplicate against the full partial payload, not just
                    # non-empty content. Tool-only/reasoning-only partials have
                    # empty content, so a content-gated check can append the same
                    # failed turn repeatedly during cancel/replay recovery (#2592).
                    if not _partial_marker_already_present(
                        _cs.messages,
                        _partial_msg,
                        before_idx=_cancel_marker_idx,
                    ):
                        _cs.messages.insert(_cancel_marker_idx, _partial_msg)
                # Cancel marker — flagged _error=True so it is stripped from conversation
                # history on the next turn (prevents model from seeing "Task cancelled."
                # as a prior assistant reply).
                if not _cancel_marker_exists:
                    _cs.messages.append({
                        'role': 'assistant',
                        'content': _cancelled_turn_content(
                            'Task cancelled.',
                            _preferred_agent_display_name_for_session(_cs),
                        ),
                        '_error': True,
                        'provider_details': 'Task cancelled.',
                        'provider_details_label': 'Cancellation details',
                        'timestamp': int(time.time()),
                    })
                _cancel_persisted = True
            if _cancel_persisted:
                _cancel_session_payload = _redacted_session_payload_with_full_messages(_cs)
        except Exception:
            logger.debug("Failed to clear session state on cancel for %s", _cancel_session_id)

    if _emit_cancel_event and q:
        _cancel_event_id = cancellation.last_event_id
        if _cancel_event_id and hasattr(q, "note_last_event_id"):
            try:
                q.note_last_event_id(_cancel_event_id)
            except Exception:
                logger.debug("Failed to note cancel event_id %s for stream %s", _cancel_event_id, stream_id, exc_info=True)
        try:
            _payload = _cancel_event_payload('Cancelled by user', session=_cancel_session_payload)
            q.put_nowait(('cancel', _payload))
        except Exception:
            logger.debug("Failed to put cancel event to queue")

    return True
