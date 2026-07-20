"""Attachment lifecycle cleanup owned by the media domain."""

from __future__ import annotations

import shutil

from api.media.uploads import session_attachment_dir


def cleanup_session_attachments(session_id: str) -> None:
    """Remove every transient attachment owned by ``session_id``."""
    shutil.rmtree(session_attachment_dir(session_id), ignore_errors=True)
