"""Commit preparation and selected-file commit ownership.

The temporary index lifecycle lives here end-to-end: create, seed, stage,
inspect or commit, reconcile the real index, and unlink on every exit.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Iterable

from .git import git_status
from .git_repository import (
    GitContext,
    GitWorkspaceError,
    _block_filtered_destructive_write,
    _git_mutation_lock,
    _repo_rel,
    _workspace_pathspec,
    _workspace_rel,
    normalize_workspace_paths,
    resolve_git_context,
    run_git as _run_git,
)


COMMIT_MESSAGE_DIFF_LIMIT = 64 * 1024

COMMIT_MESSAGE_SYSTEM_PROMPT = """When writing commit messages, PR titles, or PR descriptions:

- Inspect the staged diff before suggesting a commit message.
- Do not use vague subjects like "update", "improve", "refine", "misc changes", "fix stuff", or "various changes".
- For large commits, write a concise subject plus a short body with 2-5 bullets summarizing the main areas changed.
- The subject should describe the actual user-facing result or bug fixed, not just broad implementation activity.
- Keep wording short, clear, and natural.
- Never mention AI, Cursor, Zed, agents, or similar tooling in commits, branch names, PR titles, or PR descriptions.
- Never add your own thoughts or questions into the commit message, the commit message is definitive in nature.

