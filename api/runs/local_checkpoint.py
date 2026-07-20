"""Durable pending-turn checkpoint lifecycle for a local agent run."""

from __future__ import annotations

import logging
import threading

from .turn_context import _save_streaming_checkpoint


class LocalCheckpoint:
    """Own the periodic checkpoint thread and its stop-before-recovery rule."""

    def __init__(self, *, session_id: str, logger: logging.Logger) -> None:
        self._session_id = session_id
        self._logger = logger
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def stop_event(self) -> threading.Event:
        return self._stop

    def start(self, session, *, session_lock, activity) -> None:
        """Persist the pending turn, then start activity-triggered checkpoints."""

        if self._thread is not None:
            raise RuntimeError("local checkpoint already started")
        with session_lock:
            session.save(touch_updated_at=True, skip_index=False)

        def periodic_checkpoint() -> None:
            last_saved_activity = 0
            while not self._stop.wait(15):
                try:
                    current = activity[0]
                    if current > last_saved_activity:
                        with session_lock:
                            _save_streaming_checkpoint(session)
                        last_saved_activity = current
                except Exception as exc:
                    self._logger.debug("Periodic checkpoint save failed: %s", exc)

        self._thread = threading.Thread(
            target=periodic_checkpoint,
            daemon=True,
            name=f"ckpt-{self._session_id[:8]}",
        )
        self._thread.start()

    def close(self) -> None:
        """Stop and join before any stale-pending recovery can acquire the lock."""

        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=15)
