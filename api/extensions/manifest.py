"""Manifest loading, normalization, and runtime asset projection."""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit

from .diagnostics import add_diagnostic_warning
from .identity import is_valid_extension_id
from .roots import EXTENSION_MANIFEST_ENV, EXTENSION_ROUTE_PREFIX
from .settings_schema import manifest_entry_storage_owned, sanitize_settings_schema

_log = logging.getLogger("api.extensions")

# Keep extension manifests small and auditable. The manifest is a convenience for
# bundling static assets, not a package manager or dependency lockfile.
_MAX_MANIFEST_BYTES = 64 * 1024

class _ManifestTooLarge(ValueError):
    pass

_EXTENSION_STATE_WARNING_SOURCE = "extension_state"


def _manifest_path_with_status(root: Path) -> Tuple[Optional[Path], str]:
    from .asset_urls import _fully_unquote_path, _is_safe_relative_path

    raw = os.getenv(EXTENSION_MANIFEST_ENV, "").strip()
    if not raw:
        return None, "not_configured"
    if raw.startswith(("/", "~")):
        _log.warning("Rejected extension manifest path from %s", EXTENSION_MANIFEST_ENV)
        return None, "invalid_path"
    rel = _fully_unquote_path(raw)
    if not _is_safe_relative_path(rel):
        _log.warning("Rejected extension manifest path from %s", EXTENSION_MANIFEST_ENV)
        return None, "invalid_path"
    manifest = (root / rel).resolve()
    try:
        manifest.relative_to(root)
    except ValueError:
        _log.warning("Rejected extension manifest path from %s", EXTENSION_MANIFEST_ENV)
        return None, "invalid_path"
    return manifest, "configured"


def _manifest_path(root: Path) -> Optional[Path]:
    manifest, _ = _manifest_path_with_status(root)
    return manifest


def _manifest_asset_url(value: object, asset_base: str = "") -> str:
    """Normalize a manifest asset entry to the existing same-origin URL format."""
    if not isinstance(value, str):
        return ""
    item = value.strip()
    if not item:
        return ""
    parsed = urlsplit(item)
    if parsed.scheme or parsed.netloc or item.startswith("//"):
        return item
    # Manifests are meant to make bundled local assets less noisy to list, so
    # bare relative paths resolve under /extensions/. Absolute same-origin paths
    # are still allowed and go through the same validator as env-configured URLs.
    if item.startswith("/"):
        return item
    base = asset_base.strip("/")
    rel = f"{base}/{item}" if base else item
    return EXTENSION_ROUTE_PREFIX + rel


def _manifest_asset_value_with_base(value: object, asset_base: str) -> object:
    """Rewrite a manifest asset value so it remains relative to its manifest file."""
    if not isinstance(value, str):
        return value
    item = value.strip()
    if not item:
        return item
    parsed = urlsplit(item)
    if parsed.scheme or parsed.netloc or item.startswith("//") or item.startswith("/"):
        return item
    base = asset_base.strip("/")
    return f"{base}/{item}" if base else item


def _copy_manifest_entry_with_asset_base(entry: Dict[str, object], asset_base: str) -> Dict[str, object]:
    copied = dict(entry)
    for key in ("scripts", "stylesheets"):
        values = copied.get(key)
        if isinstance(values, list):
            copied[key] = [_manifest_asset_value_with_base(value, asset_base) for value in values]
    return copied


def _manifest_entry_text(entry: Dict[str, object], key: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str):
        return ""
    return value.strip()

def _manifest_extension_entries(manifest: object) -> List[Tuple[str, int, Dict[str, object]]]:
    extension_entries: object = []
    if isinstance(manifest, dict):
        extension_entries = manifest.get("extensions", [])
    elif isinstance(manifest, list):
        extension_entries = manifest
    entries: List[Tuple[str, int, Dict[str, object]]] = []
    if isinstance(extension_entries, list):
        for index, extension in enumerate(extension_entries):
            if isinstance(extension, dict):
                entries.append((f"manifest.extensions[{index}]", index, extension))
    return entries


