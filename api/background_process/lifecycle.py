"""Daemon lifecycle for completion draining and session-channel cleanup."""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Optional

from api.sessions import collect_expired_session_channels
from api.session_state import (
    BG_TASK_COMPLETE_EVENTS_SEEN,
    BG_TASK_COMPLETE_EVENTS_SEEN_LOCK,
    PENDING_BG_TASK_COMPLETIONS,
)

from .completion_events import EMIT_COALESCE_LOCK, LAST_EMIT_TS
from .process_coordination import process_one, recover_processes_for_webui


logger = logging.getLogger("api.background_process")

_DRAIN_THREAD: Optional[threading.Thread] = None
_DRAIN_STOP = threading.Event()

_REAPER_THREAD: Optional[threading.Thread] = None
_REAPER_STOP = threading.Event()
_REAPER_INTERVAL_SECS = 60.0

# The drain and reaper are one process-lifecycle concern. Serializing both
# check-and-start transitions prevents an unreferenced loser thread under
# concurrent startup without coupling either loop to session-channel locks.
_THREAD_LIFECYCLE_LOCK = threading.Lock()


def _reaper_loop() -> None:
    logger.info("SessionChannel reaper thread started")
    while not _REAPER_STOP.is_set():
        try:
            collected = collect_expired_session_channels(time.time())
            if collected:
                with EMIT_COALESCE_LOCK:
                    for session_id in collected:
                        LAST_EMIT_TS.pop(session_id, None)
                logger.debug("SessionChannel reaper collected: %s", collected)

            # Completion dedup state outlives a channel in the headless case.
            # Once delivery is no longer pending the registry's consumed marker
            # is the durable idempotency owner, so the process-local set can go.
            with BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
                delivered = [
                    session_id
                    for session_id in BG_TASK_COMPLETE_EVENTS_SEEN
                    if session_id not in PENDING_BG_TASK_COMPLETIONS
                ]
                for session_id in delivered:
                    BG_TASK_COMPLETE_EVENTS_SEEN.pop(session_id, None)
        except Exception:
            logger.warning("SessionChannel reaper iteration failed", exc_info=True)
        if _REAPER_STOP.wait(_REAPER_INTERVAL_SECS):
            break


def start_session_channel_reaper() -> bool:
    """Start the channel reaper once; return whether this call started it."""
    global _REAPER_THREAD
    with _THREAD_LIFECYCLE_LOCK:
        if _REAPER_THREAD is not None and _REAPER_THREAD.is_alive():
            return False
        _REAPER_STOP.clear()
        _REAPER_THREAD = threading.Thread(
            target=_reaper_loop,
            name="hermes-webui-session-channel-reaper",
            daemon=True,
        )
        _REAPER_THREAD.start()
        return True


def stop_session_channel_reaper(timeout: float = 2.0) -> None:
    _REAPER_STOP.set()
    thread = _REAPER_THREAD
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)


def _drain_loop() -> None:
    try:
        from tools.process_registry import process_registry
    except Exception as exc:
        logger.warning("bg_task_complete drain unavailable: %s", exc)
        return

    logger.info("bg_task_complete drain thread started")
    while not _DRAIN_STOP.is_set():
        completion_queue = getattr(process_registry, "completion_queue", None)
        if completion_queue is None:
            _DRAIN_STOP.wait(1.0)
            continue
        try:
            event = completion_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        except Exception:
            logger.warning("bg_task_complete drain queue read failed", exc_info=True)
            _DRAIN_STOP.wait(1.0)
            continue
        if not isinstance(event, dict):
            continue
        try:
            process_one(event, stop_event=_DRAIN_STOP)
        except Exception:
            logger.warning("bg_task_complete event handling failed", exc_info=True)


def start_drain_thread() -> bool:
    """Start the completion drain once; return whether this call started it."""
    global _DRAIN_THREAD
    with _THREAD_LIFECYCLE_LOCK:
        if _DRAIN_THREAD is not None and _DRAIN_THREAD.is_alive():
            return False
        try:
            recover_processes_for_webui()
        except Exception:
            # Recovery is best-effort. A corrupt checkpoint must not disable
            # notifications for processes spawned after startup.
            logger.warning("background process recovery failed", exc_info=True)
        _DRAIN_STOP.clear()
        _DRAIN_THREAD = threading.Thread(
            target=_drain_loop,
            name="hermes-webui-bg-task-complete-drain",
            daemon=True,
        )
        _DRAIN_THREAD.start()
        return True


def stop_drain_thread(timeout: float = 2.0) -> None:
    _DRAIN_STOP.set()
    thread = _DRAIN_THREAD
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
