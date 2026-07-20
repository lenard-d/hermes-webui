"""Workspace sentinels used by model context and transcript reconciliation."""

from __future__ import annotations

import re


_WORKSPACE_PREFIX_RE = re.compile(r'^\s*\[Workspace::v1:\s*(?:\\.|[^\]\\])+\]\s*')
_LEGACY_WORKSPACE_PREFIX_RE = re.compile(r'^\s*\[Workspace:[^\]]+\]\s*')
_WORKSPACE_PREFIX_ANY_RE = re.compile(r'\[Workspace::v1:\s*(?:\\.|[^\]\\])+\]\s*')
_LEGACY_WORKSPACE_PREFIX_ANY_RE = re.compile(r'\[Workspace:[^\]]+\]\s*')


def _user_content_text(value) -> str:
    """Extract text from user content without depending on stream rendering."""
    if not isinstance(value, list):
        return str(value or "").strip()
    parts = []
    for item in value:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").lower()
        if item_type not in {"", "text", "input_text", "output_text"}:
            continue
        for key in ("text", "content", "value"):
            text = item.get(key)
            if isinstance(text, str) and text:
                parts.append(text)
                break
    return "\n".join(parts).strip()


def _escape_workspace_prefix_path(path: str) -> str:
    return str(path or "").replace("\\", "\\\\").replace("]", "\\]")


def _workspace_context_prefix(path: str) -> str:
    return f"[Workspace::v1: {_escape_workspace_prefix_path(path)}]\n"


def _strip_workspace_prefix(text: str, *, include_legacy: bool = False) -> str:
    """Remove a WebUI workspace tag without eating user-authored text."""
    value = str(text or "")
    stripped = _WORKSPACE_PREFIX_RE.sub("", value, count=1)
    if include_legacy and stripped == value:
        stripped = _LEGACY_WORKSPACE_PREFIX_RE.sub("", value, count=1)
    return stripped.strip()


def _looks_like_current_user_turn(msg, msg_text) -> bool:
    """Match the submitted human turn even after a workspace-tag retry merge."""
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return False
    needle = " ".join(str(msg_text or "").split())
    if not needle:
        return False
    text = _user_content_text(msg.get("content", ""))
    candidates = [_strip_workspace_prefix(text, include_legacy=True)]
    for pattern in (_WORKSPACE_PREFIX_ANY_RE, _LEGACY_WORKSPACE_PREFIX_ANY_RE):
        for match in pattern.finditer(text):
            candidates.append(text[match.end():])
    return any(" ".join(str(candidate or "").split()) == needle for candidate in candidates)


__all__ = [
    "_LEGACY_WORKSPACE_PREFIX_ANY_RE",
    "_WORKSPACE_PREFIX_ANY_RE",
    "_looks_like_current_user_turn",
    "_strip_workspace_prefix",
    "_workspace_context_prefix",
]
