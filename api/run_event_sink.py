"""Journal-backed publication of browser-run SSE events."""

from __future__ import annotations

import logging
from typing import Any, Callable


class RunEventSink:
    """Publish one run's events through its durable and live projections.

    Publication order is journal, runtime cursor, transport cursor, then live
    frame. Every projection after the journal is best effort: a failed cursor
    side effect must not hide a durable frame from the current subscriber, and
    a failed journal must not suppress the legacy live-only behavior.
    """

    def __init__(
        self,
        *,
        stream_id: str,
        transport: Any,
        journal: Any | None,
        record_runtime_cursor: Callable[[str, str], Any],
        logger: logging.Logger | None = None,
        log_label: str = "run",
    ) -> None:
        self._stream_id = str(stream_id or "").strip()
        if not self._stream_id:
            raise ValueError("stream_id is required")
        if transport is None:
            raise ValueError("transport is required")
        if not callable(record_runtime_cursor):
            raise ValueError("record_runtime_cursor is required")
        self._transport = transport
        self._journal = journal
        self._record_runtime_cursor = record_runtime_cursor
        self._logger = logger or logging.getLogger(__name__)
        self._log_label = str(log_label or "run")

    @staticmethod
    def _event_id(result: Any) -> str | None:
        if not isinstance(result, dict):
            return None
        event_id = result.get("event_id")
        if not isinstance(event_id, str):
            return None
        return event_id.strip() or None

    def publish(self, event: str, data: Any) -> str | None:
        """Publish one event and return its durable event id when available."""
        event = str(event or "").strip()
        if not event:
            raise ValueError("event is required")

        event_id = None
        if self._journal is not None:
            try:
                journaled = self._journal.append_sse_event(event, data)
                event_id = self._event_id(journaled)
                if event_id:
                    try:
                        self._record_runtime_cursor(self._stream_id, event_id)
                    except Exception:
                        self._logger.debug(
                            "Failed to record %s runtime cursor %s for stream %s",
                            self._log_label,
                            event_id,
                            self._stream_id,
                            exc_info=True,
                        )
            except Exception:
                self._logger.debug(
                    "Failed to append %s journal event %s for stream %s",
                    self._log_label,
                    event,
                    self._stream_id,
                    exc_info=True,
                )

        if event_id and hasattr(self._transport, "note_last_event_id"):
            try:
                self._transport.note_last_event_id(event_id)
            except Exception:
                self._logger.debug(
                    "Failed to record %s transport cursor %s for stream %s",
                    self._log_label,
                    event_id,
                    self._stream_id,
                    exc_info=True,
                )

        try:
            queue_item = (
                (event, data, event_id)
                if event_id and hasattr(self._transport, "subscribe_with_snapshot")
                else (event, data)
            )
            self._transport.put_nowait(queue_item)
        except Exception:
            self._logger.debug(
                "Failed to publish %s live event %s for stream %s",
                self._log_label,
                event,
                self._stream_id,
                exc_info=True,
            )
        return event_id
