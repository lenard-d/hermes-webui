"""Exclusive orchestration and commit ordering for checkout updates."""

from __future__ import annotations

from . import restart, transaction_state
from .force_apply import apply_forced_checkout
from .lock_recovery import inspect_lock
from .planning import prepare_update
from .policy import DEFAULT_UPDATE_CHANNEL, _read_update_channel
from .working_tree import (
    CheckoutResult,
    apply_fast_forward,
)


def _preflight(target: str) -> dict | None:
    snapshot = restart.restart_blocker_snapshot()
    if snapshot.get("restart_blocked"):
        return restart.restart_blocked_response(target, snapshot)
    return None


def _finalize(result: CheckoutResult) -> dict:
    """Commit cache, gateway, and process-restart effects in strict order."""
    response = dict(result.response)
    if not result.finalize:
        return response

    transaction_state.invalidate_status_cache()
    target = str(response.get("target") or "")
    if target == "agent":
        gateway_ok, gateway_result = restart.ensure_gateway_restart_for_agent_update()
        if not gateway_ok:
            return {
                "ok": False,
                "message": restart.gateway_restart_failure_message(
                    target,
                    gateway_result,
                ),
                "target": target,
                "gateway_restart": gateway_result.get("status"),
            }
        response["gateway_restart"] = gateway_result.get("status")

    restart.schedule_restart()
    response["restart_scheduled"] = True
    return response


def _apply_update_inner(
    target: str,
    channel: str = DEFAULT_UPDATE_CHANNEL,
) -> dict:
    """Plan and apply a normal update while the caller owns the apply lock."""
    planned = prepare_update(target, channel, lock_aware_fetch=True)
    if planned.response is not None:
        return dict(planned.response)
    assert planned.plan is not None
    return _finalize(apply_fast_forward(planned.plan))


def apply_update(target: str, channel: str | None = None) -> dict:
    """Apply one fast-forward transaction without interleaving checkout writes."""
    blocked = _preflight(target)
    if blocked is not None:
        return blocked
    lock = transaction_state.apply_lock()
    if not lock.acquire(blocking=False):
        return {"ok": False, "message": "Update already in progress"}
    try:
        selected_channel = _read_update_channel() if channel is None else channel
        return _apply_update_inner(target, selected_channel)
    finally:
        lock.release()


def apply_force_update(target: str, channel: str | None = None) -> dict:
    """Apply a confirmed destructive reset with the same transaction gates."""
    blocked = _preflight(target)
    if blocked is not None:
        return blocked
    lock = transaction_state.apply_lock()
    if not lock.acquire(blocking=False):
        return {"ok": False, "message": "Update already in progress"}
    try:
        selected_channel = _read_update_channel() if channel is None else channel
        planned = prepare_update(
            target,
            selected_channel,
            lock_aware_fetch=False,
        )
        if planned.response is not None:
            return dict(planned.response)
        assert planned.plan is not None
        return _finalize(apply_forced_checkout(planned.plan))
    finally:
        lock.release()


def apply_clear_lock(target: str) -> dict:
    """Inspect a lock and retry the normal transaction only after it is absent."""
    blocked = _preflight(target)
    if blocked is not None:
        return blocked
    lock = transaction_state.apply_lock()
    if not lock.acquire(blocking=False):
        return {"ok": False, "message": "Update already in progress"}
    try:
        channel = _read_update_channel()
        recovery = inspect_lock(target, channel)
        if recovery.response is not None:
            return dict(recovery.response)
        if not recovery.retry:
            return {"ok": False, "message": "Lock recovery could not be verified"}
        result = _apply_update_inner(target, channel)
        result = dict(result)
        result["lock_recovery"] = dict(recovery.annotation or {})
        return result
    finally:
        lock.release()


__all__ = ["apply_clear_lock", "apply_force_update", "apply_update"]
