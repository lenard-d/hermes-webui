import subprocess
import threading

import pytest


def _git(cwd, *args):
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        shell=False,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout


def _init_repo(path):
    path.mkdir(parents=True)
    init = subprocess.run(
        ["git", "init", "-b", "master"],
        cwd=str(path),
        shell=False,
        text=True,
        capture_output=True,
        timeout=20,
    )
    if init.returncode != 0:
        _git(path, "init")
        _git(path, "checkout", "-B", "master")
    return path


def test_repository_owns_identity_path_scope_and_git_execution(tmp_path):
    from api.workspace_git_parts.repository import GitWorkspaceError, WorkspaceGitRepository

    repo = _init_repo(tmp_path / "repo")
    nested = repo / "nested"
    nested.mkdir()
    (nested / "inside.txt").write_text("inside\n", encoding="utf-8")

    owner = WorkspaceGitRepository.resolve(nested)

    assert owner is not None
    assert owner.repo_root == repo.resolve()
    assert owner.workspace == nested.resolve()
    assert owner.pathspec == "nested"
    assert owner.repo_relative("inside.txt") == "nested/inside.txt"
    assert owner.workspace_relative("nested/inside.txt") == "inside.txt"
    assert owner.workspace_relative("outside.txt") is None
    assert owner.run(["rev-parse", "--show-toplevel"], check=True).stdout.strip() == str(repo.resolve())

    with pytest.raises(GitWorkspaceError) as exc:
        owner.repo_relative("../outside.txt")
    assert exc.value.code == "path_outside_workspace"


def test_repository_mutation_owner_serializes_same_repo(tmp_path):
    from api.workspace_git_parts.repository import WorkspaceGitRepository

    repo = _init_repo(tmp_path / "repo")
    nested = repo / "nested"
    nested.mkdir()
    root_owner = WorkspaceGitRepository.resolve(repo)
    nested_owner = WorkspaceGitRepository.resolve(nested)
    assert root_owner is not None
    assert nested_owner is not None

    entered = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()

    def first_mutation():
        with root_owner.mutation():
            entered.set()
            release.wait(timeout=5)

    def second_mutation():
        entered.wait(timeout=5)
        with nested_owner.mutation():
            second_entered.set()

    first = threading.Thread(target=first_mutation)
    second = threading.Thread(target=second_mutation)
    first.start()
    second.start()
    assert entered.wait(timeout=5)
    assert not second_entered.wait(timeout=0.1)
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert second_entered.is_set()


def test_workspace_git_import_star_keeps_historical_public_surface():
    namespace = {}

    exec("from api.workspace_git import *", namespace)

    expected = {
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
    }
    assert expected <= namespace.keys()


def test_repository_late_binds_historical_facade_monkeypatch_seams(tmp_path, monkeypatch):
    from api import workspace_git
    from api.workspace_git_parts.repository import GitWorkspaceError, WorkspaceGitRepository

    repo = _init_repo(tmp_path / "repo")
    (repo / "inside.txt").write_text("inside\n", encoding="utf-8")
    real_run_git = workspace_git._run_git
    real_safe_resolve_ws = workspace_git.safe_resolve_ws
    runner_calls = []
    resolver_calls = []

    def recording_run_git(cwd, args, **kwargs):
        runner_calls.append((cwd, args))
        return real_run_git(cwd, args, **kwargs)

    def recording_safe_resolve_ws(root, path):
        resolver_calls.append((root, path))
        return real_safe_resolve_ws(root, path)

    monkeypatch.setattr(workspace_git, "_run_git", recording_run_git)
    owner = WorkspaceGitRepository.resolve(repo)
    assert owner is not None
    assert runner_calls and runner_calls[0][1] == ["rev-parse", "--show-toplevel"]

    monkeypatch.setattr(workspace_git, "safe_resolve_ws", recording_safe_resolve_ws)
    assert owner.repo_relative("inside.txt") == "inside.txt"
    assert resolver_calls == [(repo.resolve(), "inside.txt")]

    monkeypatch.setattr(workspace_git, "workspace_git_destructive_enabled", lambda: True)
    monkeypatch.setattr(workspace_git, "_has_repo_local_filters", lambda cwd, env: True)
    with pytest.raises(GitWorkspaceError) as exc:
        owner.block_filtered_write("Filtered writes are blocked")
    assert exc.value.code == "filtered_path"
