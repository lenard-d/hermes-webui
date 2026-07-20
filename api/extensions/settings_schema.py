"""Fail-closed browser settings-schema sanitization for extensions."""

import math
import re
from typing import Dict, List, Optional, Set, Tuple


_SETTING_TYPES = {"boolean", "string", "number", "integer", "enum"}
_SETTINGS_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
_DEFAULT_MISSING = object()


def manifest_entry_storage_owned(entry: Dict[str, object]) -> bool:
    permissions = entry.get("permissions")
    if not isinstance(permissions, dict):
        return False
    storage = permissions.get("storage")
    return isinstance(storage, dict) and storage.get("owned") is True


def _text(value: object, *, max_len: int = 160) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:max_len]


def _normalize_enum_options(options: object) -> Optional[List[Dict[str, str]]]:
    if not isinstance(options, list) or not options:
        return None
    normalized: List[Dict[str, str]] = []
    seen: Set[str] = set()
    for option in options:
        if isinstance(option, str):
            value = option.strip()
            label = value
        elif isinstance(option, dict):
            raw_value = option.get("value")
            if not isinstance(raw_value, str):
                return None
            value = raw_value.strip()
            label = _text(option.get("label")) or value
        else:
            return None
        if not value or value in seen:
            return None
        seen.add(value)
        normalized.append({"value": value, "label": label})
    return normalized


def _normalize_default(
    field_type: str,
    raw_default: object,
    options: Optional[List[Dict[str, str]]] = None,
) -> Tuple[bool, object]:
    if field_type == "boolean":
        if raw_default is _DEFAULT_MISSING:
            return True, False
        return (True, raw_default) if isinstance(raw_default, bool) else (False, None)
    if field_type == "string":
        if raw_default is _DEFAULT_MISSING:
            return True, ""
        return (True, raw_default) if isinstance(raw_default, str) else (False, None)
    if field_type == "number":
        if raw_default is _DEFAULT_MISSING:
            return True, 0
        valid = (
            isinstance(raw_default, (int, float))
            and not isinstance(raw_default, bool)
            and math.isfinite(raw_default)
        )
        return (True, raw_default) if valid else (False, None)
    if field_type == "integer":
        if raw_default is _DEFAULT_MISSING:
            return True, 0
        valid = isinstance(raw_default, int) and not isinstance(raw_default, bool)
        return (True, raw_default) if valid else (False, None)
    if field_type == "enum" and options:
        values = [option["value"] for option in options]
        if raw_default is _DEFAULT_MISSING:
            return True, values[0]
        valid = isinstance(raw_default, str) and raw_default in values
        return (True, raw_default) if valid else (False, None)
    return False, None


def _schema_values(raw_schema: object) -> List[object]:
    if isinstance(raw_schema, list):
        return raw_schema
    if isinstance(raw_schema, dict) and isinstance(raw_schema.get("fields"), list):
        return raw_schema["fields"]
    return []


def sanitize_settings_schema(entry: Dict[str, object]) -> List[Dict[str, object]]:
    """Project a safe, non-sensitive schema only for owned browser storage."""
    if not manifest_entry_storage_owned(entry):
        return []
    fields: List[Dict[str, object]] = []
    seen_keys: Set[str] = set()
    for raw_field in _schema_values(entry.get("settings_schema")):
        if not isinstance(raw_field, dict) or raw_field.get("sensitive") is True:
            continue
        key = raw_field.get("key")
        if not isinstance(key, str):
            continue
        key = key.strip()
        if not _SETTINGS_KEY_RE.fullmatch(key) or key in seen_keys:
            continue
        field_type = raw_field.get("type")
        if not isinstance(field_type, str):
            continue
        field_type = field_type.strip().lower()
        if field_type not in _SETTING_TYPES:
            continue
        options = (
            _normalize_enum_options(raw_field.get("options"))
            if field_type == "enum"
            else None
        )
        if field_type == "enum" and options is None:
            continue
        valid, default = _normalize_default(
            field_type,
            raw_field.get("default", _DEFAULT_MISSING),
            options,
        )
        if not valid:
            continue
        seen_keys.add(key)
        field: Dict[str, object] = {
            "key": key,
            "type": field_type,
            "label": _text(raw_field.get("label")) or key,
            "description": _text(raw_field.get("description"), max_len=300),
            "default": default,
        }
        if options is not None:
            field["options"] = options
        fields.append(field)
    return fields


__all__ = ["manifest_entry_storage_owned", "sanitize_settings_schema"]
