"""Normalization policy shared by browser and autonomous chat turns."""

from __future__ import annotations

from collections.abc import Callable

from api.workspace import get_last_workspace, resolve_trusted_workspace


def resolve_chat_workspace(
    session,
    requested_workspace,
    *,
    resolve_workspace: Callable | None = None,
    last_workspace: Callable[[], str] | None = None,
) -> str:
    """Resolve an explicit workspace or repair a stale implicit session value."""
    if resolve_workspace is None:
        resolve_workspace = resolve_trusted_workspace
    if last_workspace is None:
        last_workspace = get_last_workspace
    explicit = requested_workspace not in (None, "")
    candidate = (
        requested_workspace if explicit else getattr(session, "workspace", None)
    )
    try:
        return str(resolve_workspace(candidate))
    except ValueError:
        if explicit:
            raise
    fallback = str(resolve_workspace(last_workspace()))
    session.workspace = fallback
    try:
        session.save()
    except Exception:
        pass
    return fallback


def normalize_chat_attachments(raw_attachments) -> list[dict]:
    """Normalize legacy filename and structured browser attachment payloads."""
    normalized = []
    if not isinstance(raw_attachments, list):
        return normalized
    for item in raw_attachments:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("filename") or "").strip()
            path = str(item.get("path") or "").strip()
            mime = str(item.get("mime") or "").strip()
            attachment = {"name": name or path, "path": path, "mime": mime}
            size = item.get("size")
            if isinstance(size, int):
                attachment["size"] = size
            is_image = item.get("is_image")
            if isinstance(is_image, bool):
                attachment["is_image"] = is_image
            normalized.append(attachment)
            continue
        value = str(item).strip()
        if value:
            normalized.append({"name": value, "path": "", "mime": ""})
    return normalized
