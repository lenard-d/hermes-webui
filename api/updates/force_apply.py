"""Explicit destructive checkout replacement for confirmed update recovery."""

from __future__ import annotations

import logging

from . import policy, repository
from .planning import UpdatePlan
from .working_tree import CheckoutResult

logger = logging.getLogger(__name__)


def apply_forced_checkout(plan: UpdatePlan) -> CheckoutResult:
    """Reset to a prepared ref, refusing channel rewinds and preserving ignores."""
    target = plan.target.name
    path = plan.target.path
    channel = plan.target.channel
    compare_ref = plan.compare_ref

    if policy._head_contains_ref(path, compare_ref) and not policy._can_fast_forward_to(
        path, compare_ref
    ):
        return CheckoutResult(
            {
                "ok": False,
                "message": (
                    f"{target} is already ahead of the {channel} channel "
                    f"({compare_ref}); refusing to rewind the checkout. "
                    "Switching to a slower channel keeps your current version "
                    "until that channel catches up."
                ),
                "target": target,
                "channel": channel,
                "refused_rewind": True,
            }
        )

    repository._run_git(["checkout", "."], path)
    clean_out, clean_ok = repository._run_git(["clean", "-fd"], path)
    if not clean_ok:
        logger.warning(
            "force_apply_update: `git clean -fd` failed (non-fatal, "
            "continuing to reset --hard): %s",
            clean_out,
        )
    _, reset_ok = repository._run_git(["reset", "--hard", compare_ref], path)
    if not reset_ok:
        return CheckoutResult(
            {"ok": False, "message": f"Force reset to {compare_ref} failed"}
        )
    return CheckoutResult(
        {
            "ok": True,
            "message": f"{target} force-updated to {compare_ref}",
            "target": target,
        },
        finalize=True,
    )


__all__ = ["apply_forced_checkout"]
