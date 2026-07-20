"""Read-only Git status projection for workspace repositories.

This module owns the bounded status snapshot used by the workspace header and
Git panel. Repository identity and subprocess hardening remain in
:mod:`api.workspace.git_repository`.
"""

from __future__ import annotations

import concurrent.futures
import subprocess
from pathlib import Path

from .git_repository import (
    GitContext,
    _workspace_pathspec,
    _workspace_rel,
    resolve_git_context,
    run_git as _run_git,
    workspace_git_destructive_enabled,
)


STATUS_FILE_LIMIT = 500
DIFF_SIZE_LIMIT = 512 * 1024

def _run_summary_git(args: list[str], cwd: Path, timeout: int = 3) -> str | None:
    """Run a bounded read-only Git summary command."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def git_info_for_workspace(workspace: Path) -> dict | None:
    """Return the compact read-only Git summary used by the workspace header."""
    if not (workspace / ".git").exists():
        return None
    branch = _run_summary_git(["rev-parse", "--abbrev-ref", "HEAD"], workspace)
    if branch is None:
        return None

    def ahead_count() -> int:
        value = _run_summary_git(["rev-list", "--count", "@{u}..HEAD"], workspace)
        return int(value) if value and value.isdigit() else 0

    def behind_count() -> int:
        value = _run_summary_git(["rev-list", "--count", "HEAD..@{u}"], workspace)
        return int(value) if value and value.isdigit() else 0

    def status_counts() -> tuple[int, int, int]:
        output = _run_summary_git(["status", "--porcelain"], workspace) or ""
        lines = [line for line in output.splitlines() if line]
        modified = sum(
            1
            for line in lines
            if len(line) >= 2 and (line[0] in "MAR" or line[1] in "MAR")
        )
        untracked = sum(1 for line in lines if line.startswith("??"))
        return len(lines), modified, untracked

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        status_future = pool.submit(status_counts)
        ahead_future = pool.submit(ahead_count)
        behind_future = pool.submit(behind_count)
        dirty, modified, untracked = status_future.result()
        ahead = ahead_future.result()
        behind = behind_future.result()
    return {
        "branch": branch,
        "dirty": dirty,
        "modified": modified,
        "untracked": untracked,
        "ahead": ahead,
        "behind": behind,
        "is_git": True,
    }


def _empty_status() -> dict:
    return {
        "changed": 0,
        "staged": 0,
        "unstaged": 0,
        "untracked": 0,
        "conflicts": 0,
    }


def _status_code(xy: str, *, untracked: bool = False, renamed: bool = False) -> str:
    if untracked:
        return "??"
    if xy in {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}:
        return xy
    if renamed:
        return "R"
    for ch in xy:
        if ch in "MADRCUT":
            return ch
    return xy.strip(".") or "M"


def _parse_numstat(text: str, ctx: GitContext) -> dict[str, tuple[int, int, bool]]:
    stats: dict[str, tuple[int, int, bool]] = {}
    for line in text.splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        raw_add, raw_del, raw_path = parts
        binary = raw_add == "-" or raw_del == "-"
        additions = 0 if binary else int(raw_add or "0")
        deletions = 0 if binary else int(raw_del or "0")
        workspace_path = _workspace_rel(ctx, raw_path)
        if workspace_path is None:
            continue
        stats[workspace_path] = (additions, deletions, binary)
    return stats


def _parse_path_list(text: str, ctx: GitContext) -> set[str]:
    paths: set[str] = set()
    for raw_path in text.split("\0"):
        if not raw_path:
            continue
        workspace_path = _workspace_rel(ctx, raw_path)
        if workspace_path is not None:
            paths.add(workspace_path)
    return paths


def _collect_diff_paths(ctx: GitContext, cached: bool, *, ignore_cr_at_eol: bool = True) -> set[str] | None:
    args = ["diff", "--name-only", "-z"]
    args.append("--no-textconv")
    if ignore_cr_at_eol:
        args.append("--ignore-cr-at-eol")
    if cached:
        args.append("--cached")
    args.extend(["--", _workspace_pathspec(ctx)])
    result = _run_git(
        ctx,
        args,
        check=False,
        disable_filter_attributes=workspace_git_destructive_enabled(),
        neutralize_filter_programs=True,
    )
    if result.returncode != 0:
        return None
    return _parse_path_list(result.stdout, ctx)


def _collect_numstat(
    ctx: GitContext,
    cached: bool,
    *,
    ignore_cr_at_eol: bool = True,
) -> dict[str, tuple[int, int, bool]]:
    args = ["diff", "--numstat"]
    args.append("--no-textconv")
    if ignore_cr_at_eol:
        args.append("--ignore-cr-at-eol")
    if cached:
        args.append("--cached")
    args.extend(["--", _workspace_pathspec(ctx)])
    result = _run_git(
        ctx,
        args,
        check=False,
        disable_filter_attributes=workspace_git_destructive_enabled(),
        neutralize_filter_programs=True,
    )
    if result.returncode != 0:
        return {}
    return _parse_numstat(result.stdout, ctx)


def _count_untracked_file(path: Path) -> tuple[int, int, bool]:
    try:
        if not path.is_file() or path.stat().st_size > DIFF_SIZE_LIMIT:
            return 0, 0, False
    except OSError:
        return 0, 0, False
    try:
        data = path.read_bytes()
    except OSError:
        return 0, 0, False
    if b"\0" in data:
        return 0, 0, True
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return 0, 0, True
    return len(text.splitlines()) or (1 if text else 0), 0, False


def git_status(workspace: str | Path) -> dict:
    ctx = resolve_git_context(workspace)
    if ctx is None:
        return {"is_git": False}

    result = _run_git(
        ctx,
        [
            "status",
            "--porcelain=v2",
            "-z",
            "--branch",
            "--ignored=matching",
            "--untracked-files=all",
            "--",
            _workspace_pathspec(ctx),
        ],
        check=True,
        disable_filter_attributes=workspace_git_destructive_enabled(),
        neutralize_filter_programs=True,
    )
    staged_stats = _collect_numstat(ctx, cached=True)
    unstaged_stats = _collect_numstat(ctx, cached=False)
    staged_raw_stats = _collect_numstat(ctx, cached=True, ignore_cr_at_eol=False)
    unstaged_raw_stats = _collect_numstat(ctx, cached=False, ignore_cr_at_eol=False)
    staged_diff_paths = _collect_diff_paths(ctx, cached=True)
    unstaged_diff_paths = _collect_diff_paths(ctx, cached=False)

    branch = ""
    upstream = ""
    ahead = 0
    behind = 0
    files: dict[str, dict] = {}
    filtered_noise = {"filemode_only": 0, "crlf_only": 0}
    tokens = result.stdout.split("\0")
    i = 0
    truncated = False
    while i < len(tokens):
        rec = tokens[i]
        i += 1
        if not rec:
            continue
        if rec.startswith("# "):
            parts = rec.split(" ", 2)
            if len(parts) >= 3 and parts[1] == "branch.head":
                branch = "" if parts[2] == "(detached)" else parts[2]
            elif len(parts) >= 3 and parts[1] == "branch.upstream":
                upstream = parts[2]
            elif len(parts) >= 3 and parts[1] == "branch.ab":
                for bit in parts[2].split():
                    if bit.startswith("+") and bit[1:].isdigit():
                        ahead = int(bit[1:])
                    elif bit.startswith("-") and bit[1:].isdigit():
                        behind = int(bit[1:])
            continue

        old_path = None
        renamed = False
        if rec.startswith("? "):
            xy = "??"
            repo_path = rec[2:]
            untracked = True
            ignored = False
        elif rec.startswith("! "):
            xy = "!!"
            repo_path = rec[2:]
            untracked = False
            ignored = True
        elif rec.startswith("1 "):
            parts = rec.split(" ", 8)
            if len(parts) < 9:
                continue
            xy = parts[1]
            repo_path = parts[8]
            untracked = False
            ignored = False
        elif rec.startswith("2 "):
            parts = rec.split(" ", 9)
            if len(parts) < 10:
                continue
            xy = parts[1]
            repo_path = parts[9]
            if i < len(tokens):
                old_path = tokens[i]
                i += 1
            renamed = True
            untracked = False
            ignored = False
        elif rec.startswith("u "):
            parts = rec.split(" ", 10)
            if len(parts) < 11:
                continue
            xy = parts[1]
            repo_path = parts[10]
            untracked = False
            ignored = False
        else:
            continue

        workspace_path = _workspace_rel(ctx, repo_path)
        if workspace_path is None:
            continue
        old_workspace_path = _workspace_rel(ctx, old_path) if old_path else None
        x = xy[0] if xy else "."
        y = xy[1] if len(xy) > 1 else "."
        conflict = xy in {"DD", "AU", "UD", "UA", "DU", "AA", "UU"} or rec.startswith("u ")
        additions, deletions, binary = 0, 0, False
        for source in (staged_stats, unstaged_stats):
            if workspace_path in source:
                a, d, b = source[workspace_path]
                additions += a
                deletions += d
                binary = binary or b
        if untracked:
            additions, deletions, binary = _count_untracked_file(ctx.workspace / workspace_path)

        staged = (x not in {".", "?"}) and not untracked
        unstaged = (y not in {".", " "}) and not untracked
        if staged and staged_diff_paths is not None and not renamed:
            raw_staged = staged
            staged = workspace_path in staged_diff_paths or (
                old_workspace_path is not None and old_workspace_path in staged_diff_paths
            )
            if raw_staged and not staged:
                if workspace_path in staged_raw_stats or (
                    old_workspace_path is not None and old_workspace_path in staged_raw_stats
                ):
                    filtered_noise["crlf_only"] += 1
                else:
                    filtered_noise["filemode_only"] += 1
        if unstaged and unstaged_diff_paths is not None and not renamed:
            raw_unstaged = unstaged
            unstaged = workspace_path in unstaged_diff_paths or (
                old_workspace_path is not None and old_workspace_path in unstaged_diff_paths
            )
            if raw_unstaged and not unstaged:
                if workspace_path in unstaged_raw_stats or (
                    old_workspace_path is not None and old_workspace_path in unstaged_raw_stats
                ):
                    filtered_noise["crlf_only"] += 1
                else:
                    filtered_noise["filemode_only"] += 1
        if ignored:
            files[workspace_path] = {
                "path": workspace_path,
                "old_path": None,
                "workspace_path": workspace_path,
                "status": "Ignored",
                "staged": False,
                "unstaged": False,
                "untracked": False,
                "ignored": True,
                "conflict": False,
                "additions": 0,
                "deletions": 0,
                "binary": False,
            }
            if len(files) >= STATUS_FILE_LIMIT:
                truncated = True
                break
            continue

        if not (staged or unstaged or untracked or conflict or renamed):
            continue
        if not (untracked or conflict or renamed or binary) and additions == 0 and deletions == 0:
            filtered_noise["crlf_only"] += 1
            continue

        files[workspace_path] = {
            "path": workspace_path,
            "old_path": old_workspace_path,
            "workspace_path": workspace_path,
            "status": _status_code(xy, untracked=untracked, renamed=renamed),
            "staged": staged,
            "unstaged": unstaged,
            "untracked": untracked,
            "ignored": False,
            "conflict": conflict,
            "additions": additions,
            "deletions": deletions,
            "binary": binary,
        }
        if len(files) >= STATUS_FILE_LIMIT:
            truncated = True
            break

    file_list = sorted(files.values(), key=lambda f: (f["path"].lower()))
    totals = _empty_status()
    for item in file_list:
        if item.get("ignored"):
            continue
        if item["staged"]:
            totals["staged"] += 1
        if item["unstaged"]:
            totals["unstaged"] += 1
        if item["untracked"]:
            totals["untracked"] += 1
        if item["conflict"]:
            totals["conflicts"] += 1
    totals["changed"] = sum(1 for item in file_list if not item.get("ignored"))

    if not branch:
        branch = (_run_git(ctx, ["rev-parse", "--short", "HEAD"], check=False).stdout or "").strip()
    return {
        "is_git": True,
        "branch": branch or "HEAD",
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "totals": totals,
        "files": file_list,
        "truncated": truncated,
        "noise_filtering": {
            **filtered_noise,
            "active": any(filtered_noise.values()),
        },
    }
