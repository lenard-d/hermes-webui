"""Manual session compression execution, job lifecycle, and status routes."""

# ruff: noqa: F841 - preserve the byte-identical legacy handler implementation

from __future__ import annotations

import copy
import io
import json
import re
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from api.runs.agent_runtime import (
        AgentRuntimeChangedError,
        ensure_agent_runtime_current,
        require_ai_agent_class,
    )
    from api.compression_anchor import visible_messages_for_anchor
    from api.config import _resolve_cli_toolsets
    from api.helpers import (
        _sanitize_error,
        bad,
        j,
        redact_session_data,
        require,
    )
    from api.models import get_session
    from api.routes import _session_is_subagent_view_only, logger


_MANUAL_COMPRESSION_JOBS: dict[str, dict] = {}
_MANUAL_COMPRESSION_JOBS_LOCK = threading.Lock()
_MANUAL_COMPRESSION_JOB_TTL_SECONDS = 10 * 60


class _ManualCompressionMemoryHandler:
    def __init__(self):
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers[key] = value

    def end_headers(self):
        pass

    def payload(self):
        raw = self.wfile.getvalue().decode("utf-8")
        return json.loads(raw) if raw else {}


def _manual_compression_cleanup_locked(now=None):
    now = time.time() if now is None else now
    for sid, job in list(_MANUAL_COMPRESSION_JOBS.items()):
        if job.get("status") == "running":
            continue
        updated_at = float(job.get("updated_at") or job.get("started_at") or now)
        if now - updated_at > _MANUAL_COMPRESSION_JOB_TTL_SECONDS:
            _MANUAL_COMPRESSION_JOBS.pop(sid, None)


def _manual_compression_status_payload(job):
    status = job.get("status") or "running"
    payload = {
        "ok": status not in {"error", "cancelled"},
        "status": status,
        "session_id": job.get("session_id"),
        "focus_topic": job.get("focus_topic"),
        "started_at": job.get("started_at"),
        "updated_at": job.get("updated_at"),
    }
    if status == "done":
        result = job.get("result")
        if isinstance(result, dict):
            payload.update(result)
        payload["status"] = "done"
        payload["ok"] = True
    elif status == "error":
        payload["ok"] = False
        payload["error"] = job.get("error") or "Compression failed"
        payload["error_status"] = int(job.get("error_status") or 400)
        if job.get("error_type"):
            payload["type"] = job["error_type"]
        if job.get("retryable") is not None:
            payload["retryable"] = bool(job["retryable"])
    elif status == "cancelled":
        payload["ok"] = False
        payload["error"] = job.get("error") or "Compression cancelled"
        payload["error_status"] = int(job.get("error_status") or 409)
    return payload


def _run_manual_compression_job(sid, body):
    memory_handler = _ManualCompressionMemoryHandler()
    try:
        try:
            session = get_session(sid)
        except KeyError:
            session = None
        if session is not None:
            from api import profiles as profiles_api

            with profiles_api.profile_env_for_background_worker(session, "manual compression", logger_override=logger):
                _handle_session_compress(memory_handler, body)
        else:
            _handle_session_compress(memory_handler, body)
        status = int(memory_handler.status or 500)
        payload = memory_handler.payload()
        with _MANUAL_COMPRESSION_JOBS_LOCK:
            job = _MANUAL_COMPRESSION_JOBS.get(sid)
            if not job:
                return
            now = time.time()
            if status >= 400 or not isinstance(payload, dict) or payload.get("error"):
                job.update(
                    {
                        "status": "error",
                        "error": str((payload or {}).get("error") or "Compression failed"),
                        "error_status": status,
                        "error_type": (payload or {}).get("type"),
                        "retryable": (payload or {}).get("retryable"),
                        "updated_at": now,
                    }
                )
            else:
                job.update(
                    {
                        "status": "done",
                        "result": payload,
                        "updated_at": now,
                    }
                )
    except AgentRuntimeChangedError as exc:
        logger.warning("Manual compression worker found stale Agent runtime for session %s", sid)
        with _MANUAL_COMPRESSION_JOBS_LOCK:
            job = _MANUAL_COMPRESSION_JOBS.get(sid)
            if job:
                job.update(
                    {
                        "status": "error",
                        "error": str(exc),
                        "error_status": 409,
                        "error_type": "agent_runtime_stale",
                        "retryable": True,
                        "updated_at": time.time(),
                    }
                )
    except Exception as exc:
        logger.warning("Manual compression worker failed for session %s: %s", sid, exc)
        with _MANUAL_COMPRESSION_JOBS_LOCK:
            job = _MANUAL_COMPRESSION_JOBS.get(sid)
            if job:
                job.update(
                    {
                        "status": "error",
                        "error": f"Compression failed: {_sanitize_error(exc)}",
                        "error_status": 500,
                        "updated_at": time.time(),
                    }
                )


