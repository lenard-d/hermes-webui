"""Repository identity and safe Git execution for workspace Git operations.

``WorkspaceGitRepository`` is the owner for the trust-sensitive transition from
a session workspace to a concrete Git subprocess.  It keeps repository
identity, workspace-relative path validation, subprocess hardening, structured
errors, temporary execution resources, and per-repository mutation ownership
behind one interface.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from .path_safety import safe_resolve_ws


logger = logging.getLogger(__name__)

GIT_TIMEOUT = 5
GIT_REMOTE_TIMEOUT = 60
WORKSPACE_GIT_DESTRUCTIVE_ENV = "HERMES_WEBUI_WORKSPACE_GIT_DESTRUCTIVE"

_GIT_ENV_SCRUB_KEYS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
)
_GIT_ENV_SCRUB_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
_GIT_HARDENED_CONFIG = (
    # Workspace Git operations can run against repositories provided by agents,
    # restored sessions, or mounted workspaces. Keep repo-local configuration
    # from turning read/status/fetch calls into host command execution.
    ("core.fsmonitor", "false"),
    # Force the unmodified system ssh binary rather than clearing it. An empty
    # value would break legitimate ssh fetches, while this overrides a
    # repo-local core.sshCommand that points at an attacker helper.
    ("core.sshCommand", "ssh"),
    ("core.askPass", ""),
    ("credential.helper", ""),
    ("protocol.ext.allow", "never"),
    ("core.gitProxy", ""),
    ("submodule.recurse", "false"),
    ("fetch.recurseSubmodules", "false"),
)
_GIT_DESTRUCTIVE_HARDENED_CONFIG = (
    ("commit.gpgSign", "false"),
    ("push.gpgSign", "false"),
    ("gpg.program", ""),
    ("gpg.ssh.program", ""),
    ("gpg.x509.program", ""),
    ("core.alternateRefsCommand", ""),
)

_FILTER_CONFIG_RE = re.compile(r"^filter\.(.+)\.(clean|smudge|process|required)$")
_MERGE_DRIVER_CONFIG_RE = re.compile(r"^merge\.(.+)\.driver$")
_REMOTE_HELPER_CONFIG_RE = re.compile(r"^remote\.(.+)\.(uploadpack|receivepack)$")

_LOCKS_GUARD = threading.Lock()
_OP_LOCKS: dict[str, threading.Lock] = {}

def _windows_hide_flags() -> int:
    """Hide short-lived Git console windows on Windows without detaching."""
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def workspace_git_destructive_enabled() -> bool:
    env_name = WORKSPACE_GIT_DESTRUCTIVE_ENV
    return os.getenv(env_name, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _clean_git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if extra:
        env.update(extra)
    scrub_keys = _GIT_ENV_SCRUB_KEYS
    scrub_prefixes = _GIT_ENV_SCRUB_PREFIXES
    for key in scrub_keys:
        env.pop(key, None)
    for key in list(env):
        if key.startswith(scrub_prefixes):
            env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


class GitWorkspaceError(RuntimeError):
    """User-facing Git operation error with a stable response code."""

    def __init__(self, message: str, code: str = "git_failed"):
        super().__init__(message)
        self.code = code


def _classify_git_error(message: str, args: list[str] | None = None) -> str:
    text = (message or "").lower()
    joined = " ".join(args or []).lower()
    if "timed out" in text:
        return "timeout"
    if "not installed" in text or "no such file or directory: 'git'" in text:
        return "missing_git"
    if "not a git repository" in text:
        return "not_a_repo"
    if "outside the workspace" in text or "outside the git repository" in text:
        return "path_outside_workspace"
    if "authentication failed" in text or "permission denied" in text or "could not read username" in text:
        return "auth_failed"
    if "no upstream" in text or "no configured push destination" in text or "has no upstream branch" in text:
        return "no_upstream"
    if "non-fast-forward" in text or "fetch first" in text or ("rejected" in text and "push" in joined):
        return "non_fast_forward"
    if "conflict" in text or "unmerged" in text or ("merge" in text and "needs" in text):
        return "conflict"
    if "working tree" in text and ("clean" in text or "dirty" in text):
        return "dirty_worktree"
    if "local changes" in text or "would be overwritten by checkout" in text:
        return "dirty_worktree"
    if "invalid reference" in text or "not a valid" in text or "unknown revision" in text:
        return "invalid_ref"
    if "hook" in text:
        return "hook_failed"
    return "git_failed"


def _hardened_git_argv(
    args: list[str],
    *,
    destructive: bool = False,
    attributes_file: str | None = None,
    hooks_path: str | None = None,
) -> list[str]:
    argv = ["git"]
    hardened_config = _GIT_HARDENED_CONFIG
    destructive_config = _GIT_DESTRUCTIVE_HARDENED_CONFIG
    for key, value in hardened_config:
        argv.extend(["-c", f"{key}={value}"])
    if destructive:
        for key, value in destructive_config:
            argv.extend(["-c", f"{key}={value}"])
        if hooks_path:
            argv.extend(["-c", f"core.hooksPath={hooks_path}"])
    if attributes_file:
        argv.extend(["-c", f"core.attributesFile={attributes_file}"])
    argv.extend(args)
    return argv


def _config_names_for_scope(
    scope: str,
    cwd: Path,
    env: dict[str, str],
    config_pattern: str,
    name_re: re.Pattern[str],
    *,
    ignore_unsupported: bool = False,
) -> set[str]:
    windows_hide_flags = _windows_hide_flags
    result = subprocess.run(
        ["git", "config", "--includes", scope, "--name-only", "--get-regexp", config_pattern],
        cwd=str(cwd),
        shell=False,
        text=True,
        capture_output=True,
        timeout=GIT_TIMEOUT,
        env=env,
        creationflags=windows_hide_flags(),
    )
    if result.returncode not in {0, 1}:
        if ignore_unsupported:
            return set()
        message = (result.stderr or result.stdout or "Git command failed").strip()
        classify_error = _classify_git_error
        raise GitWorkspaceError(message, classify_error(message, ["config"]))
    names: set[str] = set()
    for line in (result.stdout or "").splitlines():
        match = name_re.match(line.strip())
        if match:
            names.add(match.group(1))
    return names


def _filter_names_for_scope(
    scope: str,
    cwd: Path,
    env: dict[str, str],
    *,
    ignore_unsupported: bool = False,
) -> set[str]:
    config_names_for_scope = _config_names_for_scope
    return config_names_for_scope(
        scope,
        cwd,
        env,
        r"^filter\..*\.(clean|smudge|process|required)$",
        _FILTER_CONFIG_RE,
        ignore_unsupported=ignore_unsupported,
    )


def _merge_driver_names_for_scope(
    scope: str,
    cwd: Path,
    env: dict[str, str],
    *,
    ignore_unsupported: bool = False,
) -> set[str]:
    config_names_for_scope = _config_names_for_scope
    return config_names_for_scope(
        scope,
        cwd,
        env,
        r"^merge\..*\.driver$",
        _MERGE_DRIVER_CONFIG_RE,
        ignore_unsupported=ignore_unsupported,
    )


def _remote_helper_names_for_scope(
    scope: str,
    cwd: Path,
    env: dict[str, str],
    *,
    ignore_unsupported: bool = False,
) -> set[str]:
    config_names_for_scope = _config_names_for_scope
    return config_names_for_scope(
        scope,
        cwd,
        env,
        r"^remote\..*\.(uploadpack|receivepack)$",
        _REMOTE_HELPER_CONFIG_RE,
        ignore_unsupported=ignore_unsupported,
    )


def _destructive_filter_overrides(cwd: Path, env: dict[str, str]) -> list[tuple[str, str]]:
    filter_names_for_scope = _filter_names_for_scope
    names = filter_names_for_scope("--local", cwd, env)
    names |= filter_names_for_scope("--worktree", cwd, env, ignore_unsupported=True)
    overrides: list[tuple[str, str]] = []
    for name in sorted(names):
        if "\n" in name or "\0" in name:
            logger.warning("Skipping filter name with illegal characters: %r", name)
            continue
        overrides.extend(
            [
                (f"filter.{name}.clean", "cat"),
                (f"filter.{name}.smudge", "cat"),
                (f"filter.{name}.process", ""),
                (f"filter.{name}.required", "false"),
            ]
        )
    return overrides


def _destructive_merge_driver_overrides(cwd: Path, env: dict[str, str]) -> list[tuple[str, str]]:
    merge_driver_names_for_scope = _merge_driver_names_for_scope
    names = merge_driver_names_for_scope("--local", cwd, env)
    names |= merge_driver_names_for_scope("--worktree", cwd, env, ignore_unsupported=True)
    overrides: list[tuple[str, str]] = []
    for name in sorted(names):
        if "\n" in name or "\0" in name:
            logger.warning("Skipping merge driver name with illegal characters: %r", name)
            continue
        overrides.append((f"merge.{name}.driver", 'git merge-file "%A" "%O" "%B"'))
    return overrides


def _destructive_remote_helper_overrides(cwd: Path, env: dict[str, str]) -> list[tuple[str, str]]:
    remote_helper_names_for_scope = _remote_helper_names_for_scope
    names = remote_helper_names_for_scope("--local", cwd, env)
    names |= remote_helper_names_for_scope("--worktree", cwd, env, ignore_unsupported=True)
    overrides: list[tuple[str, str]] = []
    for name in sorted(names):
        if "\n" in name or "\0" in name:
            logger.warning("Skipping remote helper name with illegal characters: %r", name)
            continue
        overrides.extend(
            [
                (f"remote.{name}.uploadpack", "git-upload-pack"),
                (f"remote.{name}.receivepack", "git-receive-pack"),
            ]
        )
    return overrides


def _destructive_remote_command_args(args: list[str], cwd: Path, env: dict[str, str]) -> list[str]:
    if not args:
        return args
    remote_helper_names_for_scope = _remote_helper_names_for_scope
    names = remote_helper_names_for_scope("--local", cwd, env)
    names |= remote_helper_names_for_scope("--worktree", cwd, env, ignore_unsupported=True)
    if not names:
        return args
    command = args[0]
    if command in {"fetch", "pull"}:
        return [command, "--upload-pack=git-upload-pack", *args[1:]]
    if command == "push":
        return [command, "--receive-pack=git-receive-pack", *args[1:]]
    return args


def _has_repo_local_filters(cwd: Path, env: dict[str, str]) -> bool:
    filter_names_for_scope = _filter_names_for_scope
    names = filter_names_for_scope("--local", cwd, env)
    names |= filter_names_for_scope("--worktree", cwd, env, ignore_unsupported=True)
    return bool(names)


@dataclass(frozen=True)
class WorkspaceGitRepository:
    """Own one workspace-scoped view of a concrete Git repository."""

    workspace: Path
    repo_root: Path
    workspace_prefix: str

    @classmethod
    def resolve(cls, workspace: str | Path) -> WorkspaceGitRepository | None:
        ws = Path(workspace).expanduser().resolve()
        git_runner = run_git
        result = git_runner(ws, ["rev-parse", "--show-toplevel"], check=False)
        if result.returncode != 0:
            return None
        repo_root = Path(result.stdout.strip()).resolve()
        try:
            prefix = ws.relative_to(repo_root).as_posix()
        except ValueError:
            return None
        return cls(
            workspace=ws,
            repo_root=repo_root,
            workspace_prefix="" if prefix == "." else prefix,
        )

    @property
    def pathspec(self) -> str:
        return self.workspace_prefix or "."

    def repo_relative(self, workspace_rel: str) -> str:
        try:
            path_resolver = safe_resolve_ws
            target = path_resolver(self.workspace, workspace_rel or ".")
        except ValueError as exc:
            raise GitWorkspaceError(str(exc), "path_outside_workspace") from exc
        try:
            repo_rel = target.relative_to(self.repo_root).as_posix()
        except ValueError as exc:
            raise GitWorkspaceError("Path is outside the Git repository", "path_outside_workspace") from exc
        if self.workspace_prefix:
            try:
                target.relative_to(self.workspace)
            except ValueError as exc:
                raise GitWorkspaceError("Path is outside the workspace", "path_outside_workspace") from exc
        return repo_rel

    def workspace_relative(self, repo_rel: str) -> str | None:
        repo_rel = repo_rel.replace("\\", "/")
        if not self.workspace_prefix:
            return repo_rel
        prefix = self.workspace_prefix.rstrip("/") + "/"
        if repo_rel == self.workspace_prefix:
            return "."
        if repo_rel.startswith(prefix):
            return repo_rel[len(prefix) :]
        return None

    def run(
        self,
        args: list[str],
        *,
        timeout: int = GIT_TIMEOUT,
        check: bool = False,
        env: dict[str, str] | None = None,
        destructive: bool = False,
        force_destructive_hardening: bool = False,
        disable_filter_attributes: bool = False,
        neutralize_filter_programs: bool = False,
        neutralize_remote_helpers: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        git_runner = run_git
        return git_runner(
            self,
            args,
            timeout=timeout,
            check=check,
            env=env,
            destructive=destructive,
            force_destructive_hardening=force_destructive_hardening,
            disable_filter_attributes=disable_filter_attributes,
            neutralize_filter_programs=neutralize_filter_programs,
            neutralize_remote_helpers=neutralize_remote_helpers,
        )

    @contextmanager
    def mutation(self) -> Iterator[None]:
        key = str(self.repo_root)
        locks_guard = _LOCKS_GUARD
        operation_locks = _OP_LOCKS
        remote_timeout = GIT_REMOTE_TIMEOUT
        with locks_guard:
            lock = operation_locks.setdefault(key, threading.Lock())
        if not lock.acquire(timeout=remote_timeout):
            raise GitWorkspaceError("Another Git operation is still running", "operation_in_progress")
        try:
            yield
        finally:
            lock.release()

    def block_filtered_write(self, message: str) -> None:
        destructive_enabled = workspace_git_destructive_enabled
        filter_check = _has_repo_local_filters
        clean_env = _clean_git_env
        if destructive_enabled() and filter_check(
            self.repo_root,
            clean_env(),
        ):
            raise GitWorkspaceError(message, "filtered_path")


def run_git(
    repository_or_cwd: WorkspaceGitRepository | Path,
    args: list[str],
    *,
    timeout: int = GIT_TIMEOUT,
    check: bool = False,
    env: dict[str, str] | None = None,
    destructive: bool = False,
    force_destructive_hardening: bool = False,
    disable_filter_attributes: bool = False,
    neutralize_filter_programs: bool = False,
    neutralize_remote_helpers: bool = False,
) -> subprocess.CompletedProcess[str]:
    cwd = (
        repository_or_cwd.repo_root
        if isinstance(repository_or_cwd, WorkspaceGitRepository)
        else Path(repository_or_cwd)
    )
    clean_env = _clean_git_env
    destructive_enabled = workspace_git_destructive_enabled
    run_env = clean_env(env)
    effective_destructive = destructive and destructive_enabled()
    hardened_destructive_path = effective_destructive or force_destructive_hardening
    attributes_file = None
    hooks_path = None
    extra_configs: list[tuple[str, str]] = []
    temporary_attributes: list[str] = []
    temporary_dirs: list[str] = []
    try:
        if disable_filter_attributes:
            fd, attributes_path = tempfile.mkstemp(prefix="hermes-webui-git-attrs-")
            os.close(fd)
            attributes_file = attributes_path
            temporary_attributes = [attributes_path]
        if disable_filter_attributes or neutralize_filter_programs:
            filter_overrides = _destructive_filter_overrides
            extra_configs.extend(filter_overrides(cwd, run_env))
        if effective_destructive:
            merge_overrides = _destructive_merge_driver_overrides
            extra_configs.extend(merge_overrides(cwd, run_env))
        if effective_destructive or neutralize_remote_helpers:
            remote_overrides = _destructive_remote_helper_overrides
            remote_args = _destructive_remote_command_args
            extra_configs.extend(remote_overrides(cwd, run_env))
            args = remote_args(args, cwd, run_env)
        if hardened_destructive_path:
            hooks_path = tempfile.mkdtemp(prefix="hermes-webui-git-hooks-")
            temporary_dirs = [hooks_path]
        if extra_configs:
            run_env["GIT_CONFIG_COUNT"] = str(len(extra_configs))
            for index, (key, value) in enumerate(extra_configs):
                run_env[f"GIT_CONFIG_KEY_{index}"] = key
                run_env[f"GIT_CONFIG_VALUE_{index}"] = value
        hardened_argv = _hardened_git_argv
        windows_hide_flags = _windows_hide_flags
        result = subprocess.run(
            hardened_argv(
                args,
                destructive=hardened_destructive_path,
                attributes_file=attributes_file,
                hooks_path=hooks_path,
            ),
            cwd=str(cwd),
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=run_env,
            creationflags=windows_hide_flags(),
        )
    except subprocess.TimeoutExpired as exc:
        raise GitWorkspaceError("Git command timed out", "timeout") from exc
    except FileNotFoundError as exc:
        raise GitWorkspaceError("Git is not installed or not available on PATH", "missing_git") from exc
    except OSError as exc:
        classify_error = _classify_git_error
        raise GitWorkspaceError(str(exc), classify_error(str(exc), args)) from exc
    finally:
        for path in temporary_attributes:
            Path(path).unlink(missing_ok=True)
        for path in temporary_dirs:
            shutil.rmtree(path, ignore_errors=True)
    if check and result.returncode != 0:
        message = (result.stderr or result.stdout or "Git command failed").strip()
        classify_error = _classify_git_error
        raise GitWorkspaceError(message, classify_error(message, args))
    return result


def git_command_message(result: subprocess.CompletedProcess[str]) -> str:
    """Project a Git subprocess result into the user-facing message text."""
    return (result.stdout or result.stderr or "").strip()


# Functional helpers used by the high-level Git workflow.
GitContext = WorkspaceGitRepository


def resolve_git_context(workspace: str | Path) -> WorkspaceGitRepository | None:
    return WorkspaceGitRepository.resolve(workspace)


def _git_mutation_lock(ctx: WorkspaceGitRepository):
    return ctx.mutation()


def _workspace_pathspec(ctx: WorkspaceGitRepository) -> str:
    return ctx.pathspec


def _repo_rel(ctx: WorkspaceGitRepository, workspace_rel: str) -> str:
    return ctx.repo_relative(workspace_rel)


def normalize_workspace_paths(paths: Iterable[str]) -> list[str]:
    """Normalize and de-duplicate caller paths before repository validation."""
    cleaned: list[str] = []
    for path in paths:
        value = str(path or "").strip()
        if value and value not in cleaned:
            cleaned.append(value)
    if not cleaned:
        raise GitWorkspaceError("At least one path is required")
    return cleaned


def _workspace_rel(ctx: WorkspaceGitRepository, repo_rel: str) -> str | None:
    return ctx.workspace_relative(repo_rel)


def _block_filtered_destructive_write(ctx: WorkspaceGitRepository, message: str) -> None:
    ctx.block_filtered_write(message)
