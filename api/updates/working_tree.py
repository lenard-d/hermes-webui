"""Fast-forward checkout application and local-change rollback ownership."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import repository
from .planning import UpdatePlan


@dataclass(frozen=True)
class CheckoutResult:
    """Result of applying a plan before process-level finalization."""

    response: dict
    finalize: bool = False


def restore_stash_after_pull_failure(path: Path, pull_out: str) -> str:
    """Restore an autostash after a lock failure without dropping user work."""
    _, pop_ok = repository._run_git(["stash", "pop"], path)
    if pop_ok:
        return "Local modifications were restored from the temporary stash."

    _, apply_ok = repository._run_git(["stash", "apply"], path)
    if apply_ok:
        repository._run_git(["stash", "drop"], path)
        return "Local modifications were restored from the temporary stash."

    detail = (pull_out or "").strip()[:200]
    return (
        "Your local modifications could not be restored automatically "
        f'(stash pop failed after pull error: {detail or "no detail"}). '
        "They remain safely in `git stash list`; run `git -C "
        + str(path)
        + " stash pop` once the lock is cleared."
    )


def _restore_stash_after_failed_pull(
    plan: UpdatePlan,
    pull_out: str,
    *,
    stashed: bool,
) -> CheckoutResult:
    target = plan.target.name
    path = plan.target.path
    if repository._is_git_lock_error(pull_out):
        note = restore_stash_after_pull_failure(path, pull_out) if stashed else ""
        message = f"Pull failed due to a repository lock: {pull_out.strip()}"
        if note:
            message = f"{message} {note}"
        return CheckoutResult(
            {"ok": False, "message": message, "lock_conflict": True}
        )

    pull_lower = pull_out.lower()
    detail = pull_out.strip()[:300] if pull_out.strip() else "(no output from git)"
    untracked_collision = (
        "untracked working tree files would be overwritten" in pull_lower
    )
    diverged = "not possible to fast-forward" in pull_lower or "diverged" in pull_lower
    restored_stash = False
    stash_drop_failed = False
    if stashed:
        _, apply_ok = repository._run_git(["stash", "apply"], path)
        if apply_ok:
            _, drop_ok = repository._run_git(["stash", "drop"], path)
            restored_stash = True
            stash_drop_failed = not drop_ok
        else:
            _, reset_ok = repository._run_git(["reset", "--hard", "HEAD"], path)
            if not reset_ok:
                response = {
                    "ok": False,
                    "message": (
                        "Pull failed, and failed to clean up a stash-apply "
                        "conflict while restoring local changes. Manual "
                        "intervention needed: run git -C "
                        + str(path)
                        + " reset --hard HEAD to remove conflict markers. Your "
                        "changes remain in the git stash. Pull error: "
                        + detail
                    ),
                    "stash_conflict": True,
                }
                if diverged:
                    response["diverged"] = True
                return CheckoutResult(response)
            response = {
                "ok": False,
                "message": (
                    f"Pull failed, and your local {target} modifications "
                    "conflicted while restoring from stash. The index and "
                    "tracked files were restored to HEAD, and your changes "
                    "remain in the git stash. To inspect: git -C "
                    + str(path)
                    + " stash show -p. To re-apply: git -C "
                    + str(path)
                    + " stash apply, then resolve conflicts. Pull error: "
                    + detail
                ),
                "stash_conflict": True,
            }
            if diverged:
                response["diverged"] = True
            return CheckoutResult(response)

    restored_notes = []
    if restored_stash:
        restored_notes.append(
            f"Local {target} modifications were restored to the working tree; "
            "save or stash them before running destructive recovery commands."
        )
        if stash_drop_failed:
            restored_notes.append(
                "The temporary stash entry may still be present because "
                "git stash drop failed."
            )
    restored_note = " ".join(restored_notes)

    if diverged:
        parts = [
            f"The local {target} repo has commits that are not on the remote "
            "branch, so a fast-forward update is not possible."
        ]
        if restored_note:
            parts.append(restored_note)
        parts.append(
            "Run: git -C "
            + str(path)
            + " fetch origin && git -C "
            + str(path)
            + " reset --hard "
            + plan.compare_ref
        )
        return CheckoutResult(
            {"ok": False, "message": " ".join(parts), "diverged": True}
        )
    if "does not track" in pull_lower or "no tracking information" in pull_lower:
        parts = [
            f"The local {target} branch has no upstream tracking branch configured."
        ]
        if restored_note:
            parts.append(restored_note)
        parts.append(
            "Run: git -C "
            + str(path)
            + " branch --set-upstream-to="
            + plan.compare_ref
        )
        return CheckoutResult({"ok": False, "message": " ".join(parts)})

    parts = [f"Pull failed: {detail}"]
    if restored_note:
        parts.append(restored_note)
    response = {"ok": False, "message": " ".join(parts)}
    if untracked_collision:
        response["conflict"] = True
    return CheckoutResult(response)


def apply_fast_forward(plan: UpdatePlan) -> CheckoutResult:
    """Apply one prepared ref while preserving or safely stashing local edits."""
    target = plan.target.name
    path = plan.target.path
    status_out, status_ok = repository._run_git(
        ["status", "--porcelain", "--untracked-files=no"], path
    )
    if not status_ok:
        if repository._is_git_lock_error(status_out):
            return CheckoutResult(
                {
                    "ok": False,
                    "message": (
                        "Failed to inspect repo status due to a repository lock: "
                        f"{status_out.strip()}"
                    ),
                    "lock_conflict": True,
                }
            )
        return CheckoutResult(
            {
                "ok": False,
                "message": f"Failed to inspect repo status: {status_out[:200]}",
            }
        )

    conflict_codes = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}
    if any(line[:2] in conflict_codes for line in status_out.splitlines()):
        return CheckoutResult(
            {
                "ok": False,
                "message": (
                    f"The local {target} repo has unresolved merge conflicts. "
                    "To reset to the latest remote version run: git -C "
                    + str(path)
                    + " checkout . && git -C "
                    + str(path)
                    + " pull --ff-only"
                ),
                "conflict": True,
            }
        )

    stashed = bool(status_out)
    if stashed:
        _, stash_ok = repository._run_git(
            ["stash", "push", "-m", "hermes-update-autostash"], path
        )
        if not stash_ok:
            return CheckoutResult(
                {"ok": False, "message": "Failed to stash local changes"}
            )

    remote, branch = repository._split_remote_ref(plan.compare_ref)
    pull_args = ["pull", "--ff-only"]
    pull_args.extend([remote, branch] if remote else ["origin", plan.compare_ref])
    pull_out, pull_ok = repository._run_git(pull_args, path, timeout=30)
    if not pull_ok:
        return _restore_stash_after_failed_pull(plan, pull_out, stashed=stashed)

    stash_drop_failed = False
    if stashed:
        _, apply_ok = repository._run_git(["stash", "apply"], path)
        if apply_ok:
            _, drop_ok = repository._run_git(["stash", "drop"], path)
            stash_drop_failed = not drop_ok
        else:
            _, reset_ok = repository._run_git(["reset", "--hard", "HEAD"], path)
            if not reset_ok:
                return CheckoutResult(
                    {
                        "ok": False,
                        "message": (
                            "Updated successfully, but failed to clean up a "
                            "stash-apply conflict. Manual intervention needed: "
                            "run git -C "
                            + str(path)
                            + " reset --hard HEAD to remove conflict markers. "
                            "Your changes remain in the git stash."
                        ),
                        "stash_conflict": True,
                    }
                )
            return CheckoutResult(
                {
                    "ok": True,
                    "message": (
                        f"{target} updated to the latest version. Your local "
                        "modifications conflicted with upstream changes and were "
                        "set aside in a git stash. To inspect: git -C "
                        + str(path)
                        + " stash show -p. To re-apply: git -C "
                        + str(path)
                        + " stash apply, then resolve conflicts. Drop the stash "
                        "after you are satisfied."
                    ),
                    "target": target,
                    "stash_conflict": True,
                },
                finalize=True,
            )

    message = f"{target} updated successfully"
    if stash_drop_failed:
        message += (
            ". Local modifications were restored, but the temporary stash "
            "entry may still be present because git stash drop failed."
        )
    return CheckoutResult(
        {"ok": True, "message": message, "target": target},
        finalize=True,
    )


__all__ = ["CheckoutResult", "apply_fast_forward", "restore_stash_after_pull_failure"]