def _handle_session_compress_start(handler, body):
    try:
        require(body, "session_id")
    except ValueError as e:
        return bad(handler, str(e))

    sid = str(body.get("session_id") or "").strip()
    if not sid:
        return bad(handler, "session_id is required")
    try:
        s = get_session(sid)
    except KeyError:
        return bad(handler, "Session not found", 404)
    if getattr(s, "active_stream_id", None):
        return bad(handler, "Session is still streaming; wait for the current turn to finish.", 409)

    focus_topic = str(body.get("focus_topic") or body.get("topic") or "").strip()[:500] or None
    job_body = {"session_id": sid}
    if focus_topic:
        job_body["focus_topic"] = focus_topic

    # Repeated start requests observe an existing running job and do not admit
    # new Agent work, so preserve that idempotent read even if the checkout has
    # since changed. Do not hold the job lock while running Git subprocesses.
    now = time.time()
    with _MANUAL_COMPRESSION_JOBS_LOCK:
        _manual_compression_cleanup_locked(now)
        existing = _MANUAL_COMPRESSION_JOBS.get(sid)
        if existing:
            existing_payload = _manual_compression_status_payload(existing)
            if existing_payload.get("status") == "running":
                return j(handler, existing_payload)

    # Reject a stale local Agent runtime before creating the asynchronous job.
    try:
        ensure_agent_runtime_current()
    except AgentRuntimeChangedError as exc:
        return j(
            handler,
            {
                "error": str(exc),
                "type": "agent_runtime_stale",
                "retryable": True,
            },
            status=409,
        )

    # Another start request may have admitted a job while the runtime check ran.
    # Re-check under the lock so only one worker is created.
    now = time.time()
    with _MANUAL_COMPRESSION_JOBS_LOCK:
        _manual_compression_cleanup_locked(now)
        existing = _MANUAL_COMPRESSION_JOBS.get(sid)
        if existing:
            existing_payload = _manual_compression_status_payload(existing)
            if existing_payload.get("status") == "running":
                return j(handler, existing_payload)
            # Stage-344 Opus SHOULD-FIX (#2128): always start fresh on re-invoke.
            # The prior implementation short-circuited and returned a stale `done`
            # payload for the full 10-minute TTL window when /compress/start was
            # re-invoked, so a user closing the tab mid-compress and re-running
            # /compress on a fresh open would get the previous result back rather
            # than a new compression. Drop the entry and fall through to the
            # fresh-worker path below.
            _MANUAL_COMPRESSION_JOBS.pop(sid, None)
        job = {
            "session_id": sid,
            "focus_topic": focus_topic,
            "status": "running",
            "started_at": now,
            "updated_at": now,
        }
        _MANUAL_COMPRESSION_JOBS[sid] = job

    worker = threading.Thread(
        target=_run_manual_compression_job,
        args=(sid, job_body),
        name=f"manual-compress-{sid[:8]}",
        daemon=True,
    )
    worker.start()

    with _MANUAL_COMPRESSION_JOBS_LOCK:
        return j(handler, _manual_compression_status_payload(_MANUAL_COMPRESSION_JOBS.get(sid, job)))


