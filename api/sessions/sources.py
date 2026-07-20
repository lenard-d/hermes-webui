"""Source-identity policy for sessions imported from Hermes stores.

Import and archive paths should not each decide which foreign fields may enter
the WebUI sidecar. This module owns that allowlist and the raw-source fallback.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from api.agent_ops import MESSAGING_SOURCES


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


def safe_first(*values: object) -> str:
    """Return the first non-empty source-identity value as text."""
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def normalize_messaging_source(raw_source: object) -> str:
    """Normalize a messaging source for identity and filter decisions."""
    return str(raw_source or "").strip().lower()


def is_known_messaging_source(raw_source: object) -> bool:
    """Return whether ``raw_source`` names an Agent messaging channel."""
    return normalize_messaging_source(raw_source) in MESSAGING_SOURCES


def is_messaging_session_record(session: object) -> bool:
    """Return whether a session record is owned by an external message channel."""
    if not session:
        return False
    if isinstance(session, Mapping):
        field = session.get
    else:
        field = lambda name: getattr(session, name, None)
    if field("session_source") == "messaging":
        return True
    raw = safe_first(
        field("raw_source"),
        field("source_tag"),
        field("source"),
        field("source_label"),
    )
    return is_known_messaging_source(raw)


def requires_external_metadata_lookup(session: object) -> bool:
    """Return whether a sidecar still needs foreign-source metadata."""
    if not session:
        return False
    if isinstance(session, Mapping):
        field = session.get
    else:
        field = lambda name: getattr(session, name, None)
    if is_messaging_session_record(session):
        return True
    if bool(field("is_cli_session")) or bool(field("read_only")):
        return True
    session_source = normalize_messaging_source(safe_first(field("session_source")))
    if session_source in {"messaging", "external_agent", "external-agent"}:
        return True
    return bool(
        safe_first(
            field("source_tag"),
            field("raw_source"),
            field("source"),
            field("source_label"),
            field("platform"),
        )
    )
