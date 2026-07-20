"""Thin HTTP adapters for guarded media preview and file downloads."""

from __future__ import annotations

import logging
import os
import shutil
import zipfile
from pathlib import Path
from urllib.parse import parse_qs

from api import auth as auth_module
from api.helpers import _sanitize_error, _security_headers, bad, j, safe_resolve
from api.media.delivery import (
    MEDIA_TOKEN_RE,
    collect_folder_download,
    folder_zip_max_bytes,
    folder_zip_max_files,
    message_content_text,
    path_is_within_root,
    read_anchored_file_bytes,
    resolve_local_media,
    resolve_raw_file,
    session_media_token_allows_path,
)
from api.media.preview import (
    HTML_SANDBOX_CSP,
    content_disposition_value,
    html_preview_with_blank_base,
)
from api.sessions import get_session_for_file_ops
from api.workspace import open_anchored_fd, read_file_content

logger = logging.getLogger(__name__)


_content_disposition_value = content_disposition_value
_html_preview_with_blank_base = html_preview_with_blank_base
_MEDIA_TOKEN_RE = MEDIA_TOKEN_RE
_message_content_text = message_content_text
_path_is_within_root = path_is_within_root
_folder_zip_max_bytes = folder_zip_max_bytes
_folder_zip_max_files = folder_zip_max_files
_read_anchored_file_bytes = read_anchored_file_bytes


def _parse_range_header(range_header: str, file_size: int) -> tuple[int, int] | None:
    if not range_header or not range_header.startswith("bytes=") or file_size < 1:
        return None
    spec = range_header.split("=", 1)[1].strip()
    if "," in spec or "-" not in spec:
        return None
    start_text, end_text = spec.split("-", 1)
    try:
        if start_text == "":
            suffix_length = int(end_text)
            if suffix_length <= 0:
                return None
            start = max(0, file_size - suffix_length)
            end = file_size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else file_size - 1
            if start < 0:
                return None
            end = min(end, file_size - 1)
        if start > end or start >= file_size:
            return None
        return start, end
    except ValueError:
        return None


def _session_media_token_allows_path(
    session_id,
    target,
    allowed_mimes,
    *,
    session_loader=None,
):
    kwargs = {"session_loader": session_loader} if session_loader is not None else {}
    return session_media_token_allows_path(session_id, target, allowed_mimes, **kwargs)


def _session_media_token_allows_image_path(
    session_id,
    target,
    image_mimes,
    *,
    session_loader=None,
):
    return _session_media_token_allows_path(
        session_id,
        target,
        image_mimes,
        session_loader=session_loader,
    )


