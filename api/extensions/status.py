"""Sanitized administrator status projection for extensions."""

import os
from typing import Any, Dict

from .asset_urls import _read_url_list
from .diagnostics import add_diagnostic_warning, new_diagnostics
from .gallery import _load_install_manifest
from .manifest import (
    _load_manifest_with_status,
    _manifest_extension_state,
    _read_manifest_urls_with_diagnostics,
)
from .override_state import load_extension_state
from .roots import (
    EXTENSION_DIR_ENV,
    EXTENSION_MANIFEST_ENV,
    EXTENSION_SCRIPT_URLS_ENV,
    EXTENSION_STYLESHEET_URLS_ENV,
    extension_root_status,
)


def _disabled_status(
    *,
    directory_configured: bool,
    manifest_status: Dict[str, Any],
    diagnostics: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "enabled": False,
        "extension_dir_configured": directory_configured,
        "extension_dir_valid": False,
        "script_urls": [],
        "stylesheet_urls": [],
        "sidecars": [],
        "counts": {
            "script_urls": 0,
            "stylesheet_urls": 0,
            "sidecars": 0,
            "manifest_extensions": 0,
            "user_disabled": 0,
        },
        "manifest": manifest_status,
        "extensions": [],
        "warnings": diagnostics["warnings"],
    }


def get_extension_status() -> Dict[str, Any]:
    """Return sanitized extension diagnostics for administrators."""
    diagnostics = new_diagnostics()
    root, directory_configured, directory_valid = extension_root_status()
    state = load_extension_state(diagnostics)
    disabled_ids = set(state.get("disabled_extensions") or [])
    manifest_configured = bool(os.getenv(EXTENSION_MANIFEST_ENV, "").strip())
    manifest_status: Dict[str, Any] = {
        "configured": manifest_configured,
        "loaded": False,
        "status": "extension_disabled" if manifest_configured else "not_configured",
        "entry_count": 0,
        "script_count": 0,
        "stylesheet_count": 0,
        "sidecar_count": 0,
    }
    if (
        os.getenv(EXTENSION_DIR_ENV, "").strip()
        and directory_configured
        and not directory_valid
    ):
        add_diagnostic_warning(
            diagnostics, "extension_dir_unavailable", "extension_dir"
        )
    if root is None:
        return _disabled_status(
            directory_configured=directory_configured,
            manifest_status=manifest_status,
            diagnostics=diagnostics,
        )

    manifest, manifest_status = _load_manifest_with_status(root, diagnostics)
    consent_ids = set((state.get("sidecar_proxy_consents") or {}).keys())
    extension_state = (
        _manifest_extension_state(
            manifest,
            disabled_ids,
            diagnostics,
            consent_ids=consent_ids,
        )
        if manifest is not None
        else {"extensions": [], "known_ids": set(), "manifest_disabled_ids": set()}
    )
    scripts, stylesheets, sidecars, manifest_status = (
        _read_manifest_urls_with_diagnostics(
            root,
            diagnostics,
            disabled_ids=disabled_ids,
            manifest=manifest,
            manifest_status=manifest_status,
            state=state,
        )
    )
    extensions = extension_state["extensions"]
    known_ids = extension_state["known_ids"]
    script_urls = _read_url_list(
        EXTENSION_SCRIPT_URLS_ENV, scripts or None, diagnostics=diagnostics
    )
    stylesheet_urls = _read_url_list(
        EXTENSION_STYLESHEET_URLS_ENV,
        stylesheets or None,
        diagnostics=diagnostics,
    )
    public_manifest_status = {
        key: value for key, value in manifest_status.items() if not key.startswith("_")
    }
    return {
        "enabled": True,
        "extension_dir_configured": True,
        "extension_dir_valid": True,
        "script_urls": script_urls,
        "stylesheet_urls": stylesheet_urls,
        "sidecars": sidecars,
        "counts": {
            "script_urls": len(script_urls),
            "stylesheet_urls": len(stylesheet_urls),
            "sidecars": len(sidecars),
            "manifest_extensions": len(extensions),
            "user_disabled": len(disabled_ids & known_ids),
        },
        "manifest": public_manifest_status,
        "extensions": extensions,
        "gallery_installed": _load_install_manifest().get("installed", {}),
        "warnings": diagnostics["warnings"],
    }


__all__ = ["get_extension_status"]
