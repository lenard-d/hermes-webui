"""Translate Hermes Agent callbacks into ordered local-run events."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT
from api.metering import meter
from api.todo_state import emit_todo_state
from .payloads import _compact_for_echo_compare, _strip_compact_echo_suffix
from .runtime_resolution import (
    _is_agent_compression_start_status,
    _is_fallback_lifecycle_message,
)
from .tool_events import (
    _TOOL_ARG_CONTENT_CAP,
    _TOOL_ARG_CONTENT_KEYS,
    _tool_result_snippet,
)

from .runtime_state import (
    append_runtime_partial_text,
    append_runtime_reasoning_text,
    finish_runtime_tool_call,
    replace_runtime_reasoning_text,
    start_runtime_tool_call,
)

from .local_usage import LocalUsageTracker


logger = logging.getLogger(__name__)


class LocalEventTranslator:
    """Own callback-local reasoning, tool, and metering state for one run."""

    def __init__(
        self,
        *,
        session_id: str,
        stream_id: str,
        publish: Callable[[str, dict], None],
        usage: LocalUsageTracker,
        agent_params: Callable[[], set[str]],
    ) -> None:
        self.session_id = session_id
        self.stream_id = stream_id
        self.publish = publish
        self.usage = usage
        self.agent_params = agent_params
        self.token_sent = False
        self.captured_terminal_error = [None]
        self.reasoning_segments: dict[int, str] = {}
        self.current_reasoning_idx = 0
        self.tool_boundary_advanced = False
        self.live_tool_calls: list[dict] = []
        self.checkpoint_activity = [0]
        self._tool_start_ids: set[str] = set()
        self._tool_complete_ids: set[str] = set()
        self._reasoning_buffer = ""
        self._reasoning_last_publish = 0.0
        self._metering_last_publish = time.monotonic() - 1
        self._output_deltas = 0
        self._reasoning_deltas = 0

    def status(self, kind, message) -> None:
        """Publish user-visible lifecycle statuses and retain terminal cause."""
        text = str(message or "").strip()
        kind_text = str(kind or "").strip().lower()
        if not text:
            return
        lowered = text.lower()
        if (
            self.captured_terminal_error[0] is None
            and "non-retryable error" in lowered
            and "http" in lowered
        ):
            self.captured_terminal_error[0] = text
        if _is_agent_compression_start_status(kind_text, text):
            self.publish(
                "compressing",
                {"session_id": self.session_id, "message": "Compressing context"},
            )
        elif _is_fallback_lifecycle_message(kind_text, text):
            self.publish("warning", {"type": "fallback", "message": text})

    def flush_reasoning(self) -> None:
        if self._reasoning_buffer:
            self.publish("reasoning", {"text": self._reasoning_buffer})
            self._reasoning_buffer = ""

    def _emit_metering(self) -> None:
        now = time.monotonic()
        if now - self._metering_last_publish < 0.1:
            return
        self._metering_last_publish = now
        stats = meter().get_stats(self.stream_id)
        stats["session_id"] = self.session_id
        stats["usage"] = self.usage.snapshot()
        stats.setdefault("tps_available", False)
        stats.setdefault("estimated", False)
        self.publish("metering", stats)

    def _emit_tool_metering(self) -> None:
        stats = meter().get_stats(self.stream_id)
        stats["session_id"] = self.session_id
        stats["usage"] = self.usage.snapshot()
        self.publish("metering", stats)

    def _is_visible_output_echo(self, text: str) -> bool:
        candidate = _compact_for_echo_compare(text)
        if not candidate:
            return False
        visible_output = STREAM_PARTIAL_TEXT.get(self.stream_id, "")
        visible_tail = _compact_for_echo_compare(
            visible_output[-max(len(str(text)) * 2, 512) :]
        )
        if visible_tail and visible_tail.endswith(candidate):
            return True
        if len(candidate) < 80:
            return False
        visible_compact = _compact_for_echo_compare(visible_output)
        return bool(visible_compact and candidate in visible_compact)

    def _strip_reasoning_output_echo(self, text: str) -> bool:
        removed = False
        if self.stream_id in STREAM_REASONING_TEXT:
            next_text, did_remove = _strip_compact_echo_suffix(
                STREAM_REASONING_TEXT.get(self.stream_id, ""), text
            )
            if did_remove:
                replace_runtime_reasoning_text(self.stream_id, next_text)
                removed = True
        next_buffer, did_remove = _strip_compact_echo_suffix(
            self._reasoning_buffer, text
        )
        if did_remove:
            self._reasoning_buffer = next_buffer
            removed = True
        for idx in (self.current_reasoning_idx, self.current_reasoning_idx - 1):
            if idx not in self.reasoning_segments:
                continue
            next_segment, did_remove = _strip_compact_echo_suffix(
                self.reasoning_segments.get(idx, ""), text
            )
            if not did_remove:
                continue
            if next_segment:
                self.reasoning_segments[idx] = next_segment
            else:
                self.reasoning_segments.pop(idx, None)
            removed = True
            break
        return removed

    def token(self, text) -> None:
        if text is None:
            return
        self.flush_reasoning()
        self.token_sent = True
        append_runtime_partial_text(self.stream_id, text)
        self.publish("token", {"text": text})
        self._output_deltas += 1
        meter().record_token(self.stream_id, self._output_deltas)
        self._emit_metering()

    def reasoning(self, text) -> None:
        if text is None:
            self.flush_reasoning()
            return
        self.tool_boundary_advanced = False
        delta = str(text)
        if self._is_visible_output_echo(delta):
            return
        self.reasoning_segments[self.current_reasoning_idx] = (
            self.reasoning_segments.get(self.current_reasoning_idx, "") + delta
        )
        append_runtime_reasoning_text(self.stream_id, delta)
        self._reasoning_buffer += delta
        now = time.monotonic()
        if now - self._reasoning_last_publish >= 0.1:
            self._reasoning_last_publish = now
            self.flush_reasoning()
        self._reasoning_deltas += 1
        meter().record_reasoning(self.stream_id, self._reasoning_deltas)
        self._emit_metering()

    def interim_assistant(self, text, **callback_kwargs) -> None:
        self.current_reasoning_idx += 1
        if text is None:
            return
        visible = str(text).strip()
        if not visible:
            return
        reasoning_echo = self._strip_reasoning_output_echo(visible)
        payload = {
            "text": visible,
            "already_streamed": bool(callback_kwargs.get("already_streamed", False))
            or self._is_visible_output_echo(visible),
        }
        if reasoning_echo:
            payload["reasoning_echo"] = True
        self.publish("interim_assistant", payload)

    def _tool_args_snapshot(self, args) -> dict:
        snapshot = {}
        if isinstance(args, dict):
            for key, value in list(args.items())[:4]:
                rendered = str(value)
                cap = (
                    _TOOL_ARG_CONTENT_CAP
                    if str(key).lower() in _TOOL_ARG_CONTENT_KEYS
                    else 120
                )
                snapshot[key] = rendered[:cap] + ("..." if len(rendered) > cap else "")
        return snapshot

    def _record_reasoning_progress(self, event_type, name, preview) -> bool:
        if event_type not in ("reasoning.available", "_thinking"):
            return False
        value = preview if event_type == "reasoning.available" else name
        if not value:
            return True
        delta = str(value)
        if self._is_visible_output_echo(delta):
            return True
        self.reasoning_segments[self.current_reasoning_idx] = (
            self.reasoning_segments.get(self.current_reasoning_idx, "") + delta
        )
        append_runtime_reasoning_text(self.stream_id, delta)
        self.publish("reasoning", {"text": delta})
        self._reasoning_deltas += 1
        meter().record_reasoning(self.stream_id, self._reasoning_deltas)
        self._emit_metering()
        return True

    def _poll_legacy_approval(self) -> None:
        try:
            from api.route_approvals import (
                _gateway_queues,
                _lock,
                _pending,
                reconcile_gateway_pending_mirror_locked,
            )
            from tools.approval import has_blocking_approval

            if not has_blocking_approval(self.session_id):
                return
            pending = None
            with _lock:
                reconcile_gateway_pending_mirror_locked(self.session_id)
                queue = _pending.get(self.session_id)
                if isinstance(queue, list):
                    pending = dict(queue[0]) if queue else None
                elif queue:
                    pending = dict(queue)
                if pending is None:
                    gateway_queue = _gateway_queues.get(self.session_id) or []
                    if gateway_queue:
                        raw = getattr(gateway_queue[0], "data", None) or {}
                        if raw:
                            pending = dict(raw)
                        else:
                            logger.warning(
                                "Gateway queue entry for %s has no .data attribute",
                                self.session_id,
                            )
            if pending:
                self.publish("approval", pending)
        except ImportError:
            pass

    def tool(self, *callback_args, **callback_kwargs) -> None:
        self.flush_reasoning()
        event_type = name = preview = args = None
        if len(callback_args) >= 4:
            event_type, name, preview, args = callback_args[:4]
        elif len(callback_args) == 3:
            name, preview, args = callback_args
            event_type = "tool.started"
        elif len(callback_args) == 2:
            event_type, name = callback_args
        elif len(callback_args) == 1:
            name = callback_args[0]
            event_type = "tool.started"
        if self._record_reasoning_progress(event_type, name, preview):
            return
        if (
            not self.tool_boundary_advanced
            and self.current_reasoning_idx in self.reasoning_segments
        ):
            self.current_reasoning_idx += 1
            self.tool_boundary_advanced = True
        args_snapshot = self._tool_args_snapshot(args)
        params = self.agent_params()
        if event_type in (None, "tool.started") and "tool_start_callback" in params:
            return
        if event_type in (None, "tool.started"):
            self.live_tool_calls.append(
                {"name": name, "args": args if isinstance(args, dict) else {}}
            )
            start_runtime_tool_call(
                self.stream_id,
                name=name,
                args=args if isinstance(args, dict) else {},
            )
            self.publish(
                "tool",
                {
                    "event_type": event_type or "tool.started",
                    "name": name,
                    "preview": preview,
                    "args": args_snapshot,
                },
            )
            self._emit_tool_metering()
            self._poll_legacy_approval()
            return
        if event_type == "tool.completed" and "tool_complete_callback" in params:
            return
        if event_type == "tool.completed":
            for live_tool in reversed(self.live_tool_calls):
                if live_tool.get("done"):
                    continue
                if not name or live_tool.get("name") == name:
                    live_tool.update(
                        done=True,
                        duration=callback_kwargs.get("duration"),
                        is_error=bool(callback_kwargs.get("is_error", False)),
                    )
                    break
            finish_runtime_tool_call(
                self.stream_id,
                name=name,
                duration=callback_kwargs.get("duration"),
                is_error=bool(callback_kwargs.get("is_error", False)),
            )
            self.checkpoint_activity[0] += 1
            self.publish(
                "tool_complete",
                {
                    "event_type": event_type,
                    "name": name,
                    "preview": preview,
                    "args": args_snapshot,
                    "duration": callback_kwargs.get("duration"),
                    "is_error": bool(callback_kwargs.get("is_error", False)),
                },
            )
            emit_todo_state(
                self.publish,
                name=name,
                function_result=(
                    callback_kwargs.get("result")
                    if callback_kwargs.get("result") is not None
                    else preview
                ),
                session_id=self.session_id,
                stream_id=self.stream_id,
            )
            self._emit_tool_metering()

    def tool_start(self, tool_call_id, name, args) -> None:
        try:
            self.usage.record_tool_start(tool_call_id, name, args)
            if tool_call_id and tool_call_id not in self._tool_start_ids:
                self._tool_start_ids.add(tool_call_id)
                self.live_tool_calls.append(
                    {
                        "name": name,
                        "args": args if isinstance(args, dict) else {},
                        "tid": tool_call_id,
                    }
                )
                start_runtime_tool_call(
                    self.stream_id,
                    name=name,
                    args=args if isinstance(args, dict) else {},
                    tool_call_id=tool_call_id,
                )
                self.publish(
                    "tool",
                    {
                        "event_type": "tool.started",
                        "name": name,
                        "preview": None,
                        "args": self._tool_args_snapshot(args),
                        "tid": tool_call_id,
                    },
                )
            self._emit_tool_metering()
        except Exception:
            logger.debug(
                "Failed to update live prompt estimate on tool start", exc_info=True
            )

    def tool_complete(self, tool_call_id, name, args, function_result) -> None:
        try:
            self.usage.record_tool_complete(tool_call_id, name, function_result)
            if tool_call_id and tool_call_id not in self._tool_complete_ids:
                self._tool_complete_ids.add(tool_call_id)
                result_snippet = _tool_result_snippet(function_result)
                for live_tool in reversed(self.live_tool_calls):
                    if live_tool.get("done"):
                        continue
                    if live_tool.get("tid") == tool_call_id or (
                        not live_tool.get("tid") and live_tool.get("name") == name
                    ):
                        live_tool.update(done=True, snippet=result_snippet)
                        break
                finish_runtime_tool_call(
                    self.stream_id,
                    name=name,
                    tool_call_id=tool_call_id,
                    snippet=result_snippet,
                )
                self.checkpoint_activity[0] += 1
                self.publish(
                    "tool_complete",
                    {
                        "event_type": "tool.completed",
                        "name": name,
                        "preview": result_snippet,
                        "args": self._tool_args_snapshot(args),
                        "tid": tool_call_id,
                        "is_error": False,
                    },
                )
                emit_todo_state(
                    self.publish,
                    name=name,
                    function_result=function_result,
                    session_id=self.session_id,
                    stream_id=self.stream_id,
                )
            self._emit_tool_metering()
        except Exception:
            logger.debug(
                "Failed to update live prompt estimate on tool completion",
                exc_info=True,
            )
