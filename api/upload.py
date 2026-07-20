"""Compatibility facade for media uploads and speech transcription.

New code imports storage behavior from :mod:`api.media`, upload HTTP adapters
from :mod:`api.routes_parts.media_uploads`, and transcription from
:mod:`api.speech`. This module preserves historical imports without owning any
of those implementations.
"""

from __future__ import annotations

import mimetypes  # compatibility for integrations that patch the module
import os  # compatibility for integrations that patch the module
import tempfile  # compatibility for integrations that patch the module
from pathlib import Path

from api.config import MAX_UPLOAD_BYTES, STATE_DIR
from api.helpers import j
from api.media.multipart import parse_multipart
from api.media.uploads import (
    MAX_EXTRACTED_BYTES as _MAX_EXTRACTED_BYTES,
    attachment_root as _attachment_root,
    extract_archive,
    extract_session_archive_for_session,
    max_extracted_bytes as _max_extracted_bytes,
    sanitize_upload_name as _sanitize_upload_name,
    session_attachment_dir as _session_attachment_dir,
    store_chat_attachment_for_session,
    store_workspace_uploads,
    upload_destination as _upload_destination,
    write_office_upload_sidecar as _write_office_upload_sidecar,
)
from api.profiles import _profiles_match, get_active_profile_name as _get_active_profile_name
from api.routes_parts import media_uploads as _http
from api.sessions import get_session, session_write_owner
from api.speech import (
    discover_transcription_provider,
    transcription_provider_capability_from_module,
)
from api.workspace import (
    make_anchored_dir,
    open_anchored_create_fd,
    resolve_trusted_workspace,
    rmtree_anchored,
    safe_resolve_ws,
    unlink_anchored,
)


__all__ = (
    "MAX_UPLOAD_BYTES",
    "STATE_DIR",
    "_MAX_EXTRACTED_BYTES",
    "_attachment_root",
    "_max_extracted_bytes",
    "_sanitize_upload_name",
    "_session_attachment_dir",
    "_session_visible_to_active_profile",
    "_stt_provider_capability",
    "_stt_provider_capability_from_module",
    "_upload_destination",
    "_write_office_upload_sidecar",
    "extract_archive",
    "handle_transcribe",
    "handle_transcribe_capability",
    "handle_upload",
    "handle_upload_extract",
    "handle_workspace_upload",
    "make_anchored_dir",
    "mimetypes",
    "open_anchored_create_fd",
    "os",
    "parse_multipart",
    "Path",
    "resolve_trusted_workspace",
    "rmtree_anchored",
    "safe_resolve_ws",
    "tempfile",
    "unlink_anchored",
)


def _reject_invisible_session(handler, session) -> bool:
    if _session_visible_to_active_profile(session):
        return False
    j(handler, {"error": "Session not found"}, status=404)
    return True


def _session_visible_to_active_profile(session) -> bool:
    profile = getattr(session, "profile", None)
    if not isinstance(profile, str):
        profile = None
    return _profiles_match(profile, _get_active_profile_name())


def handle_upload(handler):
    try:
        fields, files = _http._multipart(handler, parse_multipart)
        if "file" not in files:
            return j(handler, {"error": "No file field in request"}, status=400)
        filename, body = files["file"]
        if not filename:
            return j(handler, {"error": "No filename in upload"}, status=400)
        session_id = fields.get("session_id", "")
        with session_write_owner(session_id) as session:
            if _reject_invisible_session(handler, session):
                return True
            return j(
                handler,
                store_chat_attachment_for_session(
                    session_id,
                    filename,
                    body,
                    before_store=_upload_destination,
                ),
            )
    except Exception as error:
        return _http._upload_error(handler, error, operation="attachment upload")


def handle_upload_extract(handler):
    try:
        fields, files = _http._multipart(handler, parse_multipart)
        if "file" not in files:
            return j(handler, {"error": "No file field in request"}, status=400)
        filename, body = files["file"]
        if not filename:
            return j(handler, {"error": "No filename in upload"}, status=400)
        session_id = fields.get("session_id", "")
        with session_write_owner(session_id) as session:
            if _reject_invisible_session(handler, session):
                return True
            return j(
                handler,
                {
                    "ok": True,
                    **extract_session_archive_for_session(
                        session_id,
                        filename,
                        body,
                        extractor=extract_archive,
                    ),
                },
            )
    except Exception as error:
        return _http._upload_error(
            handler,
            error,
            operation="archive upload",
            generic_message="Archive extraction failed",
        )


def handle_workspace_upload(handler):
    try:
        fields, files = _http._multipart(handler, parse_multipart)
        session_id = fields.get("session_id", "")
        if not session_id:
            return j(handler, {"error": "Missing session_id"}, status=400)
        if not files:
            return j(handler, {"error": "No file field in request"}, status=400)
        try:
            session = get_session(session_id)
        except KeyError:
            return j(handler, {"error": "Session not found"}, status=404)
        if _reject_invisible_session(handler, session):
            return True
        return j(
            handler,
            store_workspace_uploads(
                session_id,
                fields.get("path", ""),
                files,
                session=session,
                workspace_resolver=resolve_trusted_workspace,
            ),
        )
    except Exception as error:
        return _http._upload_error(handler, error, operation="workspace upload")


def _stt_provider_capability_from_module(stt):
    """Compatibility adapter for the speech provider-discovery interface."""
    return transcription_provider_capability_from_module(stt)


def _stt_provider_capability():
    return discover_transcription_provider()


def handle_transcribe(handler):
    """Compatibility export; production routing uses the speech HTTP adapter."""
    from api.routes_parts.tts import handle_transcribe as speech_handle_transcribe

    return speech_handle_transcribe(handler)


def handle_transcribe_capability(handler):
    available, provider = _stt_provider_capability()
    return j(
        handler,
        {"ok": True, "available": bool(available), "provider": provider},
    )
