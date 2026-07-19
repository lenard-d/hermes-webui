"""Process-local ownership for browser-turn runtime state.

The current execution backend is in-process, so its live values remain Python
objects. This module owns the transitions across those values: publishing a
stream, registering worker liveness, and releasing every per-run value on
teardown. Storage mappings are injected temporarily so existing import aliases
remain compatible while callers migrate to this interface.
"""

from __future__ import annotations

import time
from collections.abc import MutableMapping
from threading import Lock
from typing import Any, Callable


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
