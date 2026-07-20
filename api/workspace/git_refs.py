"""Branch catalog and checkout lifecycle for workspace Git.

Branch validation, explicit dirty-worktree handling, WebUI-owned stashes, and
stash restoration stay together so one mutation lock covers every transition.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .git import git_status
from .git_repository import (
    GitContext,
    GitWorkspaceError,
    _clean_git_env,
    _git_mutation_lock,
    _has_repo_local_filters,
    git_command_message,
    resolve_git_context,
    run_git as _run_git,
    workspace_git_destructive_enabled,
)


_HERMES_BRANCH_SWITCH_STASH_PREFIX = "hermes-webui branch switch"


def _branch_ahead_behind(
    ctx: GitContext, branch: str, upstream: str
) -> tuple[int, int]:
    if not upstream:
        return 0, 0
    result = _run_git(
        ctx,
        ["rev-list", "--left-right", "--count", f"{branch}...{upstream}"],
        check=False,
    )
    if result.returncode != 0:
        return 0, 0
    parts = result.stdout.strip().split()
    if len(parts) != 2:
        return 0, 0
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return 0, 0


def _for_each_ref(ctx: GitContext, ref_prefix: str) -> list[dict]:
    fmt = (
        "%(refname)%00%(refname:short)%00%(upstream:short)%00%(objectname:short)%00"
        "%(committerdate:unix)%00%(committerdate:relative)%00%(authorname)%00%(subject)"
    )
    result = _run_git(ctx, ["for-each-ref", f"--format={fmt}", ref_prefix], check=True)
    refs = []
    for line in result.stdout.splitlines():
        full_name, name, upstream, sha, updated, updated_relative, author, subject = (
            line.split("\0") + ["", "", "", "", "", "", "", ""]
        )[:8]
        if not name or full_name.endswith("/HEAD") or name.endswith("/HEAD"):
            continue
        if ref_prefix == "refs/remotes" and "/" not in name:
            continue
        item = {
            "name": name,
            "sha": sha,
            "updated": int(updated) if str(updated).isdigit() else 0,
            "updated_relative": updated_relative,
            "author": author,
            "subject": subject,
        }
        if upstream:
            ahead, behind = _branch_ahead_behind(ctx, name, upstream)
            item.update({"upstream": upstream, "ahead": ahead, "behind": behind})
        else:
            item.update({"upstream": "", "ahead": 0, "behind": 0})
        refs.append(item)
    return sorted(refs, key=lambda item: item["name"].lower())


def git_branches(workspace: str | Path) -> dict:
    ctx = resolve_git_context(workspace)
    if ctx is None:
        raise GitWorkspaceError("Workspace is not a Git repository", "not_a_repo")
    head_name = _run_git(ctx, ["branch", "--show-current"], check=True).stdout.strip()
    detached = not bool(head_name)
    head_sha = _run_git(
        ctx, ["rev-parse", "--short", "HEAD"], check=True
    ).stdout.strip()
    status = git_status(workspace)
    local = _for_each_ref(ctx, "refs/heads")
    remote = _for_each_ref(ctx, "refs/remotes")
    return {
        "is_git": True,
        "current": head_name or head_sha or "HEAD",
        "detached": detached,
        "head": head_sha,
        "local": local,
        "remote": remote,
        "upstream": status.get("upstream", ""),
        "ahead": status.get("ahead", 0),
        "behind": status.get("behind", 0),
    }


def _validate_local_branch(ctx: GitContext, ref: str) -> str:
    ref = str(ref or "").strip()
    if not ref:
        raise GitWorkspaceError("Branch name is required", "invalid_ref")
    _run_git(ctx, ["show-ref", "--verify", f"refs/heads/{ref}"], check=True)
    return ref


def _validate_remote_branch(ctx: GitContext, ref: str) -> str:
    ref = str(ref or "").strip()
    if not ref:
        raise GitWorkspaceError("Remote branch name is required", "invalid_ref")
    _run_git(ctx, ["show-ref", "--verify", f"refs/remotes/{ref}"], check=True)
    return ref


def _validate_checkout_start(ctx: GitContext, ref: str) -> str:
    ref = str(ref or "HEAD").strip() or "HEAD"
    result = _run_git(ctx, ["rev-parse", "--verify", f"{ref}^{{commit}}"], check=False)
    if result.returncode != 0:
        raise GitWorkspaceError("Invalid checkout reference", "invalid_ref")
    return ref


def _validate_new_branch_name(ctx: GitContext, name: str) -> str:
    name = str(name or "").strip()
    if not name:
        raise GitWorkspaceError("New branch name is required", "invalid_ref")
    result = _run_git(ctx, ["check-ref-format", "--branch", name], check=False)
    if result.returncode != 0:
        raise GitWorkspaceError("Invalid branch name", "invalid_ref")
    exists = _run_git(ctx, ["show-ref", "--verify", f"refs/heads/{name}"], check=False)
    if exists.returncode == 0:
        raise GitWorkspaceError(
            "A local branch with that name already exists", "invalid_ref"
        )
    return name


def _dirty_worktree(ctx: GitContext) -> bool:
    result = _run_git(
        ctx,
        ["status", "--porcelain=v2", "--untracked-files=all"],
        check=True,
        neutralize_filter_programs=True,
    )
    return bool(result.stdout.strip())


def _current_checkout_label(ctx: GitContext) -> str:
    branch = _run_git(ctx, ["branch", "--show-current"], check=False).stdout.strip()
    if branch:
        return branch
    return (
        _run_git(ctx, ["rev-parse", "--short", "HEAD"], check=True).stdout.strip()
        or "HEAD"
    )


def _stash_subject_parts(subject: str) -> tuple[str, str] | None:
    subject = str(subject or "").strip()
    if not subject.startswith("On ") or ": " not in subject:
        return None
    branch, message = subject[3:].split(": ", 1)
    branch = branch.strip()
    message = message.strip()
    if not branch or not message.startswith(_HERMES_BRANCH_SWITCH_STASH_PREFIX):
        return None
    return branch, message


def _hermes_branch_switch_stashes(ctx: GitContext) -> list[dict]:
    result = _run_git(ctx, ["stash", "list", "--format=%gd%x00%gs"], check=False)
    if result.returncode != 0:
        return []
    stashes = []
    for line in result.stdout.splitlines():
        try:
            ref, subject = line.split("\0", 1)
        except ValueError:
            continue
        parts = _stash_subject_parts(subject)
        if not parts:
            continue
        branch, message = parts
        stashes.append({"ref": ref, "branch": branch, "message": message})
    return stashes


def _restore_branch_switch_stash_locked(ctx: GitContext, branch: str) -> dict:
    if workspace_git_destructive_enabled() and _has_repo_local_filters(
        ctx.repo_root, _clean_git_env()
    ):
        return {
            "restore_blocked": True,
            "restore_reason": "Repository defines local filter programs",
        }
    if _dirty_worktree(ctx):
        return {}
    for item in _hermes_branch_switch_stashes(ctx):
        if item.get("branch") != branch:
            continue
        result = _run_git(
            ctx,
            ["stash", "pop", "--index", item["ref"]],
            check=False,
            destructive=True,
            disable_filter_attributes=True,
        )
        if result.returncode == 0:
            return {"restored_stash": item}
        return {
            "restore_failed": True,
            "restore_error": (
                result.stderr or result.stdout or "Git stash restore failed"
            ).strip(),
            "restore_stash": item,
        }
    return {}


def _validate_checkout_request_locked(
    ctx: GitContext,
    ref: str,
    mode: str,
    new_branch: str | None,
) -> None:
    if mode == "local":
        _validate_local_branch(ctx, ref)
        return
    if mode in {"new", "create"}:
        _validate_new_branch_name(ctx, new_branch or ref)
        _validate_checkout_start(
            ctx, ref if (new_branch and ref and ref != new_branch) else "HEAD"
        )
        return
    if mode == "remote":
        remote_ref = _validate_remote_branch(ctx, ref)
        branch_name = str(new_branch or remote_ref.split("/", 1)[-1]).strip()
        exists = _run_git(
            ctx, ["show-ref", "--verify", f"refs/heads/{branch_name}"], check=False
        )
        if exists.returncode != 0:
            _validate_new_branch_name(ctx, branch_name)
        return
    if mode in {"detached", "detach"}:
        _validate_checkout_start(ctx, ref)
        return
    raise GitWorkspaceError("Unsupported checkout mode", "invalid_ref")


def _perform_checkout_locked(
    ctx: GitContext,
    workspace: str | Path,
    ref: str,
    mode: str,
    new_branch: str | None,
    track: bool,
) -> subprocess.CompletedProcess[str]:
    if workspace_git_destructive_enabled() and _has_repo_local_filters(
        ctx.repo_root, _clean_git_env()
    ):
        raise GitWorkspaceError(
            "Cannot checkout: repository defines local filter programs that would alter file content",
            "filtered_path",
        )
    if mode == "local":
        target = _validate_local_branch(ctx, ref)
        return _run_git(
            ctx,
            ["switch", "--recurse-submodules=no", target],
            check=True,
            destructive=True,
            disable_filter_attributes=True,
        )
    if mode in {"new", "create"}:
        branch = _validate_new_branch_name(ctx, new_branch or ref)
        start_ref = _validate_checkout_start(
            ctx, ref if (new_branch and ref and ref != new_branch) else "HEAD"
        )
        return _run_git(
            ctx,
            ["switch", "--recurse-submodules=no", "-c", branch, start_ref],
            check=True,
            destructive=True,
            disable_filter_attributes=True,
        )
    if mode == "remote":
        remote_ref = _validate_remote_branch(ctx, ref)
        branch_name = str(new_branch or remote_ref.split("/", 1)[-1]).strip()
        exists = _run_git(
            ctx, ["show-ref", "--verify", f"refs/heads/{branch_name}"], check=False
        )
        if exists.returncode == 0:
            result = _run_git(
                ctx,
                ["switch", "--recurse-submodules=no", branch_name],
                check=True,
                destructive=True,
                disable_filter_attributes=True,
            )
            if track:
                _run_git(
                    ctx,
                    ["branch", "--set-upstream-to", remote_ref, branch_name],
                    check=False,
                )
            return result
        branch = _validate_new_branch_name(ctx, branch_name)
        args = ["switch", "--recurse-submodules=no", "-c", branch]
        if track:
            args.append("--track")
        args.append(remote_ref)
        return _run_git(
            ctx,
            args,
            check=True,
            destructive=True,
            disable_filter_attributes=True,
        )
    if mode in {"detached", "detach"}:
        target = _validate_checkout_start(ctx, ref)
        return _run_git(
            ctx,
            ["switch", "--recurse-submodules=no", "--detach", target],
            check=True,
            destructive=True,
            disable_filter_attributes=True,
        )
    raise GitWorkspaceError("Unsupported checkout mode", "invalid_ref")


def git_checkout(
    workspace: str | Path,
    ref: str,
    mode: str,
    new_branch: str | None = None,
    track: bool = False,
    dirty_mode: str = "block",
) -> dict:
    ctx = resolve_git_context(workspace)
    if ctx is None:
        raise GitWorkspaceError("Workspace is not a Git repository", "not_a_repo")
    mode = str(mode or "local").strip().lower()
    dirty_mode = str(dirty_mode or "block").strip().lower()
    if dirty_mode != "block":
        raise GitWorkspaceError(
            "Only dirty_mode=block is supported for branch checkout", "dirty_worktree"
        )
    with _git_mutation_lock(ctx):
        _validate_checkout_request_locked(ctx, ref, mode, new_branch)
        if _dirty_worktree(ctx):
            raise GitWorkspaceError(
                "Checkout blocked because the Git worktree has uncommitted changes",
                "dirty_worktree",
            )
        result = _perform_checkout_locked(ctx, workspace, ref, mode, new_branch, track)
    status = git_status(workspace)
    branches = git_branches(workspace)
    return {
        "ok": True,
        "message": git_command_message(result),
        "current_branch": branches.get("current"),
        "status": status,
        "branches": branches,
    }


def git_stash_and_checkout(
    workspace: str | Path,
    ref: str,
    mode: str,
    new_branch: str | None = None,
    track: bool = False,
) -> dict:
    ctx = resolve_git_context(workspace)
    if ctx is None:
        raise GitWorkspaceError("Workspace is not a Git repository", "not_a_repo")
    mode = str(mode or "local").strip().lower()
    target_label = str(new_branch or ref or "HEAD").strip() or "HEAD"
    stash_name = f"{_HERMES_BRANCH_SWITCH_STASH_PREFIX} to {target_label}".strip()
    restored: dict = {}
    with _git_mutation_lock(ctx):
        _validate_checkout_request_locked(ctx, ref, mode, new_branch)
        if workspace_git_destructive_enabled() and _has_repo_local_filters(
            ctx.repo_root, _clean_git_env()
        ):
            raise GitWorkspaceError(
                "Cannot stash: repository defines local filter programs that would alter file content",
                "filtered_path",
            )
        stashed = False
        if _dirty_worktree(ctx):
            stash_result = _run_git(
                ctx,
                ["stash", "push", "-u", "-m", stash_name],
                check=True,
                destructive=True,
                disable_filter_attributes=True,
            )
            stash_text = git_command_message(stash_result)
            stashed = "No local changes to save" not in stash_text
        try:
            result = _perform_checkout_locked(
                ctx, workspace, ref, mode, new_branch, track
            )
        except Exception:
            if stashed:
                _run_git(
                    ctx,
                    ["stash", "pop", "--index", "stash@{0}"],
                    check=False,
                    destructive=True,
                    disable_filter_attributes=True,
                )
            raise
        current_branch = _current_checkout_label(ctx)
        restored = _restore_branch_switch_stash_locked(ctx, current_branch)
    status = git_status(workspace)
    branches = git_branches(workspace)
    return {
        "ok": True,
        "message": git_command_message(result),
        "stash_name": stash_name if stashed else "",
        "stashed": stashed,
        "restored_stash": restored.get("restored_stash"),
        "restore_failed": bool(restored.get("restore_failed")),
        "restore_error": restored.get("restore_error", ""),
        "restore_stash": restored.get("restore_stash"),
        "current_branch": branches.get("current"),
        "status": status,
        "branches": branches,
    }
