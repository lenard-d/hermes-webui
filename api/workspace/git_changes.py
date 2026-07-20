"""Workspace-scoped diff, index, and discard operations.

Every requested path is normalized against the resolved Git repository before
it becomes a pathspec. Destructive writes retain the repository mutation lock
and anchored filesystem deletion semantics.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Iterable

from .git import DIFF_SIZE_LIMIT, git_status
from .git_repository import (
    GitContext,
    GitWorkspaceError,
    _block_filtered_destructive_write,
    _git_mutation_lock,
    _repo_rel,
    _workspace_rel,
    normalize_workspace_paths,
    resolve_git_context,
    run_git as _run_git,
)
from .path_safety import rmtree_anchored, safe_resolve_ws, unlink_anchored


def _diff_stats(diff_text: str) -> tuple[int, int]:
    additions = deletions = 0
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return additions, deletions


def _synthetic_untracked_diff(path: Path, label: str) -> dict:
    try:
        if not path.is_file():
            raise GitWorkspaceError("Path is not a file")
        if path.stat().st_size > DIFF_SIZE_LIMIT:
            return {
                "binary": False,
                "too_large": True,
                "diff": "",
                "additions": 0,
                "deletions": 0,
            }
    except OSError as exc:
        raise GitWorkspaceError(str(exc)) from exc
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise GitWorkspaceError(str(exc)) from exc
    if b"\0" in data:
        return {
            "binary": True,
            "too_large": False,
            "diff": "",
            "additions": 0,
            "deletions": 0,
        }
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "binary": True,
            "too_large": False,
            "diff": "",
            "additions": 0,
            "deletions": 0,
        }
    lines = text.splitlines()
    diff_lines = list(
        difflib.unified_diff(
            [], lines, fromfile="/dev/null", tofile=f"b/{label}", lineterm=""
        )
    )
    diff = "\n".join(diff_lines) + ("\n" if diff_lines else "")
    too_large = len(diff.encode("utf-8", errors="replace")) > DIFF_SIZE_LIMIT
    if too_large:
        diff = diff[:DIFF_SIZE_LIMIT]
    additions, deletions = _diff_stats(diff)
    return {
        "binary": False,
        "too_large": too_large,
        "diff": diff,
        "additions": additions,
        "deletions": deletions,
    }


def git_diff(workspace: str | Path, path: str, kind: str = "unstaged") -> dict:
    ctx = resolve_git_context(workspace)
    if ctx is None:
        raise GitWorkspaceError("Workspace is not a Git repository")
    if kind not in {"unstaged", "staged"}:
        raise GitWorkspaceError("kind must be staged or unstaged")
    repo_rel = _repo_rel(ctx, path)
    workspace_rel = _workspace_rel(ctx, repo_rel) or path

    status = git_status(workspace)
    file_state = next(
        (f for f in status.get("files", []) if f.get("path") == workspace_rel), None
    )
    if kind == "unstaged" and file_state and file_state.get("untracked"):
        payload = _synthetic_untracked_diff(
            ctx.workspace / workspace_rel, workspace_rel
        )
        return {"path": workspace_rel, "kind": kind, **payload}

    args = ["diff", "--no-ext-diff", "--no-textconv", "--unified=3"]
    if kind == "staged":
        args.append("--cached")
    args.extend(["--", repo_rel])
    result = _run_git(ctx, args, check=True, neutralize_filter_programs=True)
    diff = result.stdout
    binary = "Binary files " in diff or "GIT binary patch" in diff
    too_large = len(diff.encode("utf-8", errors="replace")) > DIFF_SIZE_LIMIT
    if too_large:
        diff = diff[:DIFF_SIZE_LIMIT]
    additions, deletions = _diff_stats(diff)
    return {
        "path": workspace_rel,
        "kind": kind,
        "binary": binary,
        "too_large": too_large,
        "additions": additions,
        "deletions": deletions,
        "diff": "" if binary else diff,
    }


def _pathspecs(ctx: GitContext, paths: Iterable[str]) -> list[str]:
    return [_repo_rel(ctx, path) for path in normalize_workspace_paths(paths)]


def git_stage(workspace: str | Path, paths: Iterable[str]) -> dict:
    ctx = resolve_git_context(workspace)
    if ctx is None:
        raise GitWorkspaceError("Workspace is not a Git repository", "not_a_repo")
    with _git_mutation_lock(ctx):
        _block_filtered_destructive_write(
            ctx,
            "Repository uses local Git filters; stage may corrupt index content. Use the terminal to stage manually.",
        )
        _run_git(
            ctx,
            ["add", "--", *_pathspecs(ctx, paths)],
            check=True,
            destructive=True,
            disable_filter_attributes=True,
        )
    return git_status(workspace)


def git_unstage(workspace: str | Path, paths: Iterable[str]) -> dict:
    ctx = resolve_git_context(workspace)
    if ctx is None:
        raise GitWorkspaceError("Workspace is not a Git repository", "not_a_repo")
    specs = _pathspecs(ctx, paths)
    with _git_mutation_lock(ctx):
        result = _run_git(
            ctx,
            ["restore", "--staged", "--", *specs],
            check=False,
            destructive=True,
        )
        if result.returncode != 0:
            _run_git(ctx, ["reset", "HEAD", "--", *specs], check=True, destructive=True)
    return git_status(workspace)


def git_discard(
    workspace: str | Path, paths: Iterable[str], *, delete_untracked: bool = False
) -> dict:
    ctx = resolve_git_context(workspace)
    if ctx is None:
        raise GitWorkspaceError("Workspace is not a Git repository", "not_a_repo")
    with _git_mutation_lock(ctx):
        _block_filtered_destructive_write(
            ctx,
            "Repository uses local Git filters; discard may corrupt working-tree content. "
            "Use the terminal to discard manually.",
        )
        status = git_status(workspace)
        by_path = {f["path"]: f for f in status.get("files", [])}
        for path in normalize_workspace_paths(paths):
            repo_rel = _repo_rel(ctx, path)
            workspace_rel = _workspace_rel(ctx, repo_rel) or path
            state = by_path.get(workspace_rel) or by_path.get(
                workspace_rel.rstrip("/") + "/"
            )
            if state and state.get("conflict"):
                raise GitWorkspaceError(
                    "Conflicted files cannot be discarded from this panel", "conflict"
                )
            if state and state.get("untracked"):
                if not delete_untracked:
                    raise GitWorkspaceError(
                        "Untracked files require delete_untracked=true"
                    )
                target = safe_resolve_ws(ctx.workspace, workspace_rel)
                if target.is_dir():
                    rmtree_anchored(ctx.workspace, target)
                else:
                    try:
                        unlink_anchored(ctx.workspace, target)
                    except FileNotFoundError:
                        # Preserve the previous Path.unlink(missing_ok=True)
                        # behavior for benign races where another process
                        # removes the untracked file after git_status() has
                        # reported it but before this discard reaches unlink.
                        pass
                continue
            _run_git(
                ctx,
                ["restore", "--worktree", "--", repo_rel],
                check=True,
                destructive=True,
                disable_filter_attributes=True,
            )
    return git_status(workspace)
