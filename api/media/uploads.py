"""Upload storage, archive extraction, sidecars, and session visibility."""

from __future__ import annotations

import io
import mimetypes
import os
import random
import re
import string
import tarfile
import zipfile
from pathlib import Path

from api.config import MAX_UPLOAD_BYTES, STATE_DIR
from api.profiles import get_active_profile_name, profiles_match
from api.sessions import get_session, session_write_owner
from api.workspace import (
    make_anchored_dir,
    open_anchored_create_fd,
    resolve_trusted_workspace,
    rmtree_anchored,
    safe_resolve_ws,
    unlink_anchored,
)


ARCHIVE_SUFFIXES = (
    ".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz",
)
MAX_ARCHIVE_MEMBERS = 10_000


class SessionNotFound(KeyError):
    """Session is absent or deliberately hidden from the active profile."""


class UploadContainmentError(PermissionError):
    """Upload destination could not be proven contained at the point of use."""


class UploadConflict(FileExistsError):
    """A concurrent writer claimed the selected upload destination."""


def max_extracted_bytes() -> int:
    raw = os.getenv("HERMES_WEBUI_MAX_EXTRACTED_MB", "").strip()
    if raw:
        try:
            megabytes = float(raw)
            if megabytes > 0:
                return int(megabytes * 1024 * 1024)
        except ValueError:
            pass
    return 10 * MAX_UPLOAD_BYTES


MAX_EXTRACTED_BYTES = 10 * MAX_UPLOAD_BYTES


def sanitize_upload_name(filename: str) -> str:
    safe_name = re.sub(r"[^\w.\-]", "_", Path(filename).name)[:200]
    if not safe_name or safe_name.strip(".") == "":
        raise ValueError("Invalid filename")
    return safe_name


