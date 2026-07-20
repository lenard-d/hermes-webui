"""Containment and delivery planning for local media, files, and folders."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from api import config as media_config
from api import profiles as media_profiles
from api.config import MAX_FILE_BYTES
from api.helpers import safe_resolve
from api.media.preview import (
    SESSION_MEDIA_TOKEN_TYPES,
    PreviewPolicy,
    mime_for_path,
    preview_policy,
)
from api.sessions import get_session
from api.workspace import get_last_workspace, open_anchored_fd


@dataclass(frozen=True)
class LocalMediaPlan:
    target: Path
    anchor_root: Path
    preview: PreviewPolicy


@dataclass(frozen=True)
class RawFilePlan:
    target: Path
    anchor_root: Path
    preview: PreviewPolicy


@dataclass(frozen=True)
class FolderDownloadEntry:
    path: Path
    archive_name: str


@dataclass(frozen=True)
class FolderDownloadPlan:
    target: Path
    workspace_root: Path
    entries: tuple[FolderDownloadEntry, ...]
    total_bytes: int
    limit_hit: str | None


MEDIA_TOKEN_RE = __import__("re").compile(r"MEDIA:([^\s\)\]]+)")


def message_content_text(content) -> str:
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "") if isinstance(part, dict) else str(part or "")
            for part in content
        )
    return str(content or "")


def session_media_token_allows_path(
    session_id: str,
    target: Path,
    allowed_mimes: set[str] | frozenset[str],
    *,
    session_loader=get_session,
) -> bool:
    """Allow exact safe MEDIA paths already emitted in the requested session."""
    session_id = str(session_id or "").strip()
    if not session_id or mime_for_path(target) not in allowed_mimes:
        return False
    try:
        target_resolved = target.resolve()
        session = session_loader(session_id)
    except Exception:
        return False
    for message in getattr(session, "messages", []) or []:
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").strip().lower() == "user":
            continue
        text = message_content_text(message.get("content"))
        if "MEDIA:" not in text:
            continue
        for reference in MEDIA_TOKEN_RE.findall(text):
            if "://" in reference:
                continue
            try:
                if Path(reference).expanduser().resolve() == target_resolved:
                    return True
            except Exception:
                continue
    return False


def path_is_within_root(child: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([str(child), str(root)]) == str(root)
    except ValueError:
        return False


def _normalized_path(path: Path) -> str:
    return os.path.normcase(str(path.resolve())).casefold()


def _within_case_insensitive(child: Path, root: Path) -> bool:
    try:
        child_normalized = _normalized_path(child)
        root_normalized = _normalized_path(root)
        return os.path.commonpath([child_normalized, root_normalized]) == root_normalized
    except (ValueError, OSError):
        return False


def _equal_case_insensitive(left: Path, right: Path) -> bool:
    try:
        return _normalized_path(left) == _normalized_path(right)
    except (ValueError, OSError):
        return False


_DENY_FILENAMES = frozenset({
    "settings.json", "state.db", "state.db-wal", "state.db-shm",
    "auth.json", "auth.lock", "config.yaml", "config.yml", ".env",
    ".signing_key", ".pbkdf2_key", ".sessions.json",
    "google_token.json", "google_client_secret.json",
    "gateway_state.json", "channel_directory.json", "jobs.json",
    "passkeys.json", ".passkey_challenges.json", ".login_attempts.json",
})
_DENY_SUBDIRS = ("sessions", "memories", "cron", "logs", "checkpoints", "backups")
_DENY_TMP_SUFFIXES = (
    ".sessions.tmp", ".login_attempts.tmp", ".passkeys.tmp", ".passkey_challenges.tmp",
)


def _hermes_roots(home: Path, hermes_home: Path) -> list[Path]:
    roots: list[Path] = []
    for candidate in (
        hermes_home.resolve(),
        (home / ".hermes").resolve(),
        Path(media_profiles._DEFAULT_HERMES_HOME).resolve(),
        Path(media_config.STATE_DIR).resolve(),
    ):
        if candidate not in roots:
            roots.append(candidate)
    profile_roots: list[Path] = []
    for root in list(roots):
        profiles_dir = root / "profiles"
        try:
            if profiles_dir.is_dir():
                for child in profiles_dir.iterdir():
                    if child.is_dir():
                        resolved = child.resolve()
                        if resolved not in roots and resolved not in profile_roots:
                            profile_roots.append(resolved)
        except OSError:
            continue
    roots.extend(profile_roots)
    return roots


def _safe_workspace_carveout(workspace: Path | None, home: Path, roots: list[Path]) -> bool:
    if workspace is None or _equal_case_insensitive(workspace, home):
        return False
    for root in roots:
        if _equal_case_insensitive(workspace, root) or _within_case_insensitive(root, workspace):
            return False
    return workspace.name not in _DENY_SUBDIRS and workspace.name != "profiles" and workspace.parent.name != "profiles"


def _sensitive_media_path_denied(
    target: Path,
    *,
    home: Path,
    hermes_roots: list[Path],
    active_workspace: Path | None,
) -> bool:
    deny_directories = [
        child
        for root in hermes_roots
        for child in (
            *(root / sub for sub in _DENY_SUBDIRS),
            *((root / "webui_state" / sub) for sub in _DENY_SUBDIRS),
        )
    ]
    if any(_within_case_insensitive(target, directory.resolve()) for directory in deny_directories):
        return True
    in_workspace = (
        _safe_workspace_carveout(active_workspace, home, hermes_roots)
        and active_workspace is not None
        and _within_case_insensitive(target, active_workspace)
    )
    if in_workspace:
        return False
    under_hermes_root = any(_within_case_insensitive(target, root) for root in hermes_roots)
    name = target.name.casefold()
    return under_hermes_root and (name in _DENY_FILENAMES or name.endswith(_DENY_TMP_SUFFIXES))


def _active_workspace(workspace_getter=get_last_workspace) -> Path | None:
    try:
        workspace = Path(workspace_getter()).resolve()
        return workspace if workspace.is_dir() else None
    except Exception:
        return None


def resolve_local_media(
    raw_path: str,
    *,
    session_id: str = "",
    inline_requested: bool = False,
    workspace_getter=get_last_workspace,
    session_loader=get_session,
) -> LocalMediaPlan:
    """Authorize and plan delivery for ``/api/media`` without writing HTTP."""
    if not str(raw_path or "").strip():
        raise ValueError("path parameter required")
    try:
        target = Path(raw_path).expanduser().resolve()
    except Exception:
        raise ValueError("Invalid path") from None
    home = Path(os.path.expanduser("~")).resolve()
    hermes_home = Path(os.getenv("HERMES_HOME", str(home / ".hermes"))).expanduser().resolve()
    workspace = _active_workspace(workspace_getter)
    allowed_roots = [hermes_home, Path("/tmp").resolve(), (home / ".hermes").resolve()]
    if workspace is not None:
        allowed_roots.append(workspace)
    for raw_root in os.environ.get("MEDIA_ALLOWED_ROOTS", "").split(os.pathsep):
        if not raw_root.strip():
            continue
        try:
            root = Path(raw_root.strip()).expanduser().resolve()
            if root.is_dir():
                allowed_roots.append(root)
        except Exception:
            continue
    matching_roots = [root for root in allowed_roots if root.exists() and path_is_within_root(target, root)]
    token_allowed = session_media_token_allows_path(
        session_id,
        target,
        SESSION_MEDIA_TOKEN_TYPES,
        session_loader=session_loader,
    )
    roots = _hermes_roots(home, hermes_home)
    if _sensitive_media_path_denied(
        target,
        home=home,
        hermes_roots=roots,
        active_workspace=workspace,
    ):
        raise PermissionError("Path not in allowed location")
    if not matching_roots and not token_allowed:
        raise PermissionError("Path not in allowed location")
    if not target.exists() or not target.is_file():
        raise FileNotFoundError("not found")
    anchor_root = max(matching_roots, key=lambda path: len(path.parts)) if matching_roots else target.parent.resolve()
    return LocalMediaPlan(
        target=target,
        anchor_root=anchor_root,
        preview=preview_policy(target, inline_requested=inline_requested, local_media=True),
    )


def resolve_raw_file(
    session,
    session_id: str,
    relative_path: str,
    *,
    inline_requested: bool = False,
    force_download: bool = False,
) -> RawFilePlan:
    """Resolve workspace or per-session attachment delivery with one anchor."""
    workspace_root = Path(session.workspace).resolve()
    try:
        target = safe_resolve(workspace_root, relative_path)
    except ValueError:
        target = None
    anchor_root = workspace_root
    if target is None or not target.exists() or not target.is_file():
        from api.media.uploads import session_attachment_dir

        anchor_root = session_attachment_dir(session_id).resolve()
        try:
            target = safe_resolve(anchor_root, relative_path)
        except ValueError:
            target = None
    if target is None or not target.exists() or not target.is_file():
        raise FileNotFoundError("not found")
    return RawFilePlan(
        target=target,
        anchor_root=anchor_root,
        preview=preview_policy(
            target,
            inline_requested=inline_requested,
            force_download=force_download,
        ),
    )


def folder_zip_max_bytes() -> int:
    try:
        megabytes = int(os.getenv("HERMES_WEBUI_FOLDER_ZIP_MAX_MB", "1024"))
    except ValueError:
        megabytes = 1024
    return max(1, megabytes) * 1024 * 1024


def folder_zip_max_files() -> int:
    try:
        return max(1, int(os.getenv("HERMES_WEBUI_FOLDER_ZIP_MAX_FILES", "50000")))
    except ValueError:
        return 50000


def collect_folder_download(
    target: Path,
    workspace_root: Path,
    max_bytes: int,
    max_files: int,
) -> FolderDownloadPlan:
    """Preflight one contained folder download without opening response output."""
    entries: list[FolderDownloadEntry] = []
    total_bytes = 0
    limit_hit = None
    for raw_root, directories, names in os.walk(target, followlinks=False):
        root = Path(raw_root)
        try:
            if not root.resolve().is_relative_to(workspace_root):
                directories[:] = []
                continue
        except (ValueError, OSError):
            directories[:] = []
            continue
        for name in names:
            path = root / name
            if path.is_symlink():
                try:
                    if not path.resolve().is_relative_to(workspace_root):
                        continue
                except (ValueError, OSError):
                    continue
            try:
                size = path.stat().st_size
                archive_name = str(path.relative_to(target))
            except (OSError, ValueError):
                continue
            if len(entries) >= max_files:
                limit_hit = "max_files"
                break
            if total_bytes + size > max_bytes:
                limit_hit = "max_bytes"
                break
            entries.append(FolderDownloadEntry(path, archive_name))
            total_bytes += size
        if limit_hit:
            break
    return FolderDownloadPlan(target, workspace_root, tuple(entries), total_bytes, limit_hit)


def open_anchored_regular_file(anchor_root: Path, target: Path) -> int:
    """Open the exact contained regular file selected by a delivery plan."""
    fd = open_anchored_fd(anchor_root, target.resolve(), want_dir=False)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise FileNotFoundError(f"Not a file: {target}")
        return fd
    except Exception:
        os.close(fd)
        raise


def read_anchored_file_bytes(workspace_root: Path, target: Path) -> bytes:
    fd = open_anchored_regular_file(workspace_root, target)
    with os.fdopen(fd, "rb", closefd=True) as handle:
        size = os.fstat(handle.fileno()).st_size
        if size > MAX_FILE_BYTES:
            raise ValueError(f"File too large ({size} bytes, max {MAX_FILE_BYTES})")
        return handle.read(MAX_FILE_BYTES + 1)
