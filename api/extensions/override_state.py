"""Atomic persistence for extension enablement and sidecar consent overrides."""

import json
import os
import threading
from typing import Any, Dict, List, Optional, Set

from .diagnostics import add_diagnostic_warning
from .identity import normalize_extension_id
from . import roots


EXTENSION_STATE_LOCK = threading.Lock()
_STATE_FILENAME = "extension-overrides.json"
_MAX_STATE_BYTES = 32 * 1024
_MAX_DISABLED_IDS = 512
_MAX_SIDECAR_CONSENTS = 512
_WARNING_SOURCE = "extension_state"


def _state_file():
    return roots.extension_state_dir() / _STATE_FILENAME


def empty_extension_state() -> Dict[str, Any]:
    return {"version": 1, "disabled_extensions": [], "sidecar_proxy_consents": {}}


def load_extension_state(
    diagnostics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Load overrides with bounded input and fail-closed consent parsing."""
    from .sidecars import _normalize_loopback_sidecar_origin

    state_file = _state_file()
    try:
        if not state_file.exists() or not state_file.is_file():
            return empty_extension_state()
        with state_file.open("rb") as handle:
            raw = handle.read(_MAX_STATE_BYTES + 1)
        if len(raw) > _MAX_STATE_BYTES:
            add_diagnostic_warning(diagnostics, "extension_state_oversized", _WARNING_SOURCE)
            return empty_extension_state()
        parsed = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        add_diagnostic_warning(diagnostics, "extension_state_unreadable", _WARNING_SOURCE)
        return empty_extension_state()
    if not isinstance(parsed, dict):
        add_diagnostic_warning(diagnostics, "extension_state_invalid", _WARNING_SOURCE)
        return empty_extension_state()
    disabled_raw = parsed.get("disabled_extensions", [])
    if not isinstance(disabled_raw, list):
        add_diagnostic_warning(diagnostics, "extension_state_invalid", _WARNING_SOURCE)
        return empty_extension_state()

    disabled: List[str] = []
    seen: Set[str] = set()
    invalid = False
    for value in disabled_raw:
        extension_id = normalize_extension_id(value)
        if extension_id is None:
            invalid = True
            continue
        if extension_id in seen:
            continue
        seen.add(extension_id)
        disabled.append(extension_id)
        if len(disabled) >= _MAX_DISABLED_IDS:
            add_diagnostic_warning(diagnostics, "extension_state_truncated", _WARNING_SOURCE)
            break

    consents_raw = parsed.get("sidecar_proxy_consents", {})
    consents: Dict[str, str] = {}
    consents_invalid = False
    if consents_raw is not None:
        if not isinstance(consents_raw, dict):
            invalid = True
            consents_invalid = True
        else:
            for raw_extension_id, raw_origin in consents_raw.items():
                extension_id = normalize_extension_id(raw_extension_id)
                origin = _normalize_loopback_sidecar_origin(raw_origin)
                if extension_id is None or origin is None:
                    invalid = True
                    consents_invalid = True
                    continue
                if extension_id in consents:
                    continue
                consents[extension_id] = origin
                if len(consents) >= _MAX_SIDECAR_CONSENTS:
                    add_diagnostic_warning(
                        diagnostics, "extension_state_truncated", _WARNING_SOURCE
                    )
                    break
    if consents_invalid:
        consents = {}
    if invalid:
        add_diagnostic_warning(
            diagnostics, "extension_state_invalid_entries", _WARNING_SOURCE
        )
    return {
        "version": 1,
        "disabled_extensions": disabled,
        "sidecar_proxy_consents": consents,
    }


def _normalized_state_payload(state: Dict[str, Any]) -> Dict[str, Any]:
    from .sidecars import _normalize_loopback_sidecar_origin

    disabled: List[str] = []
    seen: Set[str] = set()
    disabled_raw = state.get("disabled_extensions", [])
    if isinstance(disabled_raw, list):
        for value in disabled_raw:
            extension_id = normalize_extension_id(value)
            if extension_id is None or extension_id in seen:
                continue
            seen.add(extension_id)
            disabled.append(extension_id)
            if len(disabled) >= _MAX_DISABLED_IDS:
                break

    consents: Dict[str, str] = {}
    consents_raw = state.get("sidecar_proxy_consents", {})
    if isinstance(consents_raw, dict):
        for raw_extension_id, raw_origin in consents_raw.items():
            extension_id = normalize_extension_id(raw_extension_id)
            origin = _normalize_loopback_sidecar_origin(raw_origin)
            if extension_id is None or origin is None or extension_id in consents:
                continue
            consents[extension_id] = origin
            if len(consents) >= _MAX_SIDECAR_CONSENTS:
                break
    return {
        "version": 1,
        "disabled_extensions": disabled,
        "sidecar_proxy_consents": consents,
    }


def write_extension_state(state: Dict[str, Any]) -> None:
    """Persist normalized overrides with fsync and same-directory replace."""
    payload = _normalized_state_payload(state)
    target = _state_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


__all__ = [
    "EXTENSION_STATE_LOCK",
    "empty_extension_state",
    "load_extension_state",
    "write_extension_state",
]
