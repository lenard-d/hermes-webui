"""Remote fetch and immutable plan selection for update transactions."""

from __future__ import annotations

from dataclasses import dataclass

from . import policy, repository
from .transaction_state import UpdateTarget, resolve_target


@dataclass(frozen=True)
class UpdatePlan:
    """The authoritative checkout/ref pair used by the apply phase."""

    target: UpdateTarget
    compare_ref: str


@dataclass(frozen=True)
class PlanResult:
    """Exactly one of a runnable plan or a terminal response."""

    plan: UpdatePlan | None = None
    response: dict | None = None


def prepare_update(
    target: str,
    channel: str | None,
    *,
    lock_aware_fetch: bool,
) -> PlanResult:
    """Fetch once and bind all later decisions to one resolved target/ref."""
    resolved = resolve_target(target, channel)
    if isinstance(resolved, dict):
        return PlanResult(response=resolved)

    fetch_out, fetch_ok = repository._run_git(
        ["fetch", "origin", "--quiet", "--tags", "--force"],
        resolved.path,
        timeout=15,
    )
    if not fetch_ok:
        if lock_aware_fetch and repository._is_git_lock_error(fetch_out):
            return PlanResult(
                response={
                    "ok": False,
                    "message": (
                        "Fetch failed due to a repository lock: "
                        f"{fetch_out.strip()}"
                    ),
                    "lock_conflict": True,
                }
            )
        fallback = (
            "Could not reach the remote repository. Check your internet "
            "connection and try again."
            if lock_aware_fetch
            else "Could not reach the remote repository. Check your connection."
        )
        return PlanResult(
            response={
                "ok": False,
                "message": repository._apply_fetch_failure_message(fetch_out, fallback),
            }
        )

    compare_ref = policy._select_apply_compare_ref(
        resolved.path,
        resolved.channel,
        resolved.name,
    )
    if compare_ref is None:
        return PlanResult(
            response={
                "ok": True,
                "message": (
                    f"{resolved.name} is already up to date on the "
                    f"{resolved.channel} channel."
                ),
                "target": resolved.name,
                "up_to_date": True,
                "channel": resolved.channel,
            }
        )
    return PlanResult(plan=UpdatePlan(resolved, compare_ref))


__all__ = ["PlanResult", "UpdatePlan", "prepare_update"]
