"""Workspace file mutation and local-navigation route domain."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from api.helpers import _sanitize_error, bad, j, require, safe_resolve
    from api.sessions.store import get_session_for_file_ops
    from api.routes import _read_anchored_file_bytes
    from api.workspace import (
        make_anchored_dir,
        open_anchored_create_fd,
        open_anchored_fd,
        open_anchored_write_fd,
        rename_anchored,
        rmtree_anchored,
        unlink_anchored,
    )

def _handle_file_delete(handler, body):
    try:
        require(body, "session_id", "path")
    except ValueError as e:
        return bad(handler, str(e))
    try:
        s = get_session_for_file_ops(body["session_id"])
    except KeyError:
        return bad(handler, "Session not found", 404)
    try:
        ws_root = Path(s.workspace)
        target = safe_resolve(ws_root, body["path"])
        # Reject a symlinked entry BEFORE the follow-based exists() check: a
        # dangling symlink resolves to a missing target, so an exists()-first
        # order would misclassify it as 404 "File not found" and leave it
        # permanently undeletable. is_symlink() is a no-follow lstat on the
        # lexically-requested path, so it catches both live and dangling links.
        if (ws_root / body["path"]).is_symlink():
            return bad(handler, "Cannot delete a symlinked entry")
        if not target.exists():
            return bad(handler, "File not found", 404)
        if target.is_dir():
            if not body.get("recursive"):
                return bad(handler, "Set recursive=true to delete directories")
            rmtree_anchored(ws_root, target)
        else:
            unlink_anchored(ws_root, target)
        return j(handler, {"ok": True, "path": body["path"]})
    except (ValueError, FileNotFoundError, PermissionError, OSError) as e:
        return bad(handler, _sanitize_error(e))


def _handle_file_save(handler, body):
    try:
        require(body, "session_id", "path")
    except ValueError as e:
        return bad(handler, str(e))
    try:
        s = get_session_for_file_ops(body["session_id"])
    except KeyError:
        return bad(handler, "Session not found", 404)
    try:
        ws_root = Path(s.workspace)
        target = safe_resolve(ws_root, body["path"])
        if (ws_root / body["path"]).is_symlink():
            return bad(handler, "Cannot save to a symlinked entry")
        if not target.exists():
            return bad(handler, "File not found", 404)
        if target.is_dir():
            return bad(handler, "Cannot save: path is a directory")
        if Path(str(body["path"])).suffix.lower() in {".docx", ".xlsx", ".pptx"}:
            return bad(handler, "Use /api/file/office-save for Office documents")
        data = str(body.get("content", "")).encode("utf-8")
        fd = open_anchored_write_fd(ws_root, target)
        with os.fdopen(fd, "wb", closefd=True) as fh:
            fh.write(data)
        return j(
            handler, {"ok": True, "path": body["path"], "size": len(data)}
        )
    except (ValueError, FileNotFoundError, PermissionError, OSError) as e:
        return bad(handler, _sanitize_error(e))


def _handle_office_file_save(handler, body):
    try:
        require(body, "session_id", "path")
    except ValueError as e:
        return bad(handler, str(e))
    try:
        s = get_session_for_file_ops(body["session_id"])
    except KeyError:
        return bad(handler, "Session not found", 404)
    try:
        ws_root = Path(s.workspace)
        target = safe_resolve(ws_root, body["path"])
        if (ws_root / body["path"]).is_symlink():
            return bad(handler, "Cannot save to a symlinked entry")
        if not target.exists():
            return bad(handler, "File not found", 404)
        if target.is_dir():
            return bad(handler, "Cannot save: path is a directory")
        if Path(str(body["path"])).suffix.lower() not in {".docx", ".xlsx", ".pptx"}:
            return bad(handler, "Office save is only available for .docx, .xlsx, and .pptx files")
        from api.office_documents import save_office_document

        current_bytes = _read_anchored_file_bytes(ws_root, target)
        preview, updated_bytes = save_office_document(body["path"], current_bytes, body.get("content", ""))
        fd = open_anchored_write_fd(ws_root, target)
        with os.fdopen(fd, "wb", closefd=True) as fh:
            fh.write(updated_bytes)
        preview.update({"ok": True, "path": body["path"], "size": len(updated_bytes)})
        return j(handler, preview)
    except ImportError as e:
        return bad(handler, str(e), 503)
    except (ValueError, FileNotFoundError, PermissionError, OSError) as e:
        return bad(handler, _sanitize_error(e))


def _handle_file_create(handler, body):
    try:
        require(body, "session_id", "path")
    except ValueError as e:
        return bad(handler, str(e))
    try:
        s = get_session_for_file_ops(body["session_id"])
    except KeyError:
        return bad(handler, "Session not found", 404)
    try:
        ws_root = Path(s.workspace)
        target = safe_resolve(ws_root, body["path"])
        if target.exists():
            return bad(handler, "File already exists")
        data = str(body.get("content", "")).encode("utf-8")
        fd = open_anchored_create_fd(ws_root, target)
        with os.fdopen(fd, "wb", closefd=True) as fh:
            fh.write(data)
        return j(
            handler, {"ok": True, "path": target.relative_to(ws_root.resolve()).as_posix()}
        )
    except FileExistsError:
        return bad(handler, "File already exists")
    except (ValueError, FileNotFoundError, PermissionError, OSError) as e:
        return bad(handler, _sanitize_error(e))


def _handle_file_rename(handler, body):
    try:
        require(body, "session_id", "path", "new_name")
    except ValueError as e:
        return bad(handler, str(e))
    try:
        s = get_session_for_file_ops(body["session_id"])
    except KeyError:
        return bad(handler, "Session not found", 404)
    try:
        ws_root = Path(s.workspace)
        ws_root_resolved = ws_root.resolve()
        source = safe_resolve(ws_root, body["path"])
        # Reject a symlinked entry BEFORE the follow-based exists() check (see
        # _handle_file_delete): a dangling symlink would otherwise 404 and stay
        # unrenameable. is_symlink() is a no-follow lstat on the requested path.
        if (ws_root / body["path"]).is_symlink():
            return bad(handler, "Cannot rename a symlinked entry")
        if not source.exists():
            return bad(handler, "File not found", 404)
        new_name = body["new_name"].strip()
        if not new_name or "/" in new_name or "\\" in new_name or ".." in new_name:
            return bad(handler, "Invalid file name")
        dest = source.parent / new_name
        if dest.exists():
            return bad(handler, f'A file named "{new_name}" already exists')
        rename_anchored(ws_root, source, dest)
        new_rel = dest.relative_to(ws_root_resolved).as_posix()
        return j(handler, {"ok": True, "old_path": body["path"], "new_path": new_rel})
    except FileExistsError:
        return bad(handler, f'A file named "{body.get("new_name", "")}" already exists')
    except (ValueError, PermissionError, OSError) as e:
        return bad(handler, _sanitize_error(e))


def _handle_file_move(handler, body):
    try:
        require(body, "session_id", "path", "dest_dir")
    except ValueError as e:
        return bad(handler, str(e))
    try:
        s = get_session_for_file_ops(body["session_id"])
    except KeyError:
        return bad(handler, "Session not found", 404)
    try:
        ws_root = Path(s.workspace)
        # safe_resolve() returns paths under the RESOLVED root, so compute
        # returned relative paths against the resolved root too — otherwise a
        # symlinked workspace root (e.g. macOS /tmp -> /private/tmp) makes
        # dest.relative_to(ws_root) raise after a successful on-disk move,
        # returning a confusing 400 for a move that actually happened.
        ws_root_resolved = ws_root.resolve()
        source = safe_resolve(ws_root, body["path"])
        # Reject a symlinked SOURCE entry BEFORE the follow-based exists() check.
        # safe_resolve() follows the final symlink, so source.name/source.parent
        # would point at the link's TARGET, not the dragged entry — moving
        # link.txt would silently move dir/real.txt and leave link.txt dangling.
        # Detect the symlink on the lexically-requested final component (lstat,
        # no-follow) and refuse; running this before exists() also means a
        # dangling symlink is rejected (400) rather than misclassified as 404
        # (matches the delete/rename ordering).
        if (ws_root / body["path"]).is_symlink():
            return bad(handler, "Cannot move a symlinked entry")
        if not source.exists():
            return bad(handler, "File not found", 404)
        dest_dir_raw = (body.get("dest_dir") or ".").strip()
        if not dest_dir_raw:
            dest_dir_raw = "."
        if ".." in dest_dir_raw.split("/"):
            return bad(handler, "Invalid destination")
        dest_parent = safe_resolve(ws_root, dest_dir_raw)
        if not dest_parent.is_dir():
            return bad(handler, "Destination folder not found", 404)
        if source.is_dir():
            try:
                dest_parent.resolve().relative_to(source.resolve())
                return bad(handler, "Cannot move a folder into itself or its subfolder")
            except ValueError:
                pass
        dest = dest_parent / source.name
        if dest.resolve() == source.resolve():
            new_rel = source.relative_to(ws_root_resolved).as_posix()
            return j(
                handler,
                {"ok": True, "old_path": body["path"], "new_path": new_rel},
            )
        # Perform the move race-safely. The path-based checks above can be raced
        # (TOCTOU): between validating dest_parent and renaming, dest_dir could be
        # swapped to a symlink pointing outside the workspace, and a path-based
        # rename would follow it. Open BOTH parent directories as workspace-anchored
        # fds (openat + O_NOFOLLOW — every component verified non-symlink), do the
        # collision check by fd, then rename via src_dir_fd/dst_dir_fd so the kernel
        # operates on the verified directories, not re-resolved pathnames.
        leaf = source.name
        if os.open in getattr(os, "supports_dir_fd", set()):
            src_parent_fd = open_anchored_fd(ws_root, source.parent, want_dir=True)
            try:
                dst_parent_fd = open_anchored_fd(ws_root, dest_parent, want_dir=True)
                try:
                    try:
                        os.stat(leaf, dir_fd=dst_parent_fd, follow_symlinks=False)
                        return bad(
                            handler,
                            f'A file named "{leaf}" already exists in that folder',
                        )
                    except FileNotFoundError:
                        pass
                    os.rename(
                        leaf, leaf,
                        src_dir_fd=src_parent_fd, dst_dir_fd=dst_parent_fd,
                    )
                finally:
                    os.close(dst_parent_fd)
            finally:
                os.close(src_parent_fd)
        else:
            # Windows / no openat: no new race protection available, but creating
            # symlinks needs admin there. Fall back to the path-based rename.
            if dest.exists():
                return bad(
                    handler,
                    f'A file named "{source.name}" already exists in that folder',
                )
            source.rename(dest)
        new_rel = dest.relative_to(ws_root_resolved).as_posix()
        return j(
            handler,
            {"ok": True, "old_path": body["path"], "new_path": new_rel},
        )
    except (ValueError, FileNotFoundError, PermissionError, OSError) as e:
        return bad(handler, _sanitize_error(e))


def _handle_create_dir(handler, body):
    try:
        require(body, "session_id", "path")
    except ValueError as e:
        return bad(handler, str(e))
    try:
        s = get_session_for_file_ops(body["session_id"])
    except KeyError:
        return bad(handler, "Session not found", 404)
    try:
        ws_root = Path(s.workspace)
        target = safe_resolve(ws_root, body["path"])
        if target.exists():
            return bad(handler, "Path already exists")
        make_anchored_dir(ws_root, target)
        return j(
            handler, {"ok": True, "path": target.relative_to(ws_root.resolve()).as_posix()}
        )
    except (ValueError, FileNotFoundError, PermissionError, OSError) as e:
        return bad(handler, _sanitize_error(e))


def _handle_file_reveal(handler, body):
    try:
        require(body, "session_id", "path")
    except ValueError as e:
        return bad(handler, str(e))
    try:
        s = get_session_for_file_ops(body["session_id"])
    except KeyError:
        return bad(handler, "Session not found", 404)
    try:
        target = safe_resolve(Path(s.workspace), body["path"])
        if not target.exists():
            # Include the resolved server-side path in the error message so
            # the frontend toast can show *which* file the system expected.
            # Useful when a stale session row still references a deleted file
            # (#1764 — Cygnus's screenshot showed a "Failed to reveal: not
            # found" toast that dropped the path entirely, leaving no clue
            # what was missing).
            return bad(handler, f"File not found: {target}", 404)

        target_str = str(target)

        # Optional Docker host/container path translation (mirrors _handle_file_open_vscode).
        from api.config import get_config as _get_cfg  # noqa: PLC0415
        vscode_cfg = _get_cfg().get("vscode", {})
        if not isinstance(vscode_cfg, dict):
            vscode_cfg = {}
        container_prefix = vscode_cfg.get("container_path_prefix", "")
        host_prefix = vscode_cfg.get("host_path_prefix", "")
        if container_prefix and host_prefix:
            _norm = container_prefix.rstrip('/') + '/'
            if target_str.startswith(_norm) or target_str == container_prefix.rstrip('/'):
                target_str = host_prefix + target_str[len(container_prefix):]

        system = platform.system()
        if system == "Darwin":
            subprocess.Popen(["open", "-R", target_str])
        elif system == "Windows":
            subprocess.Popen(["explorer.exe", "/select," + target_str])
        else:
            # Linux / other — open parent directory
            subprocess.Popen(["xdg-open", str(Path(target_str).parent)])

        return j(handler, {"ok": True, "path": body["path"]})
    except (ValueError, PermissionError, OSError) as e:
        return bad(handler, _sanitize_error(e))


def _handle_file_path(handler, body):
    """Resolve a relative workspace-rooted path into an absolute on-disk path.

    The right-click "Copy file path" action (#1764) wants to put the
    absolute path on the user's clipboard so they can paste it into a
    terminal, editor, or anywhere else without having to round-trip through
    the OS file browser. The frontend can't compute the absolute path on
    its own — `safe_resolve` joins against the session's workspace root
    which only the server knows. The handler here is a thin lookup; no
    filesystem mutation, no OS-specific dispatch. We do NOT require the
    target to exist (unlike `_handle_file_reveal`) — copying the path of a
    just-deleted file is still useful, and refusing would force callers
    to special-case 404s for an action that cannot fail destructively.
    """
    try:
        require(body, "session_id", "path")
    except ValueError as e:
        return bad(handler, str(e))
    try:
        s = get_session_for_file_ops(body["session_id"])
    except KeyError:
        return bad(handler, "Session not found", 404)
    try:
        target = safe_resolve(Path(s.workspace), body["path"])
        return j(handler, {"ok": True, "path": str(target)})
    except (ValueError, PermissionError, OSError) as e:
        return bad(handler, _sanitize_error(e))


def _handle_file_open_vscode(handler, body):
    """Open a workspace file or folder in VS Code (#2735).

    Reads optional ``vscode`` config block from config.yaml:

        vscode:
          command: code          # executable on PATH; defaults to "code"
          host_path_prefix: /home/user/projects       # Docker host path
          container_path_prefix: /app/workspace       # matching container path

    If ``host_path_prefix`` and ``container_path_prefix`` are both set,
    paths that begin with ``container_path_prefix`` are translated to the
    host prefix before being handed to VS Code.  This lets users running
    Hermes WebUI inside Docker still open files in their local editor.
    """
    try:
        require(body, "session_id", "path")
    except ValueError as e:
        return bad(handler, str(e))
    try:
        s = get_session_for_file_ops(body["session_id"])
    except KeyError:
        return bad(handler, "Session not found", 404)
    try:
        target = safe_resolve(Path(s.workspace), body["path"])
        if not target.exists():
            return bad(handler, f"File not found: {target}", 404)

        target_str = str(target)

        # Optional Docker host/container path translation
        from api.config import get_config as _get_cfg  # noqa: PLC0415
        vscode_cfg = _get_cfg().get("vscode", {})
        if not isinstance(vscode_cfg, dict):
            vscode_cfg = {}
        container_prefix = vscode_cfg.get("container_path_prefix", "")
        host_prefix = vscode_cfg.get("host_path_prefix", "")
        if container_prefix and host_prefix:
            _norm = container_prefix.rstrip('/') + '/'
            if target_str.startswith(_norm) or target_str == container_prefix.rstrip('/'):
                target_str = host_prefix + target_str[len(container_prefix):]

        cmd = vscode_cfg.get("command", "code")
        # Resolve the command to an absolute path so subprocess.Popen finds it
        # even when the server process inherits a minimal PATH (e.g. when
        # launched via start.sh on macOS where /usr/local/bin may be absent).
        resolved_cmd = shutil.which(cmd)
        if resolved_cmd is None:
            # Try common VS Code installation paths as fallback.
            # macOS: /usr/local/bin/code (symlink) or app bundle CLI
            # Linux: /usr/bin/code or snap
            # Windows: user-install under %LOCALAPPDATA%, system-install under %PROGRAMFILES%
            _local_app_data = os.environ.get("LOCALAPPDATA", "")
            _prog_files = os.environ.get("PROGRAMFILES", "C:\\Program Files")
            _prog_files_x86 = os.environ.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)")
            _vscode_fallbacks = [
                # macOS
                "/usr/local/bin/code",
                "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
                # Linux
                "/usr/bin/code",
                "/snap/bin/code",
                # Windows (user install)
                os.path.join(_local_app_data, "Programs", "Microsoft VS Code", "bin", "code.cmd"),
                # Windows (system install)
                os.path.join(_prog_files, "Microsoft VS Code", "bin", "code.cmd"),
                os.path.join(_prog_files_x86, "Microsoft VS Code", "bin", "code.cmd"),
            ]
            for fb in _vscode_fallbacks:
                if fb and Path(fb).exists():
                    resolved_cmd = fb
                    break
        if resolved_cmd is None:
            return bad(
                handler,
                f"VS Code command not found: {cmd!r}. "
                "Install VS Code and ensure the 'code' CLI is on PATH, "
                "or set vscode.command in config.yaml to the full path.",
            )
        subprocess.Popen([resolved_cmd, target_str])

        return j(handler, {"ok": True, "path": body["path"]})
    except (ValueError, PermissionError, OSError) as e:
        return bad(handler, _sanitize_error(e))


__routes_exports__ = (
    "_handle_file_delete",
    "_handle_file_save",
    "_handle_office_file_save",
    "_handle_file_create",
    "_handle_file_rename",
    "_handle_file_move",
    "_handle_create_dir",
    "_handle_file_reveal",
    "_handle_file_path",
    "_handle_file_open_vscode",
)
