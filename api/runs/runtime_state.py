"""Process-local ownership for browser-turn runtime state.

The current execution backend is in-process, so its live values remain Python
objects. This module owns the transitions across those values: publishing a
stream, registering worker liveness, and releasing every per-run value on
teardown. Storage mappings are injected temporarily so existing import aliases
remain compatible while callers migrate to this interface.
"""

from __future__ import annotations

import sys
import time
from collections.abc import MutableMapping
from dataclasses import dataclass
from threading import Lock
from typing import Any, Callable


@dataclass(frozen=True)
class RunProgressSnapshot:
    """Immutable copy of progress that may need terminal persistence."""

    partial_text: str
    reasoning_text: str
    live_tool_calls: tuple
    last_event_id: str | None


@dataclass(frozen=True)
class RunCancellationSnapshot(RunProgressSnapshot):
    """Immutable process-local state captured before a run is interrupted."""

    stream_id: str
    session_id: str | None
    channel: Any
    cancel_event: Any
    agent: Any
    had_transport: bool
    had_worker: bool


class ProcessRuntimeState:
    """Own process-local stream and worker lifecycle transitions."""

    def __init__(
        self,
        *,
        streams: MutableMapping[str, Any],
        stream_owners: MutableMapping[str, str],
        cancel_flags: MutableMapping[str, Any],
        agent_instances: MutableMapping[str, Any],
        partial_text: MutableMapping[str, str],
        reasoning_text: MutableMapping[str, str],
        live_tool_calls: MutableMapping[str, list],
        goal_related: MutableMapping[str, bool],
        last_event_ids: MutableMapping[str, str],
        active_runs: MutableMapping[str, dict],
        streams_lock: Lock,
        owners_lock: Lock,
        active_runs_lock: Lock,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._streams = streams
        self._stream_owners = stream_owners
        self._cancel_flags = cancel_flags
        self._agent_instances = agent_instances
        self._partial_text = partial_text
        self._reasoning_text = reasoning_text
        self._live_tool_calls = live_tool_calls
        self._goal_related = goal_related
        self._last_event_ids = last_event_ids
        self._active_runs = active_runs
        self._streams_lock = streams_lock
        self._owners_lock = owners_lock
        self._active_runs_lock = active_runs_lock
        self._clock = clock
        self._last_run_finished_at: float | None = None

    @staticmethod
    def _required_id(value: str, field: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError(f"{field} is required")
        return cleaned

    @property
    def last_run_finished_at(self) -> float | None:
        return self._last_run_finished_at

    def register_stream(
        self,
        stream_id: str,
        session_id: str,
        channel: Any,
        *,
        goal_related: bool = False,
    ) -> None:
        """Publish one stream and its authorization owner as one transition."""
        stream_id = self._required_id(stream_id, "stream_id")
        session_id = self._required_id(session_id, "session_id")
        if channel is None:
            raise ValueError("channel is required")

        with self._owners_lock:
            with self._streams_lock:
                if stream_id in self._streams:
                    raise ValueError(f"stream already registered: {stream_id}")
                # An owner without a transport is stale pre-worker state from a
                # failed start. Replacing it is safe; an attached transport is
                # the value that makes a stream id live.
                self._stream_owners[stream_id] = session_id
                self._streams[stream_id] = channel
                if goal_related:
                    self._goal_related[stream_id] = True

    def has_stream(self, stream_id: str) -> bool:
        """Return whether the process still exposes a transport for a run."""
        stream_id = str(stream_id or "").strip()
        if not stream_id:
            return False
        with self._streams_lock:
            return stream_id in self._streams

    def has_worker(self, stream_id: str) -> bool:
        """Return whether a worker still owns execution for a run."""
        stream_id = str(stream_id or "").strip()
        if not stream_id:
            return False
        with self._active_runs_lock:
            return stream_id in self._active_runs

    def transport(self, stream_id: str) -> Any | None:
        """Return the current transport handle without exposing its registry."""
        stream_id = str(stream_id or "").strip()
        if not stream_id:
            return None
        with self._streams_lock:
            return self._streams.get(stream_id)

    def transport_items(self) -> tuple[tuple[str, Any], ...]:
        """Return a stable snapshot of published transport handles."""
        with self._streams_lock:
            return tuple(self._streams.items())

    def transport_count(self, *, timeout: float | None = None) -> int | None:
        """Count transports, returning ``None`` when a bounded lock wait expires."""
        if timeout is None:
            with self._streams_lock:
                return len(self._streams)
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        if not self._streams_lock.acquire(timeout=timeout):
            return None
        try:
            return len(self._streams)
        finally:
            self._streams_lock.release()

    def worker_items(self) -> tuple[tuple[str, dict[str, Any]], ...]:
        """Return copied worker metadata that callers cannot mutate in place."""
        with self._active_runs_lock:
            return tuple(
                (str(stream_id), dict(metadata or {}))
                for stream_id, metadata in self._active_runs.items()
            )

    def active_run_ids(self) -> frozenset[str]:
        """Return every run with a live transport or worker."""
        with self._streams_lock:
            active_ids = set(self._streams)
        with self._active_runs_lock:
            active_ids.update(str(stream_id) for stream_id in self._active_runs)
        return frozenset(active_ids)

    def run_session_id(self, stream_id: str) -> str | None:
        """Resolve a run's session from worker metadata, then stream ownership."""
        stream_id = str(stream_id or "").strip()
        if not stream_id:
            return None
        with self._active_runs_lock:
            worker = dict(self._active_runs.get(stream_id) or {})
        session_id = str(worker.get("session_id") or "").strip()
        if session_id:
            return session_id
        return self.owner_session_id(stream_id)

    def last_event_id(self, stream_id: str) -> str | None:
        """Return the most recent durable event cursor for a live run."""
        return self.progress_snapshot(stream_id).last_event_id

    def note_last_event_id(self, stream_id: str, event_id: str) -> None:
        """Record the latest durable event cursor without exposing its registry."""
        stream_id = self._required_id(stream_id, "stream_id")
        event_id = self._required_id(event_id, "event_id")
        with self._streams_lock:
            self._last_event_ids[stream_id] = event_id

    def initialize_execution(self, stream_id: str, cancel_event: Any) -> bool:
        """Attach execution buffers only while the admitted transport is live.

        Returning ``False`` means teardown already won. Producers must not
        recreate per-run state after that point. Re-initializing a live run is
        a caller error because it would discard progress needed for recovery.
        """
        stream_id = self._required_id(stream_id, "stream_id")
        if cancel_event is None:
            raise ValueError("cancel_event is required")
        with self._streams_lock:
            if stream_id not in self._streams:
                return False
            # A few compatibility callers still pre-seed one or more empty
            # legacy buffer aliases before invoking a worker. Accept only that
            # zero-progress shape while migration is in flight; an existing
            # cancel owner or any real progress means this is a destructive
            # second initialization and must fail closed.
            partial = self._partial_text.get(stream_id, "")
            reasoning = self._reasoning_text.get(stream_id, "")
            tool_calls = self._live_tool_calls.get(stream_id, [])
            if (
                stream_id in self._cancel_flags
                or bool(partial)
                or bool(reasoning)
                or bool(tool_calls)
            ):
                raise ValueError(f"execution already initialized: {stream_id}")
            self._cancel_flags[stream_id] = cancel_event
            self._partial_text[stream_id] = ""
            self._reasoning_text[stream_id] = ""
            self._live_tool_calls[stream_id] = []
        return True

    def append_partial_text(self, stream_id: str, text: Any) -> bool:
        """Append visible output without reviving a released run."""
        stream_id = self._required_id(stream_id, "stream_id")
        with self._streams_lock:
            if stream_id not in self._partial_text:
                return False
            self._partial_text[stream_id] += str(text)
        return True

    def replace_partial_text(self, stream_id: str, text: Any) -> bool:
        """Replace visible output without reviving a released run."""
        stream_id = self._required_id(stream_id, "stream_id")
        with self._streams_lock:
            if stream_id not in self._partial_text:
                return False
            self._partial_text[stream_id] = str(text)
        return True

    def append_reasoning_text(self, stream_id: str, text: Any) -> bool:
        """Append reasoning output without reviving a released run."""
        stream_id = self._required_id(stream_id, "stream_id")
        with self._streams_lock:
            if stream_id not in self._reasoning_text:
                return False
            self._reasoning_text[stream_id] += str(text)
        return True

    def replace_reasoning_text(self, stream_id: str, text: Any) -> bool:
        """Replace reasoning output without reviving a released run."""
        stream_id = self._required_id(stream_id, "stream_id")
        with self._streams_lock:
            if stream_id not in self._reasoning_text:
                return False
            self._reasoning_text[stream_id] = str(text)
        return True

    def start_tool_call(
        self,
        stream_id: str,
        *,
        name: Any,
        args: Any,
        tool_call_id: Any = None,
    ) -> bool:
        """Append one recoverable tool-call record to a live execution."""
        stream_id = self._required_id(stream_id, "stream_id")
        stable_id = str(tool_call_id or "").strip()
        if not stable_id and not str(name or "").strip():
            return False
        call = {
            "name": name,
            "args": dict(args) if isinstance(args, dict) else {},
            "done": False,
        }
        if stable_id:
            call["tid"] = stable_id
        with self._streams_lock:
            calls = self._live_tool_calls.get(stream_id)
            if calls is None:
                return False
            calls.append(call)
        return True

    def finish_tool_call(
        self,
        stream_id: str,
        *,
        name: Any = None,
        tool_call_id: Any = None,
        **metadata: Any,
    ) -> bool:
        """Complete the latest matching unfinished tool call.

        A stable tool id is authoritative. Name fallback is allowed only for a
        legacy start record that had no id; a conflicting id must never settle
        a sibling call that happens to share the same tool name.
        """
        stream_id = self._required_id(stream_id, "stream_id")
        stable_id = str(tool_call_id or "").strip()
        if not stable_id and not str(name or "").strip():
            return False
        safe_metadata = dict(metadata)
        for identity_key in ("name", "args", "done", "tid"):
            safe_metadata.pop(identity_key, None)
        with self._streams_lock:
            calls = self._live_tool_calls.get(stream_id)
            if calls is None:
                return False
            for call in reversed(calls):
                if not isinstance(call, dict) or call.get("done"):
                    continue
                candidate_id = str(call.get("tid") or "").strip()
                if stable_id:
                    matches = candidate_id == stable_id or (
                        not candidate_id
                        and bool(name)
                        and call.get("name") == name
                    )
                else:
                    matches = not name or call.get("name") == name
                if not matches:
                    continue
                call.update(safe_metadata)
                call["done"] = True
                return True
        return False

    def attach_agent(self, stream_id: str, agent: Any) -> bool:
        """Expose an interruptible agent only while its execution is live."""
        stream_id = self._required_id(stream_id, "stream_id")
        if agent is None:
            raise ValueError("agent is required")
        with self._streams_lock:
            cancel_event = self._cancel_flags.get(stream_id)
            if (
                stream_id not in self._streams
                or cancel_event is None
                or cancel_event.is_set()
            ):
                return False
            self._agent_instances[stream_id] = agent
        return True

    def blocking_stream_for_session(
        self,
        session_id: str,
        *,
        active_stream_id: str | None = None,
        pending_user_message: str | None = None,
        pending_started_at: float | None = None,
        pending_grace_seconds: float = 30.0,
        worker_unwind_seconds: float = 180.0,
    ) -> str | None:
        """Return the run that must finish before a session can start again.

        The transport registry, worker registry, and short publication gap are
        one admission decision. Dead workers older than the bounded unwind
        window are reconciled here so callers cannot disagree about liveness.
        A worker that still has a transport is retained even after the window:
        it is genuinely live, but its old post-cancel row no longer blocks a
        successor solely because of age.
        """
        session_id = str(session_id or "").strip()
        if not session_id:
            return None
        active_stream_id = str(active_stream_id or "").strip() or None
        now = self._clock()

        with self._streams_lock:
            live_stream_ids = set(self._streams)
        if active_stream_id in live_stream_ids:
            return active_stream_id

        stale_worker_ids: list[str] = []
        blocking_worker_id: str | None = None
        with self._active_runs_lock:
            for worker_key, raw in list(self._active_runs.items()):
                entry = raw or {}
                stream_id = str(entry.get("stream_id") or worker_key or "").strip()
                owner_session_id = str(entry.get("session_id") or "").strip()
                if owner_session_id != session_id or not stream_id:
                    continue
                try:
                    started_at = float(entry.get("started_at") or 0)
                except (TypeError, ValueError):
                    started_at = 0.0
                expired = bool(
                    started_at
                    and worker_unwind_seconds >= 0
                    and now - started_at > worker_unwind_seconds
                )
                if expired:
                    if worker_key not in live_stream_ids and stream_id not in live_stream_ids:
                        stale_worker_ids.append(worker_key)
                    continue
                blocking_worker_id = stream_id
                break
            for worker_key in stale_worker_ids:
                self._active_runs.pop(worker_key, None)

        for worker_key in stale_worker_ids:
            self.unregister_owner(worker_key)
        if blocking_worker_id:
            return blocking_worker_id

        if active_stream_id and pending_user_message:
            try:
                pending_started = float(pending_started_at or 0)
            except (TypeError, ValueError):
                pending_started = 0.0
            if pending_started and now - pending_started < pending_grace_seconds:
                return active_stream_id
        return None

    def register_owner(self, stream_id: str, session_id: str) -> None:
        stream_id = str(stream_id or "").strip()
        session_id = str(session_id or "").strip()
        if not stream_id or not session_id:
            return
        with self._owners_lock:
            self._stream_owners[stream_id] = session_id

    def owner_session_id(self, stream_id: str) -> str | None:
        stream_id = str(stream_id or "").strip()
        if not stream_id:
            return None
        with self._owners_lock:
            owner = self._stream_owners.get(stream_id)
        owner = str(owner or "").strip()
        return owner or None

    def unregister_owner(self, stream_id: str) -> None:
        stream_id = str(stream_id or "").strip()
        if not stream_id:
            return
        with self._owners_lock:
            self._stream_owners.pop(stream_id, None)

    def register_worker(self, stream_id: str, **metadata: Any) -> None:
        stream_id = str(stream_id or "").strip()
        if not stream_id:
            return
        now = self._clock()
        entry = dict(metadata or {})
        entry.setdefault("stream_id", stream_id)
        entry.setdefault("started_at", now)
        entry.setdefault("phase", "running")
        with self._active_runs_lock:
            self._active_runs[stream_id] = entry

    def update_worker(self, stream_id: str, **metadata: Any) -> None:
        stream_id = str(stream_id or "").strip()
        if not stream_id:
            return
        with self._active_runs_lock:
            entry = self._active_runs.get(stream_id)
            if entry is not None:
                entry.update(metadata)

    def unregister_worker(self, stream_id: str) -> bool:
        stream_id = str(stream_id or "").strip()
        if not stream_id:
            return False
        with self._active_runs_lock:
            removed = self._active_runs.pop(stream_id, None) is not None
            self._last_run_finished_at = self._clock()
        self.unregister_owner(stream_id)
        return removed

    def _progress_snapshot_unlocked(self, stream_id: str) -> RunProgressSnapshot:
        raw_tool_calls = self._live_tool_calls.get(stream_id, []) or []
        return RunProgressSnapshot(
            partial_text=str(self._partial_text.get(stream_id, "") or ""),
            reasoning_text=str(self._reasoning_text.get(stream_id, "") or ""),
            live_tool_calls=tuple(
                dict(item) if isinstance(item, dict) else item
                for item in raw_tool_calls
            ),
            last_event_id=(
                str(self._last_event_ids.get(stream_id) or "").strip() or None
            ),
        )

    def progress_snapshot(self, stream_id: str) -> RunProgressSnapshot:
        """Copy live progress without consuming worker-owned buffers."""
        stream_id = self._required_id(stream_id, "stream_id")
        with self._streams_lock:
            return self._progress_snapshot_unlocked(stream_id)

    def begin_cancel(self, stream_id: str) -> RunCancellationSnapshot | None:
        """Claim cancellation and eagerly release transport admission state.

        Progress buffers deliberately remain owned by the worker until terminal
        cleanup. Their immutable snapshots let persistence finish after an agent
        interrupt races with worker teardown.
        """
        stream_id = str(stream_id or "").strip()
        if not stream_id:
            return None

        with self._streams_lock:
            had_transport = stream_id in self._streams
            channel = self._streams.get(stream_id)
            cancel_event = self._cancel_flags.get(stream_id)
            agent = self._agent_instances.get(stream_id)
            progress = self._progress_snapshot_unlocked(stream_id)
            if had_transport:
                self._streams.pop(stream_id, None)
                self._cancel_flags.pop(stream_id, None)
                self._agent_instances.pop(stream_id, None)

        worker_entry: dict[str, Any] = {}
        with self._active_runs_lock:
            raw_worker_entry = self._active_runs.get(stream_id)
            if raw_worker_entry is not None:
                worker_entry = dict(raw_worker_entry or {})
                raw_worker_entry["phase"] = "cancelling"
        had_worker = bool(raw_worker_entry is not None)

        if not had_transport and not had_worker:
            return None
        if cancel_event is not None:
            cancel_event.set()

        session_id = str(worker_entry.get("session_id") or "").strip() or None
        if session_id is None:
            session_id = self.owner_session_id(stream_id)
        if session_id is None and agent is not None:
            session_id = str(getattr(agent, "session_id", "") or "").strip() or None

        return RunCancellationSnapshot(
            stream_id=stream_id,
            session_id=session_id,
            channel=channel,
            cancel_event=cancel_event,
            agent=agent,
            partial_text=progress.partial_text,
            reasoning_text=progress.reasoning_text,
            live_tool_calls=progress.live_tool_calls,
            last_event_id=progress.last_event_id,
            had_transport=had_transport,
            had_worker=had_worker,
        )

    def finish_run(self, stream_id: str) -> bool:
        """Release all process-local values owned by one completed run."""
        stream_id = str(stream_id or "").strip()
        if not stream_id:
            return False

        removed = False
        with self._streams_lock:
            for mapping in (
                self._streams,
                self._cancel_flags,
                self._agent_instances,
                self._partial_text,
                self._reasoning_text,
                self._live_tool_calls,
                self._goal_related,
                self._last_event_ids,
            ):
                if mapping.pop(stream_id, None) is not None:
                    removed = True

        with self._active_runs_lock:
            if self._active_runs.pop(stream_id, None) is not None:
                removed = True
            if removed:
                self._last_run_finished_at = self._clock()

        with self._owners_lock:
            if self._stream_owners.pop(stream_id, None) is not None:
                removed = True
        return removed


# This module is the sole owner of process-local run registries.  ``api.config``
# exposes aliases for compatibility, but never creates a second mapping or
# lifecycle object.
STREAMS: dict[str, Any] = {}
STREAMS_LOCK = Lock()
STREAM_SESSION_OWNERS: dict[str, str] = {}
STREAM_SESSION_OWNERS_LOCK = Lock()
CANCEL_FLAGS: dict[str, Any] = {}
AGENT_INSTANCES: dict[str, Any] = {}
STREAM_PARTIAL_TEXT: dict[str, str] = {}
STREAM_REASONING_TEXT: dict[str, str] = {}
STREAM_LIVE_TOOL_CALLS: dict[str, list] = {}
STREAM_GOAL_RELATED: dict[str, bool] = {}
STREAM_LAST_EVENT_ID: dict[str, str] = {}
ACTIVE_RUNS: dict[str, dict] = {}
ACTIVE_RUNS_LOCK = Lock()

RUNTIME_STATE = ProcessRuntimeState(
    streams=STREAMS,
    stream_owners=STREAM_SESSION_OWNERS,
    cancel_flags=CANCEL_FLAGS,
    agent_instances=AGENT_INSTANCES,
    partial_text=STREAM_PARTIAL_TEXT,
    reasoning_text=STREAM_REASONING_TEXT,
    live_tool_calls=STREAM_LIVE_TOOL_CALLS,
    goal_related=STREAM_GOAL_RELATED,
    last_event_ids=STREAM_LAST_EVENT_ID,
    active_runs=ACTIVE_RUNS,
    streams_lock=STREAMS_LOCK,
    owners_lock=STREAM_SESSION_OWNERS_LOCK,
    active_runs_lock=ACTIVE_RUNS_LOCK,
)


def _sync_legacy_finished_at() -> None:
    """Keep the scalar compatibility alias current without sharing ownership."""
    config_module = sys.modules.get("api.config")
    if config_module is not None:
        config_module.LAST_RUN_FINISHED_AT = RUNTIME_STATE.last_run_finished_at


def register_stream_owner(stream_id: str, session_id: str) -> None:
    RUNTIME_STATE.register_owner(stream_id, session_id)


def stream_owner_session_id(stream_id: str) -> str | None:
    return RUNTIME_STATE.owner_session_id(stream_id)


def unregister_stream_owner(stream_id: str) -> None:
    RUNTIME_STATE.unregister_owner(stream_id)


def register_active_run(stream_id: str, **metadata: Any) -> None:
    RUNTIME_STATE.register_worker(stream_id, **metadata)


def update_active_run(stream_id: str, **metadata: Any) -> None:
    RUNTIME_STATE.update_worker(stream_id, **metadata)


def unregister_active_run(stream_id: str) -> None:
    RUNTIME_STATE.unregister_worker(stream_id)
    _sync_legacy_finished_at()


def register_runtime_stream(
    stream_id: str,
    session_id: str,
    channel: Any,
    *,
    goal_related: bool = False,
) -> None:
    RUNTIME_STATE.register_stream(
        stream_id,
        session_id,
        channel,
        goal_related=goal_related,
    )


def blocking_runtime_stream(
    session_id: str,
    *,
    active_stream_id: str | None = None,
    pending_user_message: str | None = None,
    pending_started_at: float | None = None,
    pending_grace_seconds: float = 30.0,
    worker_unwind_seconds: float = 180.0,
) -> str | None:
    return RUNTIME_STATE.blocking_stream_for_session(
        session_id,
        active_stream_id=active_stream_id,
        pending_user_message=pending_user_message,
        pending_started_at=pending_started_at,
        pending_grace_seconds=pending_grace_seconds,
        worker_unwind_seconds=worker_unwind_seconds,
    )


def runtime_stream_alive(stream_id: str) -> bool:
    return RUNTIME_STATE.has_stream(stream_id)


def runtime_worker_alive(stream_id: str) -> bool:
    return RUNTIME_STATE.has_worker(stream_id)


def runtime_transport(stream_id: str) -> Any | None:
    return RUNTIME_STATE.transport(stream_id)


def runtime_transport_items() -> tuple[tuple[str, Any], ...]:
    return RUNTIME_STATE.transport_items()


def runtime_transport_count(*, timeout: float | None = None) -> int | None:
    return RUNTIME_STATE.transport_count(timeout=timeout)


def runtime_worker_items() -> tuple[tuple[str, dict], ...]:
    return RUNTIME_STATE.worker_items()


def runtime_last_run_finished_at() -> float | None:
    return RUNTIME_STATE.last_run_finished_at


def runtime_active_run_ids() -> set[str]:
    return RUNTIME_STATE.active_run_ids()


def runtime_run_session_id(stream_id: str) -> str | None:
    return RUNTIME_STATE.run_session_id(stream_id)


def runtime_last_event_id(stream_id: str) -> str | None:
    return RUNTIME_STATE.last_event_id(stream_id)


def note_runtime_last_event_id(stream_id: str, event_id: str) -> None:
    RUNTIME_STATE.note_last_event_id(stream_id, event_id)


def initialize_runtime_execution(stream_id: str, cancel_event: Any) -> bool:
    return RUNTIME_STATE.initialize_execution(stream_id, cancel_event)


def append_runtime_partial_text(stream_id: str, text: Any) -> bool:
    return RUNTIME_STATE.append_partial_text(stream_id, text)


def replace_runtime_partial_text(stream_id: str, text: Any) -> bool:
    return RUNTIME_STATE.replace_partial_text(stream_id, text)


def append_runtime_reasoning_text(stream_id: str, text: Any) -> bool:
    return RUNTIME_STATE.append_reasoning_text(stream_id, text)


def replace_runtime_reasoning_text(stream_id: str, text: Any) -> bool:
    return RUNTIME_STATE.replace_reasoning_text(stream_id, text)


def start_runtime_tool_call(
    stream_id: str,
    *,
    name: Any,
    args: Any,
    tool_call_id: Any = None,
) -> bool:
    return RUNTIME_STATE.start_tool_call(
        stream_id,
        name=name,
        args=args,
        tool_call_id=tool_call_id,
    )


def finish_runtime_tool_call(
    stream_id: str,
    *,
    name: Any = None,
    tool_call_id: Any = None,
    **metadata: Any,
) -> bool:
    return RUNTIME_STATE.finish_tool_call(
        stream_id,
        name=name,
        tool_call_id=tool_call_id,
        **metadata,
    )


def attach_runtime_agent(stream_id: str, agent: Any) -> bool:
    return RUNTIME_STATE.attach_agent(stream_id, agent)


def runtime_progress_snapshot(stream_id: str) -> RunProgressSnapshot:
    return RUNTIME_STATE.progress_snapshot(stream_id)


def begin_runtime_cancel(stream_id: str) -> RunCancellationSnapshot | None:
    return RUNTIME_STATE.begin_cancel(stream_id)


def finish_runtime_run(stream_id: str) -> bool:
    removed = RUNTIME_STATE.finish_run(stream_id)
    _sync_legacy_finished_at()
    return removed
