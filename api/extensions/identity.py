"""Canonical extension identity validation."""

import re
from typing import Optional


_EXTENSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def normalize_extension_id(value: object) -> Optional[str]:
    """Return the canonical extension id, or ``None`` for hostile input."""
    if not isinstance(value, str):
        return None
    extension_id = value.strip()
    if not _EXTENSION_ID_RE.fullmatch(extension_id):
        return None
    return extension_id


def is_valid_extension_id(value: object) -> bool:
    """Return whether *value* is a valid extension identity."""
    return normalize_extension_id(value) is not None


__all__ = ["is_valid_extension_id", "normalize_extension_id"]
