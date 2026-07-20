"""Architecture contract for the importable workspace domain modules."""

import subprocess
import sys

from api import workspace
from api.workspace_parts.bindings import workspace_api
from api.workspace_parts import escape_navigation, file_access, git_summary, path_safety


def test_path_safety_imports_without_workspace_facade():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import api.workspace_parts.path_safety; "
            "assert 'api.workspace' not in sys.modules",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_file_access_imports_without_workspace_facade():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import api.workspace_parts.file_access; "
            "assert 'api.workspace' not in sys.modules",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_workspace_facade_reexports_domain_implementations():
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

    file_exports = ("list_dir", "dir_signature", "read_file_content")
    for name in file_exports:
        assert getattr(workspace, name) is getattr(file_access, name)

    escape_exports = (
        "EscapeAuthorizationExpiredError",
        "authorize_escape_target",
        "resolve_authorized_escape_request",
        "list_authorized_escape_dir",
        "read_authorized_escape_file_content",
        "raw_authorized_escape_target",
    )
    for name in escape_exports:
        assert getattr(workspace, name) is getattr(escape_navigation, name)

    assert workspace.git_info_for_workspace is git_summary.git_info_for_workspace


def test_parts_resolve_the_canonical_workspace_facade():
    assert workspace_api() is workspace


def test_file_access_honors_facade_monkeypatches(tmp_path, monkeypatch):
    target = tmp_path / "note.txt"
    target.write_text("hello", encoding="utf-8")
    calls = []

    def tracking_resolve(root, rel):
        calls.append((root, rel))
        return path_safety.safe_resolve_ws(root, rel)

    monkeypatch.setattr(workspace, "safe_resolve_ws", tracking_resolve)
    assert workspace.read_file_content(tmp_path, "note.txt")["content"] == "hello"
    assert calls == [(tmp_path, "note.txt")]


def test_file_access_honors_facade_dir_fd_switch(tmp_path, monkeypatch):
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setattr(workspace, "_DIR_FD_OK", False)

    def unexpected_anchored_open(*args, **kwargs):
        raise AssertionError("facade _DIR_FD_OK=False must select the path fallback")

    monkeypatch.setattr(workspace, "open_anchored_fd", unexpected_anchored_open)
    assert [entry["name"] for entry in workspace.list_dir(tmp_path)] == ["note.txt"]


def test_escape_reads_honor_facade_monkeypatches(tmp_path, monkeypatch):
    resolved = {
        "external_root": tmp_path,
        "external_rel": "outside.txt",
        "request_path": "escape/outside.txt",
    }
    monkeypatch.setattr(
        workspace,
        "resolve_authorized_escape_request",
        lambda *args: resolved,
    )
    monkeypatch.setattr(
        workspace,
        "read_file_content",
        lambda root, rel: {"path": rel, "content": "facade", "size": 6, "lines": 1},
    )

    payload = workspace.read_authorized_escape_file_content(
        tmp_path,
        "session",
        "token",
        "escape/outside.txt",
    )

    assert payload["content"] == "facade"
    assert payload["path"] == "escape/outside.txt"
    assert payload["escape_read_only"] is True


def test_escape_grant_state_has_one_owner():
    assert "_ESCAPE_AUTH_TOKENS" not in vars(workspace)
    assert "_ESCAPE_AUTH_LOCK" not in vars(workspace)
    assert isinstance(escape_navigation._ESCAPE_AUTH_TOKENS, dict)
    assert escape_navigation._ESCAPE_AUTH_LOCK is not None
