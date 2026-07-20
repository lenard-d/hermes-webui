"""Deferred process-wakeup persistence, claim, and turn-start ownership.

A completion that arrives while its session is active cannot start another
turn.  This module owns that full lifecycle: persist it exactly once, atomically
claim it when the session becomes idle, start one continuation outside locks,
and re-defer on an admission race.
"""

from __future__ import annotations

import logging
import threading


logger = logging.getLogger("api.background_process")


def record_deferred_wakeup(
    session_id: str,
    process_id: str,
    wakeup_prompt: str,
) -> bool:
    """Persist one wakeup idempotently for later redelivery."""
    if not session_id or not wakeup_prompt:
        return False
    from api import config as _cfg

    try:
        with _cfg.DEFERRED_PROCESS_WAKEUPS_LOCK:
            entries = _cfg.DEFERRED_PROCESS_WAKEUPS.setdefault(session_id, [])
            if process_id and any(
                entry.get("process_id") == process_id for entry in entries
            ):
                return True
            entries.append(
                {"process_id": process_id, "wakeup_prompt": wakeup_prompt}
            )
        return True
    except Exception:
        logger.debug(
            "record_deferred_wakeup failed for session %s",
            session_id,
            exc_info=True,
        )
        return False


def claim_deferred_wakeups(session_id: str) -> list[dict]:
    """Atomically remove and return every deferred wakeup for ``session_id``."""
    if not session_id:
        return []
    from api import config as _cfg

    try:
        with _cfg.DEFERRED_PROCESS_WAKEUPS_LOCK:
            return _cfg.DEFERRED_PROCESS_WAKEUPS.pop(session_id, []) or []
    except Exception:
        logger.debug(
            "claim_deferred_wakeups failed for session %s",
            session_id,
            exc_info=True,
        )
        return []


def session_has_active_turn(session_id: str) -> bool:
    """Return whether the runtime owner reports a live turn for the session."""
    from api import config as _cfg

    try:
        with _cfg.ACTIVE_RUNS_LOCK:
            for metadata in (_cfg.ACTIVE_RUNS or {}).values():
                if (
                    isinstance(metadata, dict)
                    and metadata.get("session_id") == session_id
                ):
                    return True
    except Exception:
        logger.debug("ACTIVE_RUNS active-turn check failed", exc_info=True)
    return False


def drain_for_session(session_id: str) -> int:
    """Redeliver one deferred wakeup after the session becomes truly idle.

    The pending list is peeked and claimed under its owner lock, then the lock
    is released before any turn is started. Multiple wakeups are serialized by
    re-recording entries 2..N before starting entry 1; each later turn teardown
    can therefore claim exactly one next continuation.
    """
    if not session_id:
        return 0
    from api import config as _cfg

    try:
        if session_has_active_turn(session_id):
            return 0
        with _cfg.DEFERRED_PROCESS_WAKEUPS_LOCK:
            if not _cfg.DEFERRED_PROCESS_WAKEUPS.get(session_id):
                return 0

        entries = claim_deferred_wakeups(session_id)
        if not entries:
            return 0
        try:
            _cfg.PENDING_BG_TASK_COMPLETIONS.discard(session_id)
        except Exception:
            logger.debug(
                "PENDING discard failed for session %s",
                session_id,
                exc_info=True,
            )

        deliverable = [
            entry
            for entry in entries
            if str((entry or {}).get("wakeup_prompt") or "").strip()
        ]
        if not deliverable:
            return 0

        first = deliverable[0]
        # Persist the tail before launch. If launch races a foreground turn or
        # fails after admission, no already-claimed continuation is lost.
        for entry in deliverable[1:]:
            record_deferred_wakeup(
                session_id,
                str((entry or {}).get("process_id") or ""),
                str((entry or {}).get("wakeup_prompt") or "").strip(),
            )
        start_server_side_turn(
            session_id,
            str((first or {}).get("wakeup_prompt") or "").strip(),
            process_id=str((first or {}).get("process_id") or ""),
        )
        logger.info(
            "turn-teardown idle-hook redelivered 1 deferred wakeup(s) for "
            "session %s",
            session_id,
        )
        return 1
    except Exception:
        logger.warning(
            "drain_deferred_wakeups_for_session failed for session %s",
            session_id,
            exc_info=True,
        )
        return 0


def start_server_side_turn(
    session_id: str,
    wakeup_prompt: str,
    *,
    process_id: str = "",
) -> None:
    """Start one wakeup turn asynchronously and re-defer admission races."""
    def _runner() -> None:
        try:
            from api.routes import start_session_turn

            response = start_session_turn(
                session_id,
                wakeup_prompt,
                source="process_wakeup",
            )
            status = int((response or {}).get("_status", 200) or 200)
            if (
                status == 409
                and (response or {}).get("error") == "process_wakeup_paused"
            ):
                logger.info(
                    "server-side wakeup suppressed for session %s: provider "
                    "credential state is paused",
                    session_id,
                )
            elif status == 409:
                if wakeup_prompt:
                    record_deferred_wakeup(
                        session_id,
                        process_id,
                        wakeup_prompt,
                    )
                logger.debug(
                    "server-side wakeup raced an active turn for session %s; "
                    "re-deferred for redelivery on next teardown/turn",
                    session_id,
                )
            elif status >= 400:
                logger.warning(
                    "server-side wakeup failed for session %s: status=%s err=%r",
                    session_id,
                    status,
                    (response or {}).get("error"),
                )
            else:
                logger.info(
                    "server-side wakeup turn started for session %s (stream_id=%s)",
                    session_id,
                    (response or {}).get("stream_id"),
                )
        except Exception:
            logger.warning(
                "server-side wakeup turn raised for session %s",
                session_id,
                exc_info=True,
            )

    threading.Thread(
        target=_runner,
        name=f"hermes-webui-process-wakeup-{str(session_id)[:8]}",
        daemon=True,
    ).start()
