"""Public extension runtime configuration and enablement interface."""

from typing import Any, Dict, Set

from .asset_urls import _read_url_list
from .diagnostics import new_diagnostics
from .errors import ExtensionToggleError
from .identity import normalize_extension_id
from .manifest import (
    _extension_runtime_entries,
    _load_manifest_with_status,
    _manifest_extension_state,
    _read_manifest_urls_with_diagnostics,
)
from .override_state import (
    EXTENSION_STATE_LOCK,
    load_extension_state,
    write_extension_state,
)
from .roots import (
    EXTENSION_ROUTE_PREFIX,
    EXTENSION_SCRIPT_URLS_ENV,
    EXTENSION_STYLESHEET_URLS_ENV,
    extension_root,
)
from .status import get_extension_status


def get_extension_config() -> Dict[str, Any]:
    """Return app-shell extension config without exposing filesystem paths."""
    root = extension_root()
    if root is None:
        return {"enabled": False, "script_urls": [], "stylesheet_urls": []}
    state = load_extension_state()
    disabled_ids = set(state.get("disabled_extensions") or [])
    manifest, manifest_status = _load_manifest_with_status(root)
    scripts, stylesheets, _, _ = _read_manifest_urls_with_diagnostics(
        root,
        disabled_ids=disabled_ids,
        manifest=manifest,
        manifest_status=manifest_status,
    )
    config = {
        "enabled": True,
        "script_urls": _read_url_list(EXTENSION_SCRIPT_URLS_ENV, scripts or None),
        "stylesheet_urls": _read_url_list(
            EXTENSION_STYLESHEET_URLS_ENV, stylesheets or None
        ),
    }
    runtime_entries = (
        _extension_runtime_entries(manifest, disabled_ids)
        if manifest is not None
        else []
    )
    if runtime_entries:
        config["extensions"] = runtime_entries
    return config


def set_extension_user_enabled(extension_id: object, enabled: object) -> Dict[str, Any]:
    """Persist the UI-managed enablement override for one known extension."""
    normalized_id = normalize_extension_id(extension_id)
    if normalized_id is None:
        raise ExtensionToggleError("Invalid extension id", status=400)
    if not isinstance(enabled, bool):
        raise ExtensionToggleError("enabled must be a boolean", status=400)
    root = extension_root()
    if root is None:
        raise ExtensionToggleError("Extensions are not configured", status=404)

    with EXTENSION_STATE_LOCK:
        diagnostics = new_diagnostics()
        state = load_extension_state(diagnostics)
        disabled_ids = set(state.get("disabled_extensions") or [])
        consent_map = state.get("sidecar_proxy_consents") or {}
        manifest, manifest_status = _load_manifest_with_status(root, diagnostics)
        if manifest is None or not manifest_status.get("loaded", False):
            raise ExtensionToggleError("Extension manifest is not loaded", status=409)
        extension_state = _manifest_extension_state(
            manifest,
            disabled_ids,
            diagnostics,
            consent_ids=set(consent_map.keys()),
        )
        known_ids: Set[str] = extension_state["known_ids"]
        manifest_disabled_ids: Set[str] = extension_state["manifest_disabled_ids"]
        if normalized_id not in known_ids:
            raise ExtensionToggleError("Extension not found", status=404)
        if normalized_id in manifest_disabled_ids:
            raise ExtensionToggleError(
                "Extension is disabled by its manifest", status=409
            )
        if enabled:
            disabled_ids.discard(normalized_id)
        else:
            disabled_ids.add(normalized_id)
        write_extension_state(
            {
                "disabled_extensions": sorted(disabled_ids),
                "sidecar_proxy_consents": {
                    consent_id: origin
                    for consent_id, origin in consent_map.items()
                    if consent_id in known_ids
                },
            }
        )

    # Readback happens after the atomic write and outside the shared mutation lock.
    return get_extension_status()


__all__ = [
    "EXTENSION_ROUTE_PREFIX",
    "get_extension_config",
    "get_extension_status",
    "set_extension_user_enabled",
]
