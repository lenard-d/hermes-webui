"""Workspace browsing and explicitly authorized escape-target reads."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

from api.config import MIME_MAP
from api.helpers import _sanitize_error, bad, j
from api.routes_parts.media_files import _serve_file_bytes, _serve_inline_html_preview
from api.routes_parts.security import (
    _check_csrf,
    _csrf_rejection_error,
    _safe_content_length,
)
from api.sessions.store import get_cli_sessions, get_session, get_session_for_file_ops
from api.workspace import (
    EscapeAuthorizationExpiredError,
    authorize_escape_target,
    dir_signature,
    list_authorized_escape_dir,
    list_dir,
    raw_authorized_escape_target,
    read_authorized_escape_file_content,
)

def _handle_list_dir(handler, parsed):
    qs = parse_qs(parsed.query)
    sid = qs.get("session_id", [""])[0]
    if not sid:
        return bad(handler, "session_id is required")
    try:
        s = get_session(sid)
        workspace = s.workspace
    except KeyError:
        # Fallback for CLI sessions not loaded in WebUI memory
        try:
            cli_meta = None
            for cs in get_cli_sessions():
                if cs["session_id"] == sid:
                    cli_meta = cs
                    break
            if not cli_meta:
                return bad(handler, "Session not found", 404)
            workspace = cli_meta.get("workspace", "")
        except Exception:
            return bad(handler, "Session not found", 404)
    try:
        rel_path = qs.get("path", ["."])[0]
        entries = list_dir(Path(workspace), rel_path)
        return j(
            handler,
            {
                "entries": entries,
                "signature": dir_signature(Path(workspace), rel_path, entries),
                "path": rel_path,
            },
        )
    except (FileNotFoundError, ValueError) as e:
        return bad(handler, _sanitize_error(e), 404)


def _read_json_request_body(handler, *, max_bytes: int = 4096) -> dict:
    try:
        length = _safe_content_length(handler, max_bytes)
    except (ValueError, OverflowError) as exc:
        raise ValueError(_sanitize_error(exc)) from exc
    raw = handler.rfile.read(length) if length else b"{}"
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("invalid JSON body") from exc
    return payload if isinstance(payload, dict) else {}


def _handle_escape_authorize(handler, parsed, body: dict | None = None):
    if handler.command != "POST":
        return bad(handler, "method not allowed", 405)
    if not handler.headers.get("Origin"):
        return bad(handler, "browser origin required", 403)
    if not _check_csrf(handler):
        return bad(handler, _csrf_rejection_error(handler), 403)
    if body is None:
        try:
            body = _read_json_request_body(handler)
        except ValueError as exc:
            return bad(handler, _sanitize_error(exc), 400)
    qs = parse_qs(parsed.query)
    sid = str(body.get("session_id") or qs.get("session_id", [""])[0] or "").strip()
    rel = str(body.get("path") or qs.get("path", [""])[0] or "").strip()
    token = str(body.get("token") or qs.get("token", [""])[0] or "").strip()
    if token:
        return bad(handler, "token must not be provided", 400)
    if not sid:
        return bad(handler, "session_id is required")
    if not rel:
        return bad(handler, "path is required")
    try:
        s = get_session_for_file_ops(sid)
    except KeyError:
        return bad(handler, "Session not found", 404)
    try:
        payload = authorize_escape_target(Path(s.workspace), sid, rel)
    except ValueError as exc:
        return bad(handler, _sanitize_error(exc), 404)
    return j(handler, payload)


def _handle_escape_list_dir(handler, parsed):
    qs = parse_qs(parsed.query)
    sid = qs.get("session_id", [""])[0]
    token = qs.get("token", [""])[0]
    if not sid:
        return bad(handler, "session_id is required")
    if not token:
        return bad(handler, "token is required")
    try:
        s = get_session_for_file_ops(sid)
    except KeyError:
        return bad(handler, "Session not found", 404)
    rel_path = qs.get("path", ["."])[0]
    try:
        payload = list_authorized_escape_dir(Path(s.workspace), sid, token, rel_path)
        return j(handler, payload)
    except FileNotFoundError as exc:
        return bad(handler, _sanitize_error(exc), 404)
    except EscapeAuthorizationExpiredError as exc:
        return bad(handler, _sanitize_error(exc), 403)
    except ValueError as exc:
        return bad(handler, _sanitize_error(exc), 404)


def _handle_escape_file_read(handler, parsed):
    qs = parse_qs(parsed.query)
    sid = qs.get("session_id", [""])[0]
    token = qs.get("token", [""])[0]
    if not sid:
        return bad(handler, "session_id is required")
    if not token:
        return bad(handler, "token is required")
    try:
        s = get_session_for_file_ops(sid)
    except KeyError:
        return bad(handler, "Session not found", 404)
    rel = qs.get("path", [""])[0]
    try:
        return j(handler, read_authorized_escape_file_content(Path(s.workspace), sid, token, rel))
    except FileNotFoundError as exc:
        return bad(handler, _sanitize_error(exc), 404)
    except EscapeAuthorizationExpiredError as exc:
        return bad(handler, _sanitize_error(exc), 403)
    except ImportError as exc:
        # Optional Office parsers absent on a lean install — mirror
        # _handle_file_read: a 503 with the install hint, not a 500 traceback.
        return bad(handler, _sanitize_error(exc), 503)
    except ValueError as exc:
        return bad(handler, _sanitize_error(exc), 404)


def _handle_escape_file_raw(handler, parsed):
    qs = parse_qs(parsed.query)
    sid = qs.get("session_id", [""])[0]
    token = qs.get("token", [""])[0]
    if not sid:
        return bad(handler, "session_id is required")
    if not token:
        return bad(handler, "token is required")
    try:
        s = get_session_for_file_ops(sid)
    except KeyError:
        return bad(handler, "Session not found", 404)
    rel = qs.get("path", [""])[0]
    force_download = qs.get("download", [""])[0] == "1"
    try:
        anchor_root, target = raw_authorized_escape_target(Path(s.workspace), sid, token, rel)
    except FileNotFoundError:
        return j(handler, {"error": "not found"}, status=404)
    except EscapeAuthorizationExpiredError as exc:
        return bad(handler, _sanitize_error(exc), 403)
    except ValueError as exc:
        return bad(handler, _sanitize_error(exc), 404)
    if not target.exists() or not target.is_file():
        return j(handler, {"error": "not found"}, status=404)
    ext = target.suffix.lower()
    mime = MIME_MAP.get(ext, "application/octet-stream")
    inline_preview = qs.get("inline", [""])[0] == "1"
    dangerous_types = {"text/html", "application/xhtml+xml", "image/svg+xml"}
    html_inline_ok = inline_preview and mime == "text/html"
    disposition = "attachment" if force_download or (mime in dangerous_types and not html_inline_ok) else "inline"
    sandbox_csp = "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox"
    # Content-Security-Policy sandboxing is carried through the csp=sandbox_csp handoff below.
    csp = sandbox_csp if (inline_preview and not force_download and disposition == "inline") else None
    if html_inline_ok:
        return _serve_inline_html_preview(handler, target, "no-store", csp=sandbox_csp, anchor_root=anchor_root)
    return _serve_file_bytes(handler, target, mime, disposition, "no-store", csp=csp, anchor_root=anchor_root)


__routes_exports__ = (
    "_handle_list_dir",
    "_read_json_request_body",
    "_handle_escape_authorize",
    "_handle_escape_list_dir",
    "_handle_escape_file_read",
    "_handle_escape_file_raw",
)
