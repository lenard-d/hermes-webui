"""Fetch, pull, and push operations for workspace Git remotes."""

from __future__ import annotations

from pathlib import Path

from .git import git_status
from .git_repository import (
    GIT_REMOTE_TIMEOUT,
    GitContext,
    GitWorkspaceError,
    _block_filtered_destructive_write,
    _git_mutation_lock,
    git_command_message,
    resolve_git_context,
    run_git as _run_git,
    workspace_git_destructive_enabled,
)


def _branch_name(ctx: GitContext) -> str:
    branch = _run_git(ctx, ["branch", "--show-current"], check=True).stdout.strip()
    if not branch:
        raise GitWorkspaceError("Cannot push from a detached HEAD")
    return branch


def git_fetch(workspace: str | Path) -> dict:
    ctx = resolve_git_context(workspace)
    if ctx is None:
        raise GitWorkspaceError("Workspace is not a Git repository", "not_a_repo")
    with _git_mutation_lock(ctx):
        result = _run_git(
            ctx,
            ["fetch", "--prune", "--no-recurse-submodules"],
            timeout=GIT_REMOTE_TIMEOUT,
            check=True,
            force_destructive_hardening=True,
            disable_filter_attributes=workspace_git_destructive_enabled(),
            neutralize_filter_programs=True,
            neutralize_remote_helpers=True,
        )
    return {
        "ok": True,
        "message": git_command_message(result),
        "status": git_status(workspace),
    }


def git_pull(workspace: str | Path) -> dict:
    ctx = resolve_git_context(workspace)
    if ctx is None:
        raise GitWorkspaceError("Workspace is not a Git repository", "not_a_repo")
    with _git_mutation_lock(ctx):
        _block_filtered_destructive_write(
            ctx,
            "Repository uses local Git filters; pull may corrupt working-tree content. Use the terminal to pull manually.",
        )
        result = _run_git(
            ctx,
            ["pull", "--ff-only", "--no-recurse-submodules"],
            timeout=GIT_REMOTE_TIMEOUT,
            check=True,
            destructive=True,
            disable_filter_attributes=True,
            neutralize_filter_programs=True,
            neutralize_remote_helpers=True,
        )
    return {
        "ok": True,
        "message": git_command_message(result),
        "status": git_status(workspace),
    }


def git_push(workspace: str | Path) -> dict:
    ctx = resolve_git_context(workspace)
    if ctx is None:
        raise GitWorkspaceError("Workspace is not a Git repository", "not_a_repo")
    with _git_mutation_lock(ctx):
        status = git_status(workspace)
        args = ["push"]
        if not status.get("upstream"):
            branch = _branch_name(ctx)
            remotes = _run_git(ctx, ["remote"], check=True).stdout.split()
            if "origin" not in remotes:
                raise GitWorkspaceError(
                    "No upstream branch or origin remote is configured", "no_upstream"
                )
            args.extend(["-u", "origin", branch])
        result = _run_git(
            ctx,
            args,
            timeout=GIT_REMOTE_TIMEOUT,
            check=True,
            destructive=True,
            disable_filter_attributes=True,
            neutralize_filter_programs=True,
            neutralize_remote_helpers=True,
        )
    return {
        "ok": True,
        "message": git_command_message(result),
        "status": git_status(workspace),
    }
