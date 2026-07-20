"""Short-lived, read-only grants for surfaced symlink escape targets.

The token registry is process-local state owned by this module. Grants are
bound to one session, workspace root, and unchanged symlink target, and every
read is re-anchored through the same descriptor-safe workspace file helpers.
"""

import posixpath
import secrets
import threading
import time
from pathlib import Path, PurePosixPath

from api.workspace_parts.bindings import workspace_api

_ESCAPE_AUTH_TTL_SECONDS = 300
_ESCAPE_AUTH_LOCK = threading.Lock()
_ESCAPE_AUTH_TOKENS: dict[str, dict[str, str | int | float]] = {}


def _normalize_workspace_rel_path(rel: str | Path) -> str:
    raw = workspace_api()._strip_surrounding_quotes(str(rel or "")).strip().replace("\\", "/")
    if not raw or raw == ".":
        return "."
    norm = posixpath.normpath(raw)
    if not norm or norm == ".":
        return "."
    if norm == ".." or norm.startswith("../") or norm.startswith("/"):
        raise ValueError(f"Path traversal blocked: {rel}")
    return norm


def _escape_virtual_path(root: str, rel: str) -> str:
    root_norm = _normalize_workspace_rel_path(root)
    rel_norm = _normalize_workspace_rel_path(rel)
    if root_norm == ".":
        return rel_norm
    if rel_norm == ".":
        return root_norm
    return f"{root_norm}/{rel_norm}"


def _escape_surface_target(workspace: Path, rel: str) -> tuple[Path, Path]:
    api = workspace_api()
    workspace_root = workspace.resolve()
    surface_rel = _normalize_workspace_rel_path(rel)
    surface_posix = PurePosixPath(surface_rel)
    parent_rel = str(surface_posix.parent)
    if parent_rel in ("", "."):
        parent_path = workspace_root
    else:
        parent_path = api.safe_resolve_ws(workspace_root, parent_rel)
    surface_path = parent_path / surface_posix.name
    if not surface_path.is_symlink():
        raise ValueError(f"Path is not an escape-target symlink: {rel}")
    target = surface_path.resolve()
    if not target.exists():
        raise ValueError(f"Path is no longer reachable: {rel}")
    try:
        target.relative_to(workspace_root)
    except ValueError:
        pass
    else:
        raise ValueError(f"Path does not escape workspace: {rel}")
    if api._is_blocked_system_path(target):
        raise ValueError(f"Path points to a system directory: {target}")
    return surface_path, target


def _escape_authorized_root(target: Path) -> tuple[Path, str]:
    resolved_target = target.resolve()
    if resolved_target.is_dir():
        return resolved_target, "."
    return resolved_target.parent, resolved_target.name


class EscapeAuthorizationExpiredError(ValueError):
    pass


def _escape_prune_tokens(now: float | None = None) -> None:
    cutoff = time.time() if now is None else now
    expired = [token for token, record in _ESCAPE_AUTH_TOKENS.items() if float(record.get("expires_at") or 0.0) <= cutoff]
    for token in expired:
        _ESCAPE_AUTH_TOKENS.pop(token, None)


def authorize_escape_target(workspace: Path, session_id: str, rel: str) -> dict:
    """Mint a short-lived browser-only grant for one surfaced escape-target symlink."""
    workspace_root = workspace.resolve()
    _surface_path, target = _escape_surface_target(workspace_root, rel)
    external_root, external_entry_rel = _escape_authorized_root(target)
    token = secrets.token_urlsafe(24)
    expires_at = time.time() + _ESCAPE_AUTH_TTL_SECONDS
    record = {
        "session_id": str(session_id or ""),
        "workspace_root": str(workspace_root),
        "surface_path": _normalize_workspace_rel_path(rel),
        "external_root": str(external_root),
        "external_entry_rel": external_entry_rel,
        "surface_target": str(target),
        "expires_at": expires_at,
    }
    with _ESCAPE_AUTH_LOCK:
        _escape_prune_tokens()
        _ESCAPE_AUTH_TOKENS[token] = record
    return {
        "token": token,
        "path": record["surface_path"],
        "is_dir": target.is_dir(),
        "expires_at": expires_at,
        "expires_in": _ESCAPE_AUTH_TTL_SECONDS,
        "read_only": True,
    }