def _handle_session_compress_status(handler, sid):
    sid = str(sid or "").strip()
    if not sid:
        return bad(handler, "session_id is required")
    with _MANUAL_COMPRESSION_JOBS_LOCK:
        _manual_compression_cleanup_locked()
        job = _MANUAL_COMPRESSION_JOBS.get(sid)
        if not job:
            return j(handler, {"ok": True, "status": "idle", "session_id": sid})
        payload = _manual_compression_status_payload(job)
        # Stage-344 Opus SHOULD-FIX (#2128): do not pop the job on first
        # read of a `done` payload. The session may be open in multiple
        # tabs, and the first tab's poll would otherwise leave the second
        # tab with `idle` and a "Compression job is no longer available"
        # toast. Let the 10-minute TTL handle eviction so all open tabs
        # see the same terminal payload.
        return j(handler, payload)


def _handle_session_compress(handler, body):
    def _anchor_message_key(m):
        if not isinstance(m, dict):
            return None
        role = str(m.get("role") or "")
        if not role or role == "tool":
            return None
        content = m.get("content", "")
        if isinstance(content, list):
            text = "\n".join(
                str(p.get("text") or p.get("content") or "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        else:
            text = str(content or "")
        norm = " ".join(text.split()).strip()[:160]
        ts = m.get("_ts") or m.get("timestamp")
        attachments = m.get("attachments")
        attach_count = len(attachments) if isinstance(attachments, list) else 0
        if not norm and not attach_count and not ts:
            return None
        return {"role": role, "ts": ts, "text": norm, "attachments": attach_count}

    def _compression_summary_from_messages(messages):
        text = None
        for m in reversed(messages or []):
            if not isinstance(m, dict):
                continue
            role = str(m.get("role") or "").lower()
            if role != "assistant":
                continue
            if not isinstance(m.get("content"), str):
                continue
            content = str(m.get("content") or "").strip()
            if not content:
                continue
            norm = re.sub(r"\s+", " ", content).strip()
            if (
                "context compaction" in norm.lower()
                or "context compression" in norm.lower()
            ):
                return norm
        return None

    def _compact_summary_text(raw_text):
        if not isinstance(raw_text, str):
            return None
        txt = raw_text.strip()
        if not txt:
            return None
        return re.sub(r"\s+", " ", txt)

    try:
        require(body, "session_id")
    except ValueError as e:
        return bad(handler, str(e))

    sid = str(body.get("session_id") or "").strip()
    if not sid:
        return bad(handler, "session_id is required")
    if _session_is_subagent_view_only(sid):
        return bad(handler, "Subagent sessions are view-only and cannot be compressed from WebUI", 400)

    # Cap focus_topic to 500 chars — matches the defensive input-size pattern
    # used elsewhere (session title :80, first-exchange snippets :500) and
    # prevents a user from forwarding an unbounded string into the compressor
    # prompt path. No privilege boundary here (user prompting themself), just
    # cheap bound-checking.
    focus_topic = str(body.get("focus_topic") or body.get("topic") or "").strip()[:500] or None

    try:
        s = get_session(sid)
    except KeyError:
        return bad(handler, "Session not found", 404)

    if getattr(s, "active_stream_id", None):
        return bad(handler, "Session is still streaming; wait for the current turn to finish.", 409)

    try:
        from api.streaming import _sanitize_messages_for_api

        messages = _sanitize_messages_for_api(s.messages)
        if len(messages) < 4:
            return bad(handler, "Not enough conversation to compress (need at least 4 messages).")

        def _fallback_estimate_messages_tokens_rough(msgs):
            """Fallback heuristic token estimate when runtime metadata helpers are absent.

            Uses whitespace token-like word counting only. This intentionally
            over/under-estimates BPE token counts (roughly around x3/x4 scale),
            and is only for resilient fallback behavior.
            """
            total = 0
            for m in msgs or []:
                if not isinstance(m, dict):
                    continue
                content = m.get("content", "")
                if isinstance(content, list):
                    content_text = "\n".join(
                        str(p.get("text") or p.get("content") or "")
                        for p in content
                        if isinstance(p, dict)
                    )
                else:
                    content_text = str(content or "")
                total += len(content_text.split())
            return max(1, total)

        def _fallback_summarize_manual_compression(original_messages, compressed_messages, before_tokens, after_tokens, focus_topic=None):
            """Lightweight fallback summary to keep /session/compress usable in tests/runtime."""
            after_tokens = after_tokens if after_tokens is not None else _fallback_estimate_messages_tokens_rough(compressed_messages)
            headline = f"Compressed: {len(original_messages)} \u2192 {len(compressed_messages)} messages"
            summary = {
                "headline": headline,
                "token_line": f"Rough transcript estimate: ~{before_tokens} \u2192 ~{after_tokens} tokens",
                "note": f"Focus: {focus_topic}" if focus_topic else None,
            }
            summary["reference_message"] = (
                f"[CONTEXT COMPACTION \u2014 REFERENCE ONLY] {headline}\n"
                f"{summary['token_line']}\n"
                + (summary["note"] + "\n" if summary.get("note") else "")
                + "Compression completed."
            )
            return summary

        def _estimate_messages_tokens_rough(msgs):
            try:
                from agent.model_metadata import estimate_messages_tokens_rough

                return estimate_messages_tokens_rough(msgs)
            except Exception:
                return _fallback_estimate_messages_tokens_rough(msgs)

        def _summarize_manual_compression(
            original_messages,
            compressed_messages,
            before_tokens,
            after_tokens,
            focus_topic=None,
        ):
            try:
                from agent.manual_compression_feedback import summarize_manual_compression

                return summarize_manual_compression(
                    original_messages,
                    compressed_messages,
                    before_tokens,
                    after_tokens,
                )
            except Exception:
                return _fallback_summarize_manual_compression(
                    original_messages,
                    compressed_messages,
                    before_tokens,
                    after_tokens,
                    focus_topic,
                )

        ensure_agent_runtime_current()
        import api.config as _cfg
        from api.oauth import resolve_runtime_provider_with_anthropic_env_lock
        import hermes_cli.runtime_provider as _runtime_provider
        AIAgent = require_ai_agent_class()

        resolved_model, resolved_provider, resolved_base_url = _cfg.resolve_model_provider(
            _cfg.model_with_provider_context(s.model, getattr(s, "model_provider", None))
        )

        resolved_api_key = None
        try:
            _rt = resolve_runtime_provider_with_anthropic_env_lock(
                _runtime_provider.resolve_runtime_provider,
                requested=resolved_provider,
            )
            resolved_api_key = _rt.get("api_key")
            if not resolved_provider:
                resolved_provider = _rt.get("provider")
            if not resolved_base_url:
                resolved_base_url = _rt.get("base_url")
        except Exception as _e:
            logger.warning("resolve_runtime_provider failed for compression: %s", _e)

        if isinstance(resolved_provider, str) and resolved_provider.startswith("custom:"):
            _cp_key, _cp_base = _cfg.resolve_custom_provider_connection(resolved_provider)
            if not resolved_api_key and _cp_key:
                resolved_api_key = _cp_key
            if not resolved_base_url and _cp_base:
                resolved_base_url = _cp_base

        if not resolved_api_key:
            return bad(handler, "No provider configured -- cannot compress.")

        # Compute compression *outside* the lock — the LLM round-trip can take
        # many seconds and we must not block cancel_stream or other writers.
        # Lock contract: hold for the in-memory mutation only, never across
        # network I/O.
        original_messages = list(messages)
        original_stream_state = (
            getattr(s, "active_stream_id", None),
            getattr(s, "pending_user_message", None),
            copy.deepcopy(getattr(s, "pending_attachments", None)),
            getattr(s, "pending_started_at", None),
        )
        approx_tokens = _estimate_messages_tokens_rough(original_messages)

        agent = AIAgent(
            model=resolved_model,
            provider=resolved_provider,
            base_url=resolved_base_url,
            api_key=resolved_api_key,
            # Identify browser-originated sessions as WebUI so Hermes Agent
            # does not inject CLI-specific terminal/output guidance.
            platform="webui",
            quiet_mode=True,
            enabled_toolsets=_resolve_cli_toolsets(),
            session_id=sid,
        )
        compressed = agent.context_compressor.compress(
            original_messages,
            current_tokens=approx_tokens,
            focus_topic=focus_topic,
        )
        new_tokens = _estimate_messages_tokens_rough(compressed)
        summary = _summarize_manual_compression(
            original_messages,
            compressed,
            approx_tokens,
            new_tokens,
            focus_topic=focus_topic,
        )

        with _cfg._get_session_agent_lock(sid):
            # Re-read messages to detect concurrent edits during the LLM call.
            # If the history changed, the compression result is stale — abort.
            current_stream_state = (
                getattr(s, "active_stream_id", None),
                getattr(s, "pending_user_message", None),
                copy.deepcopy(getattr(s, "pending_attachments", None)),
                getattr(s, "pending_started_at", None),
            )
            if current_stream_state != original_stream_state:
                return bad(handler, "Session stream state changed during compression; please retry.", 409)
            if _sanitize_messages_for_api(s.messages) != original_messages:
                return bad(handler, "Session was modified during compression; please retry.", 409)

            from api.session_ops import _truncation_watermark_for
            from api.streaming import _stamp_missing_message_timestamps

            compressed_copy = copy.deepcopy(compressed)
            _stamp_missing_message_timestamps(compressed_copy)
            s.context_messages = compressed_copy
            s.active_stream_id = None
            s.pending_user_message = None
            s.pending_attachments = []
            s.pending_started_at = None
            s.pending_user_source = None
            visible_after = visible_messages_for_anchor(s.messages, auto_compression=False)
            s.compression_anchor_visible_idx = max(0, len(visible_after) - 1) if visible_after else None
            s.compression_anchor_message_key = _anchor_message_key(visible_after[-1]) if visible_after else None
            summary_text = None
            if isinstance(summary, dict):
                summary_text = summary.get("reference_message") or summary.get("token_line") or summary.get("headline")
            s.compression_anchor_summary = _compact_summary_text(
                summary_text or _compression_summary_from_messages(compressed) or ""
            )
            # Persist an intentional-shrink boundary so append-only state.db
            # reconciliation does not replay pre-compression rows (#4836).
            compress_watermark = _truncation_watermark_for(compressed_copy)
            s.truncation_watermark = compress_watermark
            s.truncation_boundary = compress_watermark
            s.compression_anchor_mode = "manual"
            s.last_prompt_tokens = new_tokens
            s.save()
            # Drop stale backups that would undo an intentional manual compress.
            try:
                s.path.with_suffix(".json.bak").unlink(missing_ok=True)
            except OSError:
                pass

        session_payload = redact_session_data(
            s.compact() | {
                "messages": s.messages,
                "tool_calls": s.tool_calls,
                "active_stream_id": s.active_stream_id,
                "pending_user_message": s.pending_user_message,
                "pending_attachments": s.pending_attachments,
                "pending_started_at": s.pending_started_at,
                "compression_anchor_visible_idx": getattr(s, "compression_anchor_visible_idx", None),
                "compression_anchor_message_key": getattr(s, "compression_anchor_message_key", None),
            }
        )
        return j(
            handler,
            {
                "ok": True,
                "session": session_payload,
                "summary": summary,
                "focus_topic": focus_topic,
            },
        )
    except AgentRuntimeChangedError as e:
        return j(handler, {
            "error": str(e),
            "type": "agent_runtime_stale",
            "retryable": True,
        }, status=409)
    except Exception as e:
        logger.warning("Manual session compression failed: %s", e)
        return bad(handler, f"Compression failed: {_sanitize_error(e)}")

__routes_exports__ = (
    "_MANUAL_COMPRESSION_JOBS",
    "_MANUAL_COMPRESSION_JOBS_LOCK",
    "_MANUAL_COMPRESSION_JOB_TTL_SECONDS",
    "_ManualCompressionMemoryHandler",
    "_manual_compression_cleanup_locked",
    "_manual_compression_status_payload",
    "_run_manual_compression_job",
    "_handle_session_compress_start",
    "_handle_session_compress_status",
    "_handle_session_compress",
)