def _open_file_read_fd(
    target: Path,
    anchor_root: Path | None = None,
    *,
    anchored_open=open_anchored_fd,
) -> int:
    if anchor_root is None:
        fd = os.open(str(target), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        return fd
    fd = anchored_open(anchor_root, target.resolve(), want_dir=False)
    return fd


def _close_fd_quietly(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _serve_file_bytes(
    handler,
    target: Path,
    mime: str,
    disposition: str,
    cache_control: str,
    *,
    csp: str | None = None,
    anchor_root: Path | None = None,
    anchored_open=open_anchored_fd,
):
    """Write one already-authorized file response from a held descriptor."""
    fd = None
    try:
        fd = _open_file_read_fd(target, anchor_root, anchored_open=anchored_open)
        file_size = os.fstat(fd).st_size
    except PermissionError:
        _close_fd_quietly(fd)
        return bad(handler, "Permission denied", 403)
    except FileNotFoundError:
        _close_fd_quietly(fd)
        return j(handler, {"error": "not found"}, status=404)
    except ValueError as error:
        _close_fd_quietly(fd)
        return bad(handler, _sanitize_error(error), 403)
    except Exception:
        _close_fd_quietly(fd)
        return bad(handler, "Could not stat file", 500)
    try:
        requested_range = handler.headers.get("Range", "")
        byte_range = _parse_range_header(requested_range, file_size)
        if requested_range and byte_range is None:
            handler.send_response(416)
            handler.send_header("Content-Range", f"bytes */{file_size}")
            handler.send_header("Accept-Ranges", "bytes")
            handler.send_header("Content-Length", "0")
            _security_headers(handler)
            handler.end_headers()
            return True
        start, end = byte_range if byte_range else (0, max(0, file_size - 1))
        content_length = end - start + 1 if file_size else 0
        handler.send_response(206 if byte_range else 200)
        handler.send_header("Content-Type", mime)
        handler.send_header("Content-Length", str(content_length))
        handler.send_header("Accept-Ranges", "bytes")
        if byte_range:
            handler.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        handler.send_header("Cache-Control", cache_control)
        handler.send_header("Content-Disposition", _content_disposition_value(disposition, target.name))
        if csp:
            handler.send_header("Content-Security-Policy", csp)
            handler.send_header("X-Content-Type-Options", "nosniff")
            handler.send_header("Referrer-Policy", "same-origin")
            handler.send_header(
                "Permissions-Policy",
                "camera=(), microphone=(self), geolocation=(), clipboard-write=(self)",
            )
        else:
            _security_headers(handler)
        handler.end_headers()
        if content_length:
            with os.fdopen(fd, "rb", closefd=True) as handle:
                fd = None
                handle.seek(start)
                remaining = content_length
                while remaining:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
                    remaining -= len(chunk)
        return True
    finally:
        _close_fd_quietly(fd)


def _serve_inline_html_preview(
    handler,
    target: Path,
    cache_control: str,
    *,
    csp: str,
    anchor_root: Path | None = None,
    anchored_open=open_anchored_fd,
):
    fd = None
    try:
        fd = _open_file_read_fd(target, anchor_root, anchored_open=anchored_open)
        with os.fdopen(fd, "rb", closefd=True) as handle:
            fd = None
            body = _html_preview_with_blank_base(handle.read())
    except PermissionError:
        return bad(handler, "Permission denied", 403)
    except FileNotFoundError:
        return j(handler, {"error": "not found"}, status=404)
    except ValueError as error:
        return bad(handler, _sanitize_error(error), 403)
    except Exception:
        return bad(handler, "Could not read file", 500)
    finally:
        _close_fd_quietly(fd)
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Accept-Ranges", "none")
    handler.send_header("Cache-Control", cache_control)
    handler.send_header("Content-Disposition", _content_disposition_value("inline", target.name))
    handler.send_header("Content-Security-Policy", csp)
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "same-origin")
    handler.send_header(
        "Permissions-Policy",
        "camera=(), microphone=(self), geolocation=(), clipboard-write=(self)",
    )
    handler.end_headers()
    handler.wfile.write(body)
    return True


def _handle_media(
    handler,
    parsed,
    *,
    workspace_getter=None,
    session_loader=None,
    file_sender=_serve_file_bytes,
    html_sender=_serve_inline_html_preview,
):
    if auth_module.is_auth_enabled():
        cookie = auth_module.parse_cookie(handler)
        if not (cookie and auth_module.verify_session(cookie)):
            return j(handler, {"error": "Authentication required"}, status=401)
    query = parse_qs(parsed.query)
    try:
        resolution_kwargs = {}
        if workspace_getter is not None:
            resolution_kwargs["workspace_getter"] = workspace_getter
        if session_loader is not None:
            resolution_kwargs["session_loader"] = session_loader
        plan = resolve_local_media(
            query.get("path", [""])[0].strip(),
            session_id=query.get("session_id", [""])[0],
            inline_requested=query.get("inline", [""])[0] == "1",
            **resolution_kwargs,
        )
    except ValueError as error:
        return bad(handler, str(error), 400)
    except PermissionError:
        return bad(handler, "Path not in allowed location", 403)
    except FileNotFoundError:
        return j(handler, {"error": "not found"}, status=404)
    if plan.preview.transform_html:
        return html_sender(
            handler,
            plan.target,
            "private, max-age=3600",
            csp=plan.preview.csp or HTML_SANDBOX_CSP,
            anchor_root=plan.anchor_root,
        )
    return file_sender(
        handler,
        plan.target,
        plan.preview.mime,
        plan.preview.disposition,
        "private, max-age=3600",
        csp=plan.preview.csp,
        anchor_root=plan.anchor_root,
    )


def _file_raw_target(session, session_id: str, relative_path: str) -> tuple[Path, Path] | None:
    try:
        plan = resolve_raw_file(session, session_id, relative_path)
    except FileNotFoundError:
        return None
    return plan.anchor_root, plan.target


def _folder_download_collect(target, workspace_root, max_bytes, max_files):
    plan = collect_folder_download(target, workspace_root, max_bytes, max_files)
    files = [(entry.path, entry.archive_name) for entry in plan.entries]
    return files, plan.total_bytes, plan.limit_hit


def _handle_folder_download(
    handler,
    parsed,
    *,
    session_lookup=get_session_for_file_ops,
    anchored_open=open_anchored_fd,
):
    query = parse_qs(parsed.query)
    session_id = query.get("session_id", [""])[0]
    if not session_id:
        return bad(handler, "session_id is required")
    try:
        session = session_lookup(session_id)
    except KeyError:
        return bad(handler, "Session not found", 404)
    try:
        target = safe_resolve(Path(session.workspace), query.get("path", [""])[0])
    except ValueError:
        return bad(handler, "invalid path", 400)
    if not target.exists():
        return j(handler, {"error": "not found"}, status=404)
    if not target.is_dir():
        return bad(handler, "path must be a directory; use /api/file/raw for single files", 400)
    workspace_root = Path(session.workspace).resolve()
    plan = collect_folder_download(
        target,
        workspace_root,
        folder_zip_max_bytes(),
        folder_zip_max_files(),
    )
    if plan.limit_hit == "max_files":
        return j(handler, {
            "error": "too many files",
            "limit": folder_zip_max_files(),
            "configure": "HERMES_WEBUI_FOLDER_ZIP_MAX_FILES",
        }, status=413)
    if plan.limit_hit == "max_bytes":
        return j(handler, {
            "error": "folder too large",
            "limit_bytes": folder_zip_max_bytes(),
            "configure": "HERMES_WEBUI_FOLDER_ZIP_MAX_MB",
        }, status=413)
    handler.send_response(200)
    handler.send_header("Content-Type", "application/zip")
    handler.send_header(
        "Content-Disposition",
        _content_disposition_value("attachment", (target.name or "workspace") + ".zip"),
    )
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Connection", "close")
    handler.end_headers()
    written = 0
    with zipfile.ZipFile(handler.wfile, mode="w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for entry in plan.entries:
            fd = None
            try:
                fd = anchored_open(workspace_root, entry.path.resolve(), want_dir=False)
                info = zipfile.ZipInfo(entry.archive_name)
                info.compress_type = zipfile.ZIP_DEFLATED
                with os.fdopen(fd, "rb", closefd=True) as source:
                    fd = None
                    with archive.open(info, "w") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                written += 1
            except (ValueError, OSError, PermissionError) as error:
                logger.warning("folder-download: skipping %s: %s", entry.path, error)
            finally:
                _close_fd_quietly(fd)
    logger.info(
        "folder-download: streamed %d/%d files (~%d bytes) from %s",
        written,
        len(plan.entries),
        plan.total_bytes,
        target,
    )


def _handle_file_raw(
    handler,
    parsed,
    *,
    session_lookup=get_session_for_file_ops,
    file_sender=_serve_file_bytes,
    html_sender=_serve_inline_html_preview,
):
    query = parse_qs(parsed.query)
    session_id = query.get("session_id", [""])[0]
    if not session_id:
        return bad(handler, "session_id is required")
    try:
        session = session_lookup(session_id)
    except KeyError:
        return bad(handler, "Session not found", 404)
    try:
        plan = resolve_raw_file(
            session,
            session_id,
            query.get("path", [""])[0],
            inline_requested=query.get("inline", [""])[0] == "1",
            force_download=query.get("download", [""])[0] == "1",
        )
    except FileNotFoundError:
        return j(handler, {"error": "not found"}, status=404)
    if plan.preview.transform_html:
        return html_sender(
            handler,
            plan.target,
            "no-store",
            csp=plan.preview.csp or HTML_SANDBOX_CSP,
            anchor_root=plan.anchor_root,
        )
    return file_sender(
        handler,
        plan.target,
        plan.preview.mime,
        plan.preview.disposition,
        "no-store",
        csp=plan.preview.csp,
        anchor_root=plan.anchor_root,
    )


def _handle_file_read(
    handler,
    parsed,
    *,
    session_lookup=get_session_for_file_ops,
    file_reader=read_file_content,
):
    query = parse_qs(parsed.query)
    session_id = query.get("session_id", [""])[0]
    if not session_id:
        return bad(handler, "session_id is required")
    try:
        session = session_lookup(session_id)
    except KeyError:
        return bad(handler, "Session not found", 404)
    relative_path = query.get("path", [""])[0]
    if not relative_path:
        return bad(handler, "path is required")
    try:
        return j(handler, file_reader(Path(session.workspace), relative_path))
    except ImportError as error:
        return bad(handler, str(error), 503)
    except (FileNotFoundError, ValueError) as error:
        return bad(handler, _sanitize_error(error), 404)


__all__ = [
    "_content_disposition_value",
    "_parse_range_header",
    "_open_file_read_fd",
    "_close_fd_quietly",
    "_serve_file_bytes",
    "_html_preview_with_blank_base",
    "_serve_inline_html_preview",
    "_MEDIA_TOKEN_RE",
    "_message_content_text",
    "_session_media_token_allows_path",
    "_session_media_token_allows_image_path",
    "_path_is_within_root",
    "_handle_media",
    "_file_raw_target",
    "_folder_zip_max_bytes",
    "_folder_zip_max_files",
    "_folder_download_collect",
    "_handle_folder_download",
    "_handle_file_raw",
    "_handle_file_read",
    "_read_anchored_file_bytes",
]
