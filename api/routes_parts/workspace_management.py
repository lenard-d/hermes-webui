"""Workspace registration, naming, removal, and ordering route domain."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from api.helpers import _sanitize_error, bad, j
    from api.workspace import (
        _home_path,
        _is_blocked_system_path,
        _is_within,
        _strip_surrounding_quotes,
        load_workspaces,
        save_workspaces,
        validate_workspace_to_add,
    )


def _handle_workspace_add(handler, body):
    # Strip surrounding paired quotes BEFORE any further processing — macOS
    # Finder's "Copy as Pathname" wraps paths in single quotes, and users
    # routinely paste those quoted strings into the Add Space input.
    # Doing this at the route entry means every downstream check (blocked
    # system path, validate_workspace_to_add, duplicate detection) sees the
    # cleaned form.
    path_str = _strip_surrounding_quotes(body.get("path", "").strip())
    name = body.get("name", "").strip()
    auto_create = body.get("create", False)
    if not path_str:
        return bad(handler, "path is required")
    # Validate the path is NOT a blocked system root BEFORE any filesystem mutation.
    # This prevents creating orphan directories on rejected paths (#782 review).
    # _is_blocked_system_path honours user-tmp carve-outs (e.g. /var/folders on
    # macOS) so pytest's tmp_path_factory paths and other legit user-tmp dirs
    # still register cleanly.
    try:
        candidate = Path(path_str).expanduser().resolve()
    except (ValueError, OSError, RuntimeError) as e:
        # Invalid path (e.g. embedded null byte) — fail closed with a clean 400
        # instead of letting .resolve() raise an uncaught 500.
        return bad(handler, f"Invalid path: {_sanitize_error(e)}")
    if _is_blocked_system_path(candidate):
        # Home-directory carve-out, mirroring the validators
        # (resolve_trusted_workspace / validate_workspace_to_add): a workspace
        # at or under the active user's home must stay allowed even when that
        # home lives under an otherwise-blocked root (e.g. systemd-homed
        # /var/home/<user>/...). Without this the route rejects valid
        # /var/home workspaces before validate_workspace_to_add()'s carve-out
        # can run.
        _home = _home_path()
        if not (_home != Path("/") and (candidate == _home or _is_within(candidate, _home))):
            return bad(handler, f"Path points to a system directory: {candidate}")
    # Now safe to create the directory if requested
    if auto_create:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except (OSError, PermissionError) as e:
            return bad(handler, f"Could not create directory: {_sanitize_error(e)}")
    # Full validation (exists, is_dir) — should pass now that dir exists
    try:
        p = validate_workspace_to_add(path_str)
    except ValueError as e:
        return bad(handler, str(e))
    wss = load_workspaces()
    if any(w["path"] == str(p) for w in wss):
        return bad(handler, "Workspace already in list")
    wss.append({"path": str(p), "name": name or p.name})
    save_workspaces(wss)
    return j(handler, {"ok": True, "workspaces": wss})


def _handle_workspace_remove(handler, body):
    path_str = body.get("path", "").strip()
    if not path_str:
        return bad(handler, "path is required")
    wss = load_workspaces()
    wss = [w for w in wss if w["path"] != path_str]
    save_workspaces(wss)
    return j(handler, {"ok": True, "workspaces": wss})


def _handle_workspace_rename(handler, body):
    path_str = body.get("path", "").strip()
    name = body.get("name", "").strip()
    if not path_str or not name:
        return bad(handler, "path and name are required")
    wss = load_workspaces()
    for w in wss:
        if w["path"] == path_str:
            w["name"] = name
            break
    else:
        return bad(handler, "Workspace not found", 404)
    save_workspaces(wss)
    return j(handler, {"ok": True, "workspaces": wss})


def _handle_workspace_reorder(handler, body):
    """Reorder workspaces by providing an ordered list of paths.

    Accepts {"paths": ["path1", "path2", ...]}. The workspaces list is
    rewritten so that entries appear in the given order. Any workspace
    not included in the request is appended at the end (preserves data).
    """
    paths = body.get("paths", [])
    if not paths or not isinstance(paths, list):
        return bad(handler, "paths is required and must be a list")
    wss = load_workspaces()
    by_path = {w["path"]: w for w in wss}
    # Build reordered list: given order first, then any omitted entries
    reordered = []
    seen = set()
    for p in paths:
        p = p.strip()
        if p in by_path and p not in seen:
            reordered.append(by_path[p])
            seen.add(p)
    # Append any workspaces not mentioned (safety net)
    for w in wss:
        if w["path"] not in seen:
            reordered.append(w)
    save_workspaces(reordered)
    return j(handler, {"ok": True, "workspaces": reordered})


__routes_exports__ = (
    "_handle_workspace_add",
    "_handle_workspace_remove",
    "_handle_workspace_rename",
    "_handle_workspace_reorder",
)
