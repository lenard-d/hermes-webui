"""Approval and clarify interaction lifecycle for one local run."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from .agent_loader import _clarify_timeout_seconds


_CLARIFY_FALLBACK = (
    "The user did not provide a response within the time limit. "
    "Use your best judgement to make the choice and proceed."
)


class LocalInteractionBridge:
    """Own callback registration, pending waits, and symmetric teardown."""

    def __init__(
        self,
        *,
        session_id: str,
        cancel_event,
        publish: Callable[[str, dict], None],
        logger: logging.Logger,
    ) -> None:
        self._session_id = session_id
        self._cancel_event = cancel_event
        self._publish = publish
        self._logger = logger
        self._approval_registered = False
        self._unregister_approval = None
        self._cleanup_approval_mirror = None
        self._clarify_registered = False
        self._unregister_clarify = None

    def open(self) -> None:
        self._open_approval_bridge()
        self._open_clarify_bridge()

    def clarify(self, question, choices) -> str:
        """Submit one blocking Hermes clarify request to the WebUI."""

        timeout = _clarify_timeout_seconds()
        data = {
            "question": str(question or ""),
            "choices_offered": [str(choice) for choice in (choices or [])],
            "session_id": self._session_id,
            "kind": "clarify",
            "requested_at": time.time(),
            "timeout_seconds": timeout,
        }
        try:
            from api.clarify import clear_pending, submit_pending
        except ImportError:
            return _CLARIFY_FALLBACK

        entry = submit_pending(self._session_id, data)
        deadline = time.monotonic() + timeout
        while True:
            if self._cancel_event.is_set():
                clear_pending(self._session_id)
                return _CLARIFY_FALLBACK
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                clear_pending(self._session_id)
                return _CLARIFY_FALLBACK
            if entry.event.wait(timeout=min(1.0, remaining)):
                return str(entry.result or "").strip() or _CLARIFY_FALLBACK

    def close(self) -> None:
        """Release every callback and pending approval projection we own."""

        if self._approval_registered and self._unregister_approval is not None:
            try:
                self._unregister_approval(self._session_id)
            except Exception:
                self._logger.debug("Failed to unregister approval callback")
        if self._cleanup_approval_mirror is not None:
            try:
                self._cleanup_approval_mirror()
            except Exception:
                self._logger.debug("Failed to reconcile gateway approval mirror")
        if self._clarify_registered and self._unregister_clarify is not None:
            try:
                self._unregister_clarify(self._session_id)
            except Exception:
                self._logger.debug("Failed to unregister clarify callback")

    def _open_approval_bridge(self) -> None:
        submit_pending = None
        try:
            try:
                from api.route_approvals import (
                    _approval_sse_notify_locked,
                    _lock as approval_lock,
                    reconcile_gateway_pending_mirror_locked,
                    submit_gateway_pending_mirror,
                )

                submit_pending = submit_gateway_pending_mirror

                def cleanup_mirror() -> None:
                    with approval_lock:
                        head, total, _changed = reconcile_gateway_pending_mirror_locked(
                            self._session_id
                        )
                        _approval_sse_notify_locked(self._session_id, head, total)

                self._cleanup_approval_mirror = cleanup_mirror
            except ImportError:
                pass

            from tools.approval import register_gateway_notify, unregister_gateway_notify

            def notify(approval_data) -> None:
                if submit_pending is not None:
                    try:
                        submit_pending(self._session_id, approval_data)
                    except Exception:
                        self._logger.warning(
                            "Failed to mirror approval into WebUI polling state",
                            exc_info=True,
                        )
                self._publish("approval", approval_data)

            register_gateway_notify(self._session_id, notify)
            self._approval_registered = True
            self._unregister_approval = unregister_gateway_notify
        except ImportError:
            self._logger.debug("Approval module not available, falling back to polling")

    def _open_clarify_bridge(self) -> None:
        try:
            from api.clarify import register_gateway_notify, unregister_gateway_notify

            register_gateway_notify(
                self._session_id,
                lambda data: self._publish("clarify", data),
            )
            self._clarify_registered = True
            self._unregister_clarify = unregister_gateway_notify
        except ImportError:
            self._logger.debug("Clarify module not available, falling back to polling")
