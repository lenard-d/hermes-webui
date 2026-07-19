"""Common process-local startup and teardown for browser turn workers."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from api.config import (
    finish_runtime_run,
    initialize_runtime_execution,
    note_runtime_last_event_id,
    register_active_run,
    runtime_transport,
)
from api.run_event_sink import RunEventSink
from api.run_journal import RunJournalWriter

logger = logging.getLogger(__name__)


class TurnExecution:
    """Own the common process-local resources for one worker generation."""

    def __init__(
        self,
        *,
        stream_id: str,
        session_id: str,
        transport: Any,
        journal: Any | None,
        cancel_event: threading.Event,
        event_sink: RunEventSink,
    ) -> None:
        self.stream_id = stream_id
        self.session_id = session_id
        self.transport = transport
        self.journal = journal
        self.cancel_event = cancel_event
        self.event_sink = event_sink
        self._finished = False

    @classmethod
    def start(
        cls,
        *,
        stream_id: str,
        session_id: str,
        phase: str,
        logger: logging.Logger | None = None,
        log_label: str = "run",
        **worker_metadata: Any,
    ) -> TurnExecution | None:
        """Claim all common worker resources or release the generation.

        ``None`` means teardown won before execution buffers could be attached.
        Unexpected setup errors are re-raised only after the complete runtime
        owner has been compensated.
        """
        stream_id = str(stream_id or "").strip()
        session_id = str(session_id or "").strip()
        phase = str(phase or "").strip()
        if not stream_id:
            raise ValueError("stream_id is required")
        if not session_id:
            raise ValueError("session_id is required")
        if not phase:
            raise ValueError("phase is required")
        active_logger = logger or globals()["logger"]

        transport = runtime_transport(stream_id)
        if transport is None:
            finish_runtime_run(stream_id)
            return None

        metadata = dict(worker_metadata)
        metadata.update(
            session_id=session_id,
            started_at=time.time(),
            phase=phase,
        )
        try:
            register_active_run(stream_id, **metadata)
            try:
                journal = RunJournalWriter(session_id, stream_id)
            except Exception:
                journal = None
                active_logger.debug(
                    "Failed to initialize %s journal for stream %s",
                    log_label,
                    stream_id,
                    exc_info=True,
                )

            cancel_event = threading.Event()
            if not initialize_runtime_execution(stream_id, cancel_event):
                finish_runtime_run(stream_id)
                return None
            event_sink = RunEventSink(
                stream_id=stream_id,
                transport=transport,
                journal=journal,
                record_runtime_cursor=note_runtime_last_event_id,
                logger=active_logger,
                log_label=log_label,
            )
        except Exception:
            finish_runtime_run(stream_id)
            raise

        return cls(
            stream_id=stream_id,
            session_id=session_id,
            transport=transport,
            journal=journal,
            cancel_event=cancel_event,
            event_sink=event_sink,
        )

    def finish(self) -> bool:
        """Release the complete runtime owner exactly once."""
        if self._finished:
            return False
        removed = bool(finish_runtime_run(self.stream_id))
        self._finished = True
        return removed