def _escape_authorization_record(workspace: Path, session_id: str, token: str) -> dict:
    api = workspace_api()
    workspace_root = str(workspace.resolve())
    token = str(token or "").strip()
    if not token:
        raise ValueError("Escape authorization token is required")
    now = time.time()
    with _ESCAPE_AUTH_LOCK:
        _escape_prune_tokens(now)
        record = dict(_ESCAPE_AUTH_TOKENS.get(token) or {})
    if not record:
        raise EscapeAuthorizationExpiredError("Escape authorization expired")
    if str(record.get("session_id") or "") != str(session_id or ""):
        raise EscapeAuthorizationExpiredError("Escape authorization expired")
    if str(record.get("workspace_root") or "") != workspace_root:
        raise EscapeAuthorizationExpiredError("Escape authorization expired")
    surface_path = _normalize_workspace_rel_path(record.get("surface_path") or ".")
    surface_target = str(record.get("surface_target") or "")
    try:
        _surface, current_target = _escape_surface_target(Path(workspace_root), surface_path)
    except ValueError:
        raise EscapeAuthorizationExpiredError("Escape authorization expired") from None
    if str(current_target.resolve()) != surface_target:
        raise EscapeAuthorizationExpiredError("Escape authorization expired")
    if not current_target.exists() or api._is_blocked_system_path(current_target):
        raise EscapeAuthorizationExpiredError("Escape authorization expired")
    return record


def resolve_authorized_escape_request(workspace: Path, session_id: str, token: str, rel: str) -> dict:
    record = _escape_authorization_record(workspace, session_id, token)
    surface_path = _normalize_workspace_rel_path(record["surface_path"])
    request_path = _normalize_workspace_rel_path(rel)
    try:
        requested_rel = str(PurePosixPath(request_path).relative_to(PurePosixPath(surface_path)))
    except ValueError:
        raise ValueError(f"Path traversal blocked: {rel}") from None
    if not requested_rel:
        requested_rel = "."
    external_root = Path(str(record["external_root"]))
    external_entry_rel = _normalize_workspace_rel_path(record.get("external_entry_rel") or ".")
    if external_entry_rel == ".":
        external_rel = requested_rel
    elif requested_rel == ".":
        external_rel = external_entry_rel
    else:
        external_rel = str(PurePosixPath(external_entry_rel) / PurePosixPath(requested_rel))
    target = external_root / external_rel
    return {
        "record": record,
        "workspace_root": Path(str(record["workspace_root"])),
        "surface_path": surface_path,
        "request_path": request_path,
        "external_rel": external_rel,
        "external_root": external_root,
        "target": target,
    }


def list_authorized_escape_dir(workspace: Path, session_id: str, token: str, rel: str) -> dict:
    api = workspace_api()
    resolved = api.resolve_authorized_escape_request(workspace, session_id, token, rel)
    external_root = resolved["external_root"]
    external_rel = resolved["external_rel"]
    entries = api.list_dir(external_root, external_rel)
    surface_path = resolved["surface_path"]
    external_root_resolved = external_root.resolve()
    for entry in entries:
        entry["path"] = _escape_virtual_path(surface_path, entry.get("path") or ".")
        entry["escape_read_only"] = True
        target = entry.get("target")
        if not target:
            continue
        try:
            target_path = Path(str(target)).resolve()
            target_rel = target_path.relative_to(external_root_resolved).as_posix()
        except Exception:
            entry.pop("target", None)
            continue
        entry["target"] = _escape_virtual_path(surface_path, target_rel)
    return {
        "path": resolved["request_path"],
        "entries": entries,
        "signature": api.dir_signature(external_root, external_rel, entries),
        "virtual_root": surface_path,
        "read_only": True,
    }


def read_authorized_escape_file_content(workspace: Path, session_id: str, token: str, rel: str) -> dict:
    api = workspace_api()
    resolved = api.resolve_authorized_escape_request(workspace, session_id, token, rel)
    payload = api.read_file_content(resolved["external_root"], resolved["external_rel"])
    payload["path"] = resolved["request_path"]
    payload["escape_read_only"] = True
    return payload


def raw_authorized_escape_target(workspace: Path, session_id: str, token: str, rel: str) -> tuple[Path, Path]:
    api = workspace_api()
    resolved = api.resolve_authorized_escape_request(workspace, session_id, token, rel)
    target = api.safe_resolve_ws(resolved["external_root"], resolved["external_rel"])
    return resolved["external_root"], target


# ── Git detection ──────────────────────────────────────────────────────────
