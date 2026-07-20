"""Fail-closed inspection for operator-driven Git lock recovery."""

from __future__ import annotations

from dataclasses import dataclass

from . import repository
from .transaction_state import resolve_target


@dataclass(frozen=True)
class LockRecoveryResult:
    """A terminal response or a safe request to retry the normal transaction."""

    response: dict | None = None
    retry: bool = False
    annotation: dict | None = None


def inspect_lock(target: str, channel: str | None) -> LockRecoveryResult:
    """Inspect but never delete Git locks; retry only after proven absence."""
    resolved = resolve_target(target, channel)
    if isinstance(resolved, dict):
        return LockRecoveryResult(response=resolved)

    inventory = repository._inventory_locks(resolved.path)
    manual_command = f"rm -f {inventory['well_known_lock_path']}"
    annotation = {
        "action": "no-lock-found",
        "manual_command": manual_command,
        "other_locks": inventory["other_locks"],
    }
    if not inventory["well_known_lock_present"]:
        return LockRecoveryResult(retry=True, annotation=annotation)

    message = (
        "A git lock file (.git/index.lock) is present. The server does not "
        "delete locks automatically -- git uses O_CREAT|O_EXCL locking, "
        "which cannot be detected with advisory probes. To recover: confirm "
        "no other git process is running against this checkout, then run: "
        f'{manual_command}  Click "Retry update" once you have removed it.'
    )
    return LockRecoveryResult(
        response={
            "ok": False,
            "message": message,
            "lock_held": True,
            "target": target,
            "manual_command": manual_command,
            "well_known_lock_path": inventory["well_known_lock_path"],
            "other_locks": inventory["other_locks"],
        }
    )


__all__ = ["LockRecoveryResult", "inspect_lock"]