def _iter_manifest_entries(
    manifest: object, disabled_ids: Optional[Set[str]] = None
) -> List[Tuple[str, object]]:
    disabled_ids = disabled_ids or set()
    entries: List[Tuple[str, object]] = []
    if isinstance(manifest, dict):
        entries.append(("manifest", manifest))
    for source, _index, extension in _manifest_extension_entries(manifest):
        if extension.get("enabled", True) is False:
            continue
        ext_id = _manifest_entry_text(extension, "id")
        if is_valid_extension_id(ext_id) and ext_id in disabled_ids:
            continue
        entries.append((source, extension))
    return entries


def _entry_asset_values(entry: Dict[str, object], key: str) -> List[object]:
    values = entry.get(key, [])
    return values if isinstance(values, list) else []


def _read_manifest_text(manifest_file: Path) -> str:
    with manifest_file.open("rb") as fh:
        data = fh.read(_MAX_MANIFEST_BYTES + 1)
    if len(data) > _MAX_MANIFEST_BYTES:
        raise _ManifestTooLarge("manifest too large")
    return data.decode("utf-8")


def _empty_manifest_status(path_status: str) -> Dict[str, Any]:
    return {
        "configured": path_status != "not_configured",
        "loaded": False,
        "status": path_status,
        "_asset_base": "",
        "entry_count": 0,
        "script_count": 0,
        "stylesheet_count": 0,
        "sidecar_count": 0,
    }


def _manifest_asset_base(root: Path, manifest_file: Path) -> str:
    try:
        rel_parent = manifest_file.parent.relative_to(root).as_posix()
    except ValueError:
        return ""
    return "" if rel_parent == "." else rel_parent


def _gallery_installed_runtime_manifest(
    root: Path, diagnostics: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, object]]:
    """Build a runtime manifest from gallery-installed extension manifests."""
    from .gallery import _load_install_manifest

    install_manifest = _load_install_manifest()
    installed = install_manifest.get("installed", {})
    if not isinstance(installed, dict):
        return None
    entries: List[Dict[str, object]] = []
    for ext_id in sorted(installed):
        if not is_valid_extension_id(ext_id):
            continue
        manifest_file = root / ext_id / "manifest.json"
        try:
            if not manifest_file.exists() or not manifest_file.is_file():
                add_diagnostic_warning(diagnostics, "gallery_manifest_missing", "gallery")
                continue
            manifest = json.loads(_read_manifest_text(manifest_file))
        except _ManifestTooLarge:
            add_diagnostic_warning(diagnostics, "gallery_manifest_oversized", "gallery")
            continue
        except json.JSONDecodeError:
            add_diagnostic_warning(diagnostics, "gallery_manifest_malformed", "gallery")
            continue
        except RecursionError:
            add_diagnostic_warning(diagnostics, "gallery_manifest_too_deeply_nested", "gallery")
            continue
        except (OSError, UnicodeDecodeError):
            add_diagnostic_warning(diagnostics, "gallery_manifest_unreadable", "gallery")
            continue
        asset_base = ext_id
        if isinstance(manifest, dict):
            top_entry: Dict[str, object] = {"id": ext_id}
            for key in ("name", "enabled", "scripts", "stylesheets", "sidecar", "permissions", "settings_schema"):
                if key in manifest:
                    top_entry[key] = manifest[key]
            if any(
                key in top_entry
                for key in ("scripts", "stylesheets", "sidecar", "permissions", "settings_schema")
            ):
                entries.append(_copy_manifest_entry_with_asset_base(top_entry, asset_base))
        for _source, _index, entry in _manifest_extension_entries(manifest):
            copied = _copy_manifest_entry_with_asset_base(entry, asset_base)
            if not is_valid_extension_id(copied.get("id")):
                copied["id"] = ext_id
            entries.append(copied)
    if not entries:
        return None
    return {"extensions": entries}


