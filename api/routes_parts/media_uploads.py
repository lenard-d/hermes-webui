"""Thin HTTP adapters for media upload endpoints."""

from __future__ import annotations

import logging

from api.config import MAX_UPLOAD_BYTES
from api.helpers import j
from api.media.multipart import UploadTooLarge, parse_multipart
from api.media.uploads import (
    SessionNotFound,
    UploadContainmentError,
    extract_session_archive,
    store_chat_attachment,
    store_workspace_uploads,
)

logger = logging.getLogger(__name__)


def _multipart(handler, parser=parse_multipart) -> tuple[dict, dict]:
    content_type = handler.headers.get("Content-Type", "")
    content_length = handler.headers.get("Content-Length", 0) or 0
    return parser(handler.rfile, content_type, content_length)


def _upload_error(
    handler,
    error: Exception,
    *,
    operation: str,
    generic_message: str = "Upload failed",
):
    if isinstance(error, UploadTooLarge):
        return j(
            handler,
            {"error": f"File too large (max {MAX_UPLOAD_BYTES // 1024 // 1024}MB)"},
            status=413,
        )
    if isinstance(error, (SessionNotFound, KeyError)):
        return j(handler, {"error": "Session not found"}, status=404)
    if isinstance(error, UploadContainmentError):
        return j(handler, {"error": str(error)}, status=403)
    if isinstance(error, ValueError):
        return j(handler, {"error": str(error)}, status=400)
    logger.exception("%s failed", operation)
    return j(handler, {"error": generic_message}, status=500)


def handle_upload(
    handler,
    *,
    parser=parse_multipart,
    store=store_chat_attachment,
):
    try:
        fields, files = _multipart(handler, parser)
        if "file" not in files:
            return j(handler, {"error": "No file field in request"}, status=400)
        filename, body = files["file"]
        if not filename:
            return j(handler, {"error": "No filename in upload"}, status=400)
        return j(handler, store(fields.get("session_id", ""), filename, body))
    except Exception as error:
        return _upload_error(handler, error, operation="attachment upload")


def handle_upload_extract(
    handler,
    *,
    parser=parse_multipart,
    extract=extract_session_archive,
):
    try:
        fields, files = _multipart(handler, parser)
        if "file" not in files:
            return j(handler, {"error": "No file field in request"}, status=400)
        filename, body = files["file"]
        if not filename:
            return j(handler, {"error": "No filename in upload"}, status=400)
        return j(handler, {"ok": True, **extract(fields.get("session_id", ""), filename, body)})
    except Exception as error:
        response = _upload_error(
            handler,
            error,
            operation="archive upload",
            generic_message="Archive extraction failed",
        )
        return response


def handle_workspace_upload(
    handler,
    *,
    parser=parse_multipart,
    store=store_workspace_uploads,
):
    try:
        fields, files = _multipart(handler, parser)
        return j(
            handler,
            store(fields.get("session_id", ""), fields.get("path", ""), files),
        )
    except Exception as error:
        return _upload_error(handler, error, operation="workspace upload")