def attachment_root() -> Path:
    override = os.getenv("HERMES_WEBUI_ATTACHMENT_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (STATE_DIR / "attachments").resolve()


def session_attachment_dir(session_id: str, *, root: Path | None = None) -> Path:
    resolved_root = (root or attachment_root()).resolve()
    directory_name = re.sub(r"[^\w.\-]", "_", str(session_id or "session"))[:120]
    destination = (resolved_root / directory_name).resolve()
    if not destination.is_relative_to(resolved_root):
        raise ValueError("Invalid attachment directory")
    return destination


def upload_destination(session_id: str, safe_name: str) -> Path:
    """Return the first currently-unused legacy destination without reserving it."""
    directory = session_attachment_dir(session_id)
    directory.mkdir(parents=True, exist_ok=True)
    destination = (directory / safe_name).resolve()
    if not destination.is_relative_to(directory):
        raise ValueError("Invalid upload destination")
    if not destination.exists():
        return destination
    for index in range(1, 1000):
        candidate = (directory / f"{destination.stem}-{index}{destination.suffix}").resolve()
        if not candidate.is_relative_to(directory):
            raise ValueError("Invalid upload destination")
        if not candidate.exists():
            return candidate
    raise ValueError("Too many uploads with the same filename")


def session_visible_to_active_profile(session) -> bool:
    profile = getattr(session, "profile", None)
    if not isinstance(profile, str):
        profile = None
    return profiles_match(profile, get_active_profile_name())


def _visible_session(session_id: str):
    try:
        session = get_session(session_id)
    except KeyError:
        raise SessionNotFound(session_id) from None
    if not session_visible_to_active_profile(session):
        raise SessionNotFound(session_id)
    return session


def _candidate_names(safe_name: str):
    path = Path(safe_name)
    yield safe_name
    for index in range(1, 1000):
        yield f"{path.stem}-{index}{path.suffix}"


def _create_unique_file(root: Path, directory: Path, safe_name: str, body: bytes) -> Path:
    make_anchored_dir(root, directory)
    for candidate_name in _candidate_names(safe_name):
        destination = safe_resolve_ws(directory, candidate_name)
        try:
            fd = open_anchored_create_fd(root, destination.resolve())
        except FileExistsError:
            continue
        except (OSError, ValueError):
            raise UploadContainmentError(f"Path traversal blocked: {safe_name}") from None
        try:
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(body)
        except Exception:
            try:
                unlink_anchored(root, destination.resolve())
            except Exception:
                pass
            raise
        return destination
    raise ValueError("Too many uploads with the same filename")


def store_chat_attachment(session_id: str, filename: str, body: bytes) -> dict:
    """Persist one transient attachment while holding the session write owner."""
    if not filename:
        raise ValueError("No filename in upload")
    with session_write_owner(session_id) as session:
        if not session_visible_to_active_profile(session):
            raise SessionNotFound(session_id)
        return store_chat_attachment_for_session(session_id, filename, body)


def store_chat_attachment_for_session(
    session_id: str,
    filename: str,
    body: bytes,
    *,
    before_store=None,
) -> dict:
    """Persist bytes after the caller acquired and authorized the session."""
    if not filename:
        raise ValueError("No filename in upload")
    safe_name = sanitize_upload_name(filename)
    if before_store is not None:
        before_store(session_id, safe_name)
    root = attachment_root()
    root.mkdir(parents=True, exist_ok=True)
    destination = _create_unique_file(
        root,
        session_attachment_dir(session_id, root=root),
        safe_name,
        body,
    )
    mime = mimetypes.guess_type(destination.name)[0] or "application/octet-stream"
    return {
        "filename": destination.name,
        "path": str(destination),
        "size": destination.stat().st_size,
        "mime": mime,
        "is_image": mime.startswith("image/"),
    }


def _archive_mode(filename: str) -> str:
    lowered = Path(filename).name.lower()
    if lowered.endswith(".zip"):
        return "zip"
    if lowered.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
        return "tar"
    raise ValueError(f"Unsupported archive format: {filename}")


def _unique_extraction_dir(workspace: Path, filename: str) -> Path:
    stem = Path(filename).stem
    destination = safe_resolve_ws(workspace, stem)
    if not destination.exists():
        return destination
    for _ in range(1000):
        candidate = destination.with_name(stem + "_" + "".join(random.choices(string.digits, k=3)))
        if not candidate.exists():
            return candidate
    raise ValueError("Could not allocate a unique extraction directory")


def _copy_bounded(source, destination, total: int, cap: int) -> int:
    while True:
        chunk = source.read(65_536)
        if not chunk:
            return total
        total += len(chunk)
        if total > cap:
            raise ValueError(
                f"Extraction too large (> {cap // (1024 * 1024)} MB limit). Possible zip bomb."
            )
        destination.write(chunk)


def extract_archive(file_bytes: bytes, filename: str, workspace: Path) -> dict:
    """Extract one supported archive with member, byte, slip, and race guards."""
    workspace = Path(workspace).resolve()
    mode = _archive_mode(filename)
    destination = _unique_extraction_dir(workspace, filename)
    make_anchored_dir(workspace, destination)
    extracted: list[str] = []
    total = 0
    cap = max_extracted_bytes()
    try:
        if mode == "zip":
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
                members = (member for member in archive.infolist() if not member.is_dir())
                for member in members:
                    if len(extracted) >= MAX_ARCHIVE_MEMBERS:
                        raise ValueError(f"Archive has too many files (> {MAX_ARCHIVE_MEMBERS}). Possible archive bomb.")
                    target = (destination / member.filename).resolve()
                    if not target.is_relative_to(destination.resolve()):
                        raise ValueError(f"Zip-slip blocked: {member.filename}")
                    fd = open_anchored_create_fd(workspace, target)
                    with archive.open(member) as source, os.fdopen(fd, "wb", closefd=True) as output:
                        total = _copy_bounded(source, output, total, cap)
                    extracted.append(str(target.relative_to(workspace)))
        else:
            with tarfile.open(fileobj=io.BytesIO(file_bytes)) as archive:
                for member in archive.getmembers():
                    if not member.isfile():
                        continue
                    if len(extracted) >= MAX_ARCHIVE_MEMBERS:
                        raise ValueError(f"Archive has too many files (> {MAX_ARCHIVE_MEMBERS}). Possible archive bomb.")
                    target = (destination / member.name).resolve()
                    if not target.is_relative_to(destination.resolve()):
                        raise ValueError(f"Tar-slip blocked: {member.name}")
                    source = archive.extractfile(member)
                    if source is None:
                        continue
                    fd = open_anchored_create_fd(workspace, target)
                    with source, os.fdopen(fd, "wb", closefd=True) as output:
                        total = _copy_bounded(source, output, total, cap)
                    extracted.append(str(target.relative_to(workspace)))
    except Exception:
        try:
            rmtree_anchored(workspace, destination)
        except Exception:
            pass
        raise
    return {"extracted": len(extracted), "files": extracted, "dest": str(destination)}


def extract_session_archive(session_id: str, filename: str, file_bytes: bytes) -> dict:
    with session_write_owner(session_id) as session:
        if not session_visible_to_active_profile(session):
            raise SessionNotFound(session_id)
        return extract_session_archive_for_session(session_id, filename, file_bytes)


def extract_session_archive_for_session(
    session_id: str,
    filename: str,
    file_bytes: bytes,
    *,
    extractor=extract_archive,
) -> dict:
    root = attachment_root()
    root.mkdir(parents=True, exist_ok=True)
    directory = session_attachment_dir(session_id, root=root)
    make_anchored_dir(root, directory)
    return extractor(file_bytes, filename, directory)


def write_office_upload_sidecar(workspace: Path, destination: Path, body: bytes) -> dict | None:
    if destination.suffix.lower() not in {".docx", ".xlsx", ".pptx"}:
        return None
    error = "Office sidecar extraction failed"
    sidecar = destination.with_name(f"{destination.name}.md").resolve()
    created = False
    try:
        from api.office_documents import preview_office_document

        preview = preview_office_document(destination.name, body)
        if not sidecar.is_relative_to(workspace.resolve()):
            raise ValueError("Invalid sidecar destination")
        fd = open_anchored_create_fd(workspace, sidecar)
        created = True
        with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as handle:
            handle.write(str(preview.get("content") or ""))
        return {
            "filename": sidecar.name,
            "path": str(sidecar),
            "size": sidecar.stat().st_size,
            "preview_kind": preview.get("preview_kind"),
            "office_format": preview.get("office_format"),
        }
    except FileExistsError:
        return {"error": error}
    except Exception:
        if created:
            try:
                unlink_anchored(workspace, sidecar)
            except Exception:
                pass
        return {"error": error}


def _workspace_upload_result(
    workspace: Path,
    target_directory: Path,
    filename: str,
    body: bytes,
) -> dict:
    safe_name = sanitize_upload_name(filename)
    destination = _create_unique_file(workspace, target_directory, safe_name, body)
    mime = mimetypes.guess_type(destination.name)[0] or "application/octet-stream"
    if safe_name.lower().endswith(ARCHIVE_SUFFIXES):
        try:
            extraction = extract_archive(body, safe_name, target_directory)
        except (zipfile.BadZipFile, tarfile.TarError, ValueError) as error:
            try:
                unlink_anchored(workspace, destination.resolve())
            except FileNotFoundError:
                pass
            return {
                "filename": safe_name,
                "path": str(target_directory),
                "size": len(body),
                "mime": mime,
                "is_image": False,
                "extracted": False,
                "extract_error": str(error) or "Archive extraction failed",
            }
        except Exception:
            try:
                unlink_anchored(workspace, destination.resolve())
            except FileNotFoundError:
                pass
            return {
                "filename": safe_name,
                "path": str(target_directory),
                "size": len(body),
                "mime": mime,
                "is_image": False,
                "extracted": False,
                "extract_error": "Archive extraction failed",
            }
        try:
            unlink_anchored(workspace, destination.resolve())
        except FileNotFoundError:
            pass
        return {
            "filename": safe_name,
            "path": str(extraction.get("dest", target_directory)),
            "size": len(body),
            "is_image": False,
            "extracted": True,
            "extracted_files": extraction.get("files", []),
            "extracted_count": extraction.get("extracted", 0),
        }
    sidecar = write_office_upload_sidecar(workspace, destination, body)
    return {
        "filename": destination.name,
        "path": str(destination),
        "size": destination.stat().st_size,
        "mime": mime,
        "is_image": mime.startswith("image/"),
        "extracted": False,
        **({"sidecar": sidecar} if sidecar and "error" not in sidecar else {}),
        **({"sidecar_error": sidecar["error"]} if sidecar and "error" in sidecar else {}),
    }


def store_workspace_uploads(
    session_id: str,
    subpath: str,
    files: dict,
    *,
    session=None,
    workspace_resolver=resolve_trusted_workspace,
) -> dict:
    """Store all multipart files under one trusted session workspace."""
    if not session_id:
        raise ValueError("Missing session_id")
    if not files:
        raise ValueError("No file field in request")
    session = session if session is not None else _visible_session(session_id)
    workspace = workspace_resolver(session.workspace)
    target_directory = safe_resolve_ws(workspace, subpath) if subpath else workspace
    if not target_directory.resolve().is_relative_to(workspace.resolve()):
        raise UploadContainmentError("Upload target escapes workspace")
    try:
        make_anchored_dir(workspace, target_directory)
    except (ValueError, OSError):
        raise UploadContainmentError("Upload target escapes workspace") from None
    results = [
        _workspace_upload_result(workspace, target_directory, filename, body)
        for filename, body in files.values()
        if filename
    ]
    if len(results) == 1:
        return results[0]
    return {"files": results, "count": len(results)}