def _load_manifest_with_status(
    root: Path, diagnostics: Optional[Dict[str, Any]] = None
) -> Tuple[Optional[object], Dict[str, Any]]:
    """Load the configured manifest once, returning sanitized status/warnings."""
    manifest_file, path_status = _manifest_path_with_status(root)
    manifest_status = _empty_manifest_status(path_status)
    if manifest_file is None:
        if path_status == "invalid_path":
            add_diagnostic_warning(diagnostics, "manifest_invalid_path", "manifest")
        elif path_status == "not_configured":
            manifest = _gallery_installed_runtime_manifest(root, diagnostics)
            if manifest is not None:
                manifest_status.update(
                    {
                        "loaded": True,
                        "status": "gallery_installed",
                        "_asset_base": "",
                    }
                )
                return manifest, manifest_status
        return None, manifest_status
    try:
        if not manifest_file.exists() or not manifest_file.is_file():
            _log.warning("Configured extension manifest was not found")
            manifest_status["status"] = "missing"
            add_diagnostic_warning(diagnostics, "manifest_missing", "manifest")
            return None, manifest_status
        manifest = json.loads(_read_manifest_text(manifest_file))
        manifest_status.update(
            {
                "loaded": True,
                "status": "loaded",
                "_asset_base": _manifest_asset_base(root, manifest_file),
            }
        )
        return manifest, manifest_status
    except _ManifestTooLarge:
        _log.warning("Configured extension manifest exceeds %d bytes", _MAX_MANIFEST_BYTES)
        manifest_status["status"] = "oversized"
        add_diagnostic_warning(diagnostics, "manifest_oversized", "manifest")
    except json.JSONDecodeError:
        _log.warning("Configured extension manifest is not valid JSON")
        manifest_status["status"] = "malformed"
        add_diagnostic_warning(diagnostics, "manifest_malformed", "manifest")
    except RecursionError:
        # A <=64KB but deeply-nested manifest makes json.loads exceed the
        # interpreter recursion limit. Without this, the RecursionError escapes
        # into the app-shell route and every page load 503s. Fail safe.
        _log.warning("Configured extension manifest is too deeply nested")
        manifest_status["status"] = "too_deeply_nested"
        add_diagnostic_warning(diagnostics, "manifest_too_deeply_nested", "manifest")
    except (OSError, UnicodeDecodeError):
        _log.warning("Configured extension manifest could not be read")
        manifest_status["status"] = "unreadable"
        add_diagnostic_warning(diagnostics, "manifest_unreadable", "manifest")
    return None, manifest_status