Return only the commit message text. Do not wrap it in Markdown fences.
""".strip()


def _staged_diff_text(ctx: GitContext) -> tuple[str, bool]:
    result = _run_git(
        ctx,
        [
            "diff",
            "--cached",
            "--no-ext-diff",
            "--no-textconv",
            "--unified=3",
            "--",
            _workspace_pathspec(ctx),
        ],
        check=True,
        neutralize_filter_programs=True,
    )
    diff = result.stdout or ""
    encoded = diff.encode("utf-8", errors="replace")
    if len(encoded) <= COMMIT_MESSAGE_DIFF_LIMIT:
        return diff, False
    return encoded[:COMMIT_MESSAGE_DIFF_LIMIT].decode("utf-8", errors="replace"), True


def _selected_temp_index_env(
    ctx: GitContext, specs: list[str]
) -> tuple[dict[str, str], str]:
    _block_filtered_destructive_write(
        ctx,
        "Repository uses local Git filters; selected commit staging may corrupt index content. "
        "Use the terminal to commit manually.",
    )
    fd, index_path = tempfile.mkstemp(prefix="hermes-webui-git-index-")
    os.close(fd)
    Path(index_path).unlink(missing_ok=True)
    env = {"GIT_INDEX_FILE": index_path}
    try:
        head = _run_git(
            ctx,
            ["rev-parse", "--verify", "HEAD"],
            check=False,
            env=env,
            destructive=True,
        )
        if head.returncode == 0:
            _run_git(ctx, ["read-tree", "HEAD"], check=True, env=env, destructive=True)
        else:
            _run_git(
                ctx, ["read-tree", "--empty"], check=True, env=env, destructive=True
            )
        _run_git(
            ctx,
            ["add", "-A", "--", *specs],
            check=True,
            env=env,
            destructive=True,
            disable_filter_attributes=True,
        )
        return env, index_path
    except Exception:
        Path(index_path).unlink(missing_ok=True)
        raise


def _selected_files(
    ctx: GitContext, paths: Iterable[str]
) -> tuple[list[str], list[str], list[dict]]:
    requested = normalize_workspace_paths(paths)
    requested_specs = [_repo_rel(ctx, path) for path in requested]
    workspace_paths = [
        _workspace_rel(ctx, spec) or path
        for spec, path in zip(requested_specs, requested, strict=True)
    ]
    status = git_status(ctx.workspace)
    by_path = {f["path"]: f for f in status.get("files", [])}
    specs: list[str] = []
    selected = []
    for path, repo_rel in zip(workspace_paths, requested_specs, strict=True):
        state = by_path.get(path)
        if not state:
            continue
        if state.get("conflict"):
            raise GitWorkspaceError(
                "Resolve conflicts before committing selected files", "conflict"
            )
        if state.get("staged") or state.get("unstaged") or state.get("untracked"):
            selected.append(state)
            for spec in (
                repo_rel,
                _repo_rel(ctx, state["old_path"]) if state.get("old_path") else "",
            ):
                if spec and spec not in specs:
                    specs.append(spec)
    if len(selected) != len(workspace_paths):
        raise GitWorkspaceError("Selected paths have no committable changes")
    return specs, workspace_paths, selected


def _selected_diff_text(ctx: GitContext, specs: list[str]) -> tuple[str, bool]:
    env, index_path = _selected_temp_index_env(ctx, specs)
    try:
        result = _run_git(
            ctx,
            [
                "diff",
                "--cached",
                "--no-ext-diff",
                "--no-textconv",
                "--unified=3",
                "--",
                *specs,
            ],
            check=True,
            env=env,
            destructive=True,
            disable_filter_attributes=True,
        )
        diff = result.stdout or ""
        encoded = diff.encode("utf-8", errors="replace")
        if len(encoded) <= COMMIT_MESSAGE_DIFF_LIMIT:
            return diff, False
        return encoded[:COMMIT_MESSAGE_DIFF_LIMIT].decode(
            "utf-8", errors="replace"
        ), True
    finally:
        Path(index_path).unlink(missing_ok=True)


def selected_commit_message_prompt(workspace: str | Path, paths: Iterable[str]) -> dict:
    ctx = resolve_git_context(workspace)
    if ctx is None:
        raise GitWorkspaceError("Workspace is not a Git repository", "not_a_repo")
    specs, _workspace_paths, selected_files = _selected_files(ctx, paths)
    diff, truncated = _selected_diff_text(ctx, specs)
    if not diff.strip():
        raise GitWorkspaceError("No selected diff is available")
    status = git_status(workspace)
    file_lines = []
    for item in selected_files[:80]:
        stats = (
            "binary"
            if item.get("binary")
            else f"+{item.get('additions') or 0} -{item.get('deletions') or 0}"
        )
        file_lines.append(f"- {item.get('status') or 'M'} {item.get('path')} ({stats})")
    if len(selected_files) > 80:
        file_lines.append(f"- ... {len(selected_files) - 80} more selected file(s)")
    user_prompt = (
        "Write a commit message for the selected Git diff below.\n\n"
        f"Branch: {status.get('branch') or 'HEAD'}\n"
        f"Selected files ({len(selected_files)}):\n"
        + "\n".join(file_lines)
        + (
            "\n\nDiff was truncated for size; summarize only what is visible.\n"
            if truncated
            else "\n"
        )
        + "\nSelected diff:\n```diff\n"
        + diff
        + "\n```"
    )
    return {
        "system_prompt": COMMIT_MESSAGE_SYSTEM_PROMPT,
        "user_prompt": user_prompt,
        "truncated": truncated,
        "status": status,
    }


def staged_commit_message_prompt(workspace: str | Path) -> dict:
    ctx = resolve_git_context(workspace)
    if ctx is None:
        raise GitWorkspaceError("Workspace is not a Git repository")
    status = git_status(workspace)
    if int((status.get("totals") or {}).get("staged") or 0) <= 0:
        raise GitWorkspaceError("Stage changes before generating a commit message")
    diff, truncated = _staged_diff_text(ctx)
    if not diff.strip():
        raise GitWorkspaceError("No staged diff is available")
    staged_files = [f for f in status.get("files", []) if f.get("staged")]
    file_lines = []
    for item in staged_files[:80]:
        stats = (
            "binary"
            if item.get("binary")
            else f"+{item.get('additions') or 0} -{item.get('deletions') or 0}"
        )
        file_lines.append(f"- {item.get('status') or 'M'} {item.get('path')} ({stats})")
    if len(staged_files) > 80:
        file_lines.append(f"- ... {len(staged_files) - 80} more staged file(s)")
    user_prompt = (
        "Write a commit message for the staged Git diff below.\n\n"
        f"Branch: {status.get('branch') or 'HEAD'}\n"
        f"Staged files ({len(staged_files)}):\n"
        + "\n".join(file_lines)
        + (
            "\n\nDiff was truncated for size; summarize only what is visible.\n"
            if truncated
            else "\n"
        )
        + "\nStaged diff:\n```diff\n"
        + diff
        + "\n```"
    )
    return {
        "system_prompt": COMMIT_MESSAGE_SYSTEM_PROMPT,
        "user_prompt": user_prompt,
        "truncated": truncated,
        "status": status,
    }


def clean_generated_commit_message(message: str) -> str:
    text = str(message or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if (text.startswith('"') and text.endswith('"')) or (
        text.startswith("'") and text.endswith("'")
    ):
        text = text[1:-1].strip()
    return text


def git_commit(workspace: str | Path, message: str) -> dict:
    msg = str(message or "").strip()
    if not msg:
        raise GitWorkspaceError("Commit message is required")
    ctx = resolve_git_context(workspace)
    if ctx is None:
        raise GitWorkspaceError("Workspace is not a Git repository", "not_a_repo")
    with _git_mutation_lock(ctx):
        _run_git(
            ctx,
            ["commit", "-m", msg],
            timeout=10,
            check=True,
            destructive=True,
            disable_filter_attributes=True,
        )
    sha = _run_git(ctx, ["rev-parse", "--short", "HEAD"], check=True).stdout.strip()
    return {"ok": True, "commit": sha, "status": git_status(workspace)}


def git_commit_selected(
    workspace: str | Path, message: str, paths: Iterable[str]
) -> dict:
    msg = str(message or "").strip()
    if not msg:
        raise GitWorkspaceError("Commit message is required")
    ctx = resolve_git_context(workspace)
    if ctx is None:
        raise GitWorkspaceError("Workspace is not a Git repository", "not_a_repo")
    with _git_mutation_lock(ctx):
        specs, workspace_paths, _selected_files_list = _selected_files(ctx, paths)
        env, index_path = _selected_temp_index_env(ctx, specs)
        try:
            quiet = _run_git(
                ctx,
                ["diff", "--cached", "--quiet", "--no-textconv", "--", *specs],
                check=False,
                env=env,
                destructive=True,
                disable_filter_attributes=True,
            )
            if quiet.returncode == 0:
                raise GitWorkspaceError("Selected paths have no committable changes")
            _run_git(
                ctx,
                ["commit", "-m", msg],
                timeout=10,
                check=True,
                env=env,
                destructive=True,
                disable_filter_attributes=True,
            )
            _run_git(
                ctx,
                ["reset", "-q", "HEAD", "--", *specs],
                check=True,
                destructive=True,
            )
        finally:
            Path(index_path).unlink(missing_ok=True)
    sha = _run_git(ctx, ["rev-parse", "--short", "HEAD"], check=True).stdout.strip()
    return {
        "ok": True,
        "commit": sha,
        "paths": workspace_paths,
        "status": git_status(workspace),
    }
