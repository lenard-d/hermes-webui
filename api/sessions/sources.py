"""Source-identity policy for sessions imported from Hermes stores.

Import and archive paths should not each decide which foreign fields may enter
the WebUI sidecar. This module owns that allowlist and the raw-source fallback.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


CLI_SOURCE_FIELDS = (
    "source_tag",
    "session_source",
    "source_label",
    "user_id",
    "chat_id",
    "chat_type",
    "thread_id",
    "session_key",
    "platform",
)

IMPORT_SOURCE_FIELDS = (
    "is_cli_session",
    "source_tag",
    "raw_source",
    "session_source",
    "source_label",
    "user_id",
    "chat_id",
    "chat_type",
    "thread_id",
    "session_key",
    "platform",
    "project_id",
    "model_provider",
)


def apply_cli_source_metadata(
    session: object,
    metadata: Mapping[str, Any] | None,
    *,
    is_cli_session: bool,
) -> object:
    """Apply the complete CLI-source identity to a session object.

    Missing values intentionally clear stale identity fields. ``raw_source``
    falls back to the normalized source tag, matching Hermes import semantics.
    """
    source = metadata if isinstance(metadata, Mapping) else {}
    session.is_cli_session = bool(is_cli_session)
    for field in CLI_SOURCE_FIELDS:
        setattr(session, field, source.get(field))
    session.raw_source = source.get("raw_source") or source.get("source_tag")
    return session


def import_source_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return only foreign fields accepted by a newly imported sidecar."""
    if not isinstance(metadata, Mapping):
        return {}
    return {field: metadata[field] for field in IMPORT_SOURCE_FIELDS if field in metadata}