def _manifest_extension_state(
    manifest: object,
    disabled_ids: Set[str],
    diagnostics: Optional[Dict[str, Any]] = None,
    consent_ids: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """Return sanitized per-extension state for manifest extension entries."""
    extension_entries: List[Dict[str, Any]] = []
    known_ids: Set[str] = set()
    manifest_disabled_ids: Set[str] = set()
    seen_ids: Set[str] = set()
    invalid_seen = False
    duplicate_seen = False
    for _source, _index, entry in _manifest_extension_entries(manifest):
        raw_id = _manifest_entry_text(entry, "id")
        if not is_valid_extension_id(raw_id):
            invalid_seen = True
            continue
        ext_id = raw_id.strip()
        if ext_id in seen_ids:
            duplicate_seen = True
            continue
        seen_ids.add(ext_id)
        known_ids.add(ext_id)
        name = _manifest_entry_text(entry, "name")
        manifest_enabled = entry.get("enabled", True) is not False
        user_disabled = ext_id in disabled_ids
        can_toggle = manifest_enabled
        effective_enabled = manifest_enabled and not user_disabled
        if not manifest_enabled:
            manifest_disabled_ids.add(ext_id)
        settings_schema = sanitize_settings_schema(entry)
        extension_entries.append(
            {
                "id": ext_id,
                "name": name or ext_id,
                "manifest_enabled": manifest_enabled,
                "user_enabled": (not user_disabled) if can_toggle else False,
                "user_disabled": user_disabled,
                "effective_enabled": effective_enabled,
                "can_toggle": can_toggle,
                "reload_required": True,
                "storage_owned": manifest_entry_storage_owned(entry),
                "settings_schema": settings_schema,
                "status": (
                    "manifest_disabled"
                    if not manifest_enabled
                    else ("user_disabled" if user_disabled else "enabled")
                ),
            }
        )
    if invalid_seen:
        add_diagnostic_warning(diagnostics, "manifest_extension_id_invalid", "manifest:extensions")
    if duplicate_seen:
        add_diagnostic_warning(diagnostics, "manifest_extension_id_duplicate", "manifest:extensions")
    stale_ids = sorted((disabled_ids | (consent_ids or set())) - known_ids)
    if stale_ids:
        add_diagnostic_warning(diagnostics, "extension_state_unknown_ids", _EXTENSION_STATE_WARNING_SOURCE)
    return {
        "extensions": extension_entries,
        "known_ids": known_ids,
        "manifest_disabled_ids": manifest_disabled_ids,
    }


def _extension_runtime_entries(
    manifest: object, disabled_ids: Optional[Set[str]] = None
) -> List[Dict[str, object]]:
    """Return enabled extension metadata injected before extension scripts run."""
    disabled_ids = disabled_ids or set()
    extensions: List[Dict[str, object]] = []
    seen_ids: Set[str] = set()
    for _source, _index, entry in _manifest_extension_entries(manifest):
        raw_id = _manifest_entry_text(entry, "id")
        if not is_valid_extension_id(raw_id):
            continue
        ext_id = raw_id.strip()
        if ext_id in seen_ids:
            continue
        seen_ids.add(ext_id)
        if entry.get("enabled", True) is False or ext_id in disabled_ids:
            continue
        extensions.append(
            {
                "id": ext_id,
                "name": _manifest_entry_text(entry, "name") or ext_id,
                "storage_owned": manifest_entry_storage_owned(entry),
                "settings_schema": sanitize_settings_schema(entry),
            }
        )
    return extensions


def _read_manifest_urls_with_diagnostics(
    root: Path,
    diagnostics: Optional[Dict[str, Any]] = None,
    disabled_ids: Optional[Set[str]] = None,
    manifest: Optional[object] = None,
    manifest_status: Optional[Dict[str, Any]] = None,
    state: Optional[Dict[str, Any]] = None,
) -> Tuple[List[str], List[str], List[Dict[str, Any]], Dict[str, Any]]:
    from .asset_urls import _append_safe_asset_url
    from .sidecars import _extension_sidecar_records

    disabled_ids = disabled_ids or set()
    if manifest is None or manifest_status is None:
        manifest, manifest_status = _load_manifest_with_status(root, diagnostics)
    if manifest is None:
        return [], [], [], manifest_status

    scripts: List[str] = []
    stylesheets: List[str] = []
    sidecars: List[Dict[str, Any]] = []
    asset_base = str(manifest_status.get("_asset_base", "") or "")
    entries = _iter_manifest_entries(manifest, disabled_ids=disabled_ids)
    manifest_status["entry_count"] = len(entries)
    scripts_full = False
    stylesheets_full = False
    for _source, entry in entries:
        if not isinstance(entry, dict):
            continue
        script_source = "manifest:scripts"
        stylesheet_source = "manifest:stylesheets"
        if not scripts_full:
            for value in _entry_asset_values(entry, "scripts"):
                if not _append_safe_asset_url(
                    scripts,
                    _manifest_asset_url(value, asset_base),
                    script_source,
                    diagnostics=diagnostics,
                ):
                    scripts_full = True
                    break
        if not stylesheets_full:
            for value in _entry_asset_values(entry, "stylesheets"):
                if not _append_safe_asset_url(
                    stylesheets,
                    _manifest_asset_url(value, asset_base),
                    stylesheet_source,
                    diagnostics=diagnostics,
                ):
                    stylesheets_full = True
                    break
    sidecars, _ = _extension_sidecar_records(
        manifest,
        disabled_ids=disabled_ids,
        state=state,
        diagnostics=diagnostics,
    )
    manifest_status.update(
        {
            "loaded": True,
            "status": manifest_status.get("status") or "loaded",
            "script_count": len(scripts),
            "stylesheet_count": len(stylesheets),
            "sidecar_count": len(sidecars),
        }
    )
    return scripts, stylesheets, sidecars, manifest_status


def _read_manifest_urls(
    root: Path, disabled_ids: Optional[Set[str]] = None
) -> Tuple[List[str], List[str]]:
    scripts, stylesheets, _, _ = _read_manifest_urls_with_diagnostics(
        root, disabled_ids=disabled_ids
    )
    return scripts, stylesheets
