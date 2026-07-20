"""Architecture contract for the workspace package."""

import importlib
import inspect

from api import workspace
from api.workspace import (
    file_access,
    git,
    git_changes,
    git_commits,
    git_remotes,
    navigation,
    path_safety,
    registry,
)


git_refs = importlib.import_module("api.workspace.git_refs")


def test_workspace_package_reexports_domain_implementations():
    path_exports = (
        "safe_resolve_ws",
        "open_anchored_fd",
        "open_anchored_create_fd",
        "make_anchored_dir",
        "open_anchored_write_fd",
        "unlink_anchored",
        "rmtree_anchored",
        "rename_anchored",
    )
    for name in path_exports:
        assert getattr(workspace, name) is getattr(path_safety, name)

    for name in ("list_dir", "dir_signature", "read_file_content"):
        assert getattr(workspace, name) is getattr(file_access, name)

    navigation_exports = (
        "EscapeAuthorizationExpiredError",
        "authorize_escape_target",
        "resolve_authorized_escape_request",
        "list_authorized_escape_dir",
        "read_authorized_escape_file_content",
        "raw_authorized_escape_target",
    )
    for name in navigation_exports:
        assert getattr(workspace, name) is getattr(navigation, name)

    assert workspace.resolve_trusted_workspace is registry.resolve_trusted_workspace
    assert workspace.git_info_for_workspace is git.git_info_for_workspace
    for name in ("git_branches", "git_checkout", "git_stash_and_checkout"):
        assert getattr(workspace, name) is getattr(git_refs, name)
    for name in ("git_diff", "git_stage", "git_unstage", "git_discard"):
        assert getattr(workspace, name) is getattr(git_changes, name)
    for name in (
        "clean_generated_commit_message",
        "git_commit",
        "git_commit_selected",
        "selected_commit_message_prompt",
        "staged_commit_message_prompt",
    ):
        assert getattr(workspace, name) is getattr(git_commits, name)
    for name in ("git_fetch", "git_pull", "git_push"):
        assert getattr(workspace, name) is getattr(git_remotes, name)


def test_workspace_internals_use_direct_imports_without_facade_binding():
    for module in (
        path_safety,
        file_access,
        navigation,
        git,
        git_refs,
        git_changes,
        git_commits,
        git_remotes,
    ):
        source = inspect.getsource(module)
        assert "workspace_api" not in source
        assert "sys.modules" not in source
        assert "api.workspace_parts" not in source
        assert "api.workspace_git_parts" not in source


def test_file_access_uses_path_safety_owner(tmp_path, monkeypatch):
    target = tmp_path / "note.txt"
    target.write_text("hello", encoding="utf-8")
    calls = []
    real_resolve = path_safety.safe_resolve_ws

    def tracking_resolve(root, rel):
        calls.append((root, rel))
        return real_resolve(root, rel)

    monkeypatch.setattr(path_safety, "safe_resolve_ws", tracking_resolve)
    assert file_access.read_file_content(tmp_path, "note.txt")["content"] == "hello"
    assert calls == [(tmp_path, "note.txt")]


def test_file_access_honors_path_safety_dir_fd_switch(tmp_path, monkeypatch):
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setattr(path_safety, "_DIR_FD_OK", False)

    def unexpected_anchored_open(*args, **kwargs):
        raise AssertionError("_DIR_FD_OK=False must select the path fallback")

    monkeypatch.setattr(path_safety, "open_anchored_fd", unexpected_anchored_open)
    assert [entry["name"] for entry in file_access.list_dir(tmp_path)] == ["note.txt"]


def test_escape_reads_use_navigation_and_file_owners(tmp_path, monkeypatch):
    resolved = {
        "external_root": tmp_path,
        "external_rel": "outside.txt",
        "request_path": "escape/outside.txt",
    }
    monkeypatch.setattr(
        navigation,
        "resolve_authorized_escape_request",
        lambda *args: resolved,
    )
    monkeypatch.setattr(
        file_access,
        "read_file_content",
        lambda root, rel: {"path": rel, "content": "owner", "size": 5, "lines": 1},
    )

    payload = navigation.read_authorized_escape_file_content(
        tmp_path,
        "session",
        "token",
        "escape/outside.txt",
    )

    assert payload["content"] == "owner"
    assert payload["path"] == "escape/outside.txt"
    assert payload["escape_read_only"] is True


def test_escape_grant_state_has_one_owner():
    assert "_ESCAPE_AUTH_TOKENS" not in vars(workspace)
    assert "_ESCAPE_AUTH_LOCK" not in vars(workspace)
    assert isinstance(navigation._ESCAPE_AUTH_TOKENS, dict)
    assert navigation._ESCAPE_AUTH_LOCK is not None
