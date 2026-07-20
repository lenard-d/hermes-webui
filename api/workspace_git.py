"""Temporary compatibility imports for the workspace Git interface.

Production callers use :mod:`api.workspace`.  This module remains only for
legacy imports and owns no state or dispatch.
"""

# Compatibility re-exports intentionally include historical private helpers.
# ruff: noqa: F401

from api.workspace.git import DIFF_SIZE_LIMIT, STATUS_FILE_LIMIT, git_status
from api.workspace.git_refs import (
    _dirty_worktree,
    git_branches,
    git_checkout,
    git_stash_and_checkout,
)
from api.workspace.git_changes import git_diff, git_discard, git_stage, git_unstage
from api.workspace.git_commits import (
    COMMIT_MESSAGE_DIFF_LIMIT,
    COMMIT_MESSAGE_SYSTEM_PROMPT,
    _selected_temp_index_env,
    clean_generated_commit_message,
    git_commit,
    git_commit_selected,
    selected_commit_message_prompt,
    staged_commit_message_prompt,
)
from api.workspace.git_remotes import git_fetch, git_pull, git_push
from api.workspace.git_repository import (
    GIT_REMOTE_TIMEOUT,
    GIT_TIMEOUT,
    WORKSPACE_GIT_DESTRUCTIVE_ENV,
    GitContext,
    GitWorkspaceError,
    _GIT_DESTRUCTIVE_HARDENED_CONFIG,
    _GIT_ENV_SCRUB_KEYS,
    _GIT_ENV_SCRUB_PREFIXES,
    _GIT_HARDENED_CONFIG,
    _LOCKS_GUARD,
    _OP_LOCKS,
    _block_filtered_destructive_write,
    _classify_git_error,
    _clean_git_env,
    _config_names_for_scope,
    _destructive_filter_overrides,
    _destructive_merge_driver_overrides,
    _destructive_remote_command_args,
    _destructive_remote_helper_overrides,
    _filter_names_for_scope,
    _git_mutation_lock,
    _hardened_git_argv,
    _has_repo_local_filters,
    _merge_driver_names_for_scope,
    _remote_helper_names_for_scope,
    _repo_rel,
    run_git as _run_git,
    _windows_hide_flags,
    _workspace_pathspec,
    _workspace_rel,
    resolve_git_context,
    workspace_git_destructive_enabled,
)
from api.workspace.path_safety import safe_resolve_ws, unlink_anchored

__all__ = [
    "COMMIT_MESSAGE_DIFF_LIMIT",
    "COMMIT_MESSAGE_SYSTEM_PROMPT",
    "DIFF_SIZE_LIMIT",
    "GIT_REMOTE_TIMEOUT",
    "GIT_TIMEOUT",
    "GitContext",
    "GitWorkspaceError",
    "STATUS_FILE_LIMIT",
    "WORKSPACE_GIT_DESTRUCTIVE_ENV",
    "clean_generated_commit_message",
    "git_branches",
    "git_checkout",
    "git_commit",
    "git_commit_selected",
    "git_diff",
    "git_discard",
    "git_fetch",
    "git_pull",
    "git_push",
    "git_stage",
    "git_stash_and_checkout",
    "git_status",
    "git_unstage",
    "selected_commit_message_prompt",
    "staged_commit_message_prompt",
    "workspace_git_destructive_enabled",
]
