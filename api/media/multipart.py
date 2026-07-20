"""Bounded multipart request-body parsing used by media route adapters."""

from __future__ import annotations

import email.parser
import re

from api.config import MAX_UPLOAD_BYTES


class UploadTooLarge(ValueError):
    """Raised before a request body larger than the configured cap is read."""


def parse_multipart(rfile, content_type, content_length) -> tuple[dict, dict]:
    """Read and parse one bounded multipart body.

    The length guard is authoritative for every upload endpoint. In particular,
    negative lengths are rejected before ``read`` because ``read(-1)`` consumes
    the stream without a bound.
    """
    boundary_match = re.search(r"boundary=([^;\s]+)", str(content_type or ""))
    if not boundary_match:
        raise ValueError("No boundary in Content-Type")
    boundary = boundary_match.group(1).strip('"').encode()
    try:
        length = int(content_length)
    except (TypeError, ValueError):
        raise ValueError("Invalid Content-Length") from None
    if length < 0:
        raise ValueError("Invalid Content-Length (negative)")
    if length > MAX_UPLOAD_BYTES:
        raise UploadTooLarge(f"Upload too large (max {MAX_UPLOAD_BYTES} bytes)")

    raw = rfile.read(length)
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    delimiter = b"--" + boundary
    for part in raw.split(delimiter)[1:]:
        stripped = part.lstrip(b"\r\n")
        if stripped.startswith(b"--"):
            break
        separator = b"\r\n\r\n" if b"\r\n\r\n" in part else b"\n\n"
        if separator not in part:
            continue
        header_raw, body = part.split(separator, 1)
        if body.endswith(b"\r\n"):
            body = body[:-2]
        elif body.endswith(b"\n"):
            body = body[:-1]
        header_text = header_raw.lstrip(b"\r\n").decode("utf-8", errors="replace")
        message = email.parser.HeaderParser().parsestr(header_text)
        disposition = message.get("Content-Disposition", "")
        name_match = re.search(r'name="([^"]*)"', disposition)
        file_match = re.search(r'filename="([^"]*)"', disposition)
        if not name_match:
            continue
        name = name_match.group(1)
        if file_match:
            files[name] = (file_match.group(1), body)
        else:
            fields[name] = body.decode("utf-8", errors="replace")
    return fields, files
