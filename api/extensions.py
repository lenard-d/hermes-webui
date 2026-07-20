"""Opt-in WebUI extension hooks.

This module intentionally provides a small, self-hosted extension surface:
configured same-origin script/style injection plus sandboxed static file serving.
It is disabled by default and never executes or fetches third-party URLs.
"""

import json
import math
import logging
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit

from api.helpers import _security_headers, j  # noqa: F401 - late-bound part dependencies
from api.extensions_parts.facade import bind_extensions_api


bind_extensions_api(lambda: sys.modules[__name__])

_log = logging.getLogger(__name__)

# Sane bound on configured URLs — real extensions ship 1-3 files. Higher values
# typically indicate a misconfiguration (one giant unsplit string, or a runaway
# generator script that wrote an env-var template without filtering). Capping
# avoids rendering tens of thousands of <script> tags into every page load.
_MAX_URL_LIST = 32

# Keep extension manifests small and auditable. The manifest is a convenience for
# bundling static assets, not a package manager or dependency lockfile.
_MAX_MANIFEST_BYTES = 64 * 1024

# Tracks rejected URL strings we've already warned about so a misconfigured env
# var doesn't spam the log on every request that re-reads it.
_warned_urls: set = set()


class _ManifestTooLarge(ValueError):
    pass


class ExtensionToggleError(Exception):
    """Sanitized extension mutation error safe to return to the browser."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class ExtensionInstallError(Exception):
    """Sanitized extension install/uninstall error safe to return to the browser."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class ExtensionSidecarProxyError(Exception):
    """Sanitized sidecar proxy error safe to return to the browser."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


from api.extensions_parts.gallery import (  # noqa: E402, F401 - compatibility exports
    _GALLERY_INSTALL_STATE_FILENAME,
    _MAX_INSTALL_MANIFEST_BYTES,
    _MAX_GALLERY_INSTALLED_IDS,
    _MAX_ZIP_DOWNLOAD_BYTES,
    _REGISTRY_URL,
    _REGISTRY_ALLOWED_DOWNLOAD_HOSTS,
    _REGISTRY_CACHE,
    _REGISTRY_LOCK,
    _REGISTRY_TTL_SECONDS,
    _AllowlistRedirectHandler,
    _connect_ipv4_first,
    _IPv4FirstHTTPSConnection,
    _IPv4FirstHTTPSHandler,
    _build_gallery_opener,
    _safe_download,
    _install_manifest_file,
    _empty_install_manifest,
    _load_install_manifest,
    _write_install_manifest,
    install_extension,
    uninstall_extension,
    get_extension_registry,
)
from api.extensions_parts.security import (  # noqa: E402, F401 - compatibility exports
    _fully_unquote_path,
    _is_safe_relative_path,
    _is_safe_asset_url,
    _warn_rejected_url,
    _append_safe_asset_url,
    _read_url_list,
    inject_extension_tags,
    _not_found,
    serve_extension_static,
)
from api.extensions_parts.sidecars import (  # noqa: E402, F401 - compatibility exports
    _normalize_loopback_sidecar_origin,
    _normalize_sidecar_health_path,
    _is_valid_sidecar_proxy_path,
    _sidecar_from_manifest_entry,
    _extension_sidecar_proxy_path,
    _sidecar_proxy_public_status,
    _extension_sidecar_records,
    _normalize_sidecar_proxy_path,
    set_extension_sidecar_proxy_consent,
    resolve_extension_sidecar_proxy_target,
)


EXTENSION_ROUTE_PREFIX = "/extensions/"
_EXTENSION_DIR_ENV = "HERMES_WEBUI_EXTENSION_DIR"
_EXTENSION_SCRIPT_URLS_ENV = "HERMES_WEBUI_EXTENSION_SCRIPT_URLS"
_EXTENSION_STYLESHEET_URLS_ENV = "HERMES_WEBUI_EXTENSION_STYLESHEET_URLS"
_EXTENSION_MANIFEST_ENV = "HERMES_WEBUI_EXTENSION_MANIFEST"
_ALLOWED_ASSET_PREFIXES = ("/extensions/", "/static/")
_SIDECAR_WARNING_SOURCE = "manifest:sidecars"
_DEFAULT_SIDECAR_HEALTH_PATH = "/health"
_LOOPBACK_SIDECAR_HOSTS = {"127.0.0.1", "localhost", "::1"}
_EXTENSION_STATE_FILENAME = "extension-overrides.json"
_MAX_EXTENSION_STATE_BYTES = 32 * 1024
_MAX_DISABLED_EXTENSION_IDS = 512
_MAX_SIDECAR_PROXY_CONSENTS = 512
_EXTENSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_EXTENSION_SETTINGS_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
_EXTENSION_STATE_WARNING_SOURCE = "extension_state"
_EXTENSION_STATE_LOCK = threading.Lock()
_EXTENSION_SETTING_TYPES = {"boolean", "string", "number", "integer", "enum"}

_EXTENSION_MIME = {
    "css": "text/css",
    "js": "application/javascript",
    "html": "text/html",
    "svg": "image/svg+xml",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "ico": "image/x-icon",
    "gif": "image/gif",
    "webp": "image/webp",
    "woff": "font/woff",
    "woff2": "font/woff2",
    "ttf": "font/ttf",
    "otf": "font/otf",
    "wasm": "application/wasm",
}
_TEXT_MIME_TYPES = {"text/css", "application/javascript", "text/html", "image/svg+xml", "text/plain"}


def _default_extension_root() -> Path:
    """WebUI-managed default extension directory under the state dir.

    Used when ``HERMES_WEBUI_EXTENSION_DIR`` is unset so one-click gallery
    install works out of the box on a single-user self-hosted instance with no
    environment setup. It lives alongside sessions/settings in the WebUI-owned
    state dir, which is a different trust domain from "a user-writable directory
    on a shared box" — the loaded code still runs with full session authority,
    so the trust model is unchanged (see docs/EXTENSIONS.md).
    """
    return _extension_state_dir() / "extensions"


def _extension_root() -> Optional[Path]:
    """Return the active extension directory, or None when none is available.

    Resolution order:
    1. ``HERMES_WEBUI_EXTENSION_DIR`` when set — must be an existing directory,
       otherwise None (the admin owns that path; we never auto-create it).
    2. Otherwise the WebUI-managed default (``STATE_DIR/extensions``) when it
       already exists. The first gallery install creates it on demand
       (see ``_writable_extension_root``); until then this stays None and the
       UI reports the same "nothing installed yet" state as before.
    """
    raw = os.getenv(_EXTENSION_DIR_ENV, "").strip()
    if raw:
        root = Path(raw).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            return None
        return root
    default_root = _default_extension_root()
    try:
        if default_root.is_dir() and not default_root.is_symlink():
            return default_root.resolve()
    except OSError:
        return None
    return None


def _writable_extension_root() -> Optional[Path]:
    """Resolve the extension root for writes, bootstrapping the managed default.

    When ``HERMES_WEBUI_EXTENSION_DIR`` is set we use it as-is (the admin owns
    it; it must already exist). When unset we create and return the
    WebUI-managed default so a fresh install can install an extension with zero
    configuration — plug and play.
    """
    raw = os.getenv(_EXTENSION_DIR_ENV, "").strip()
    if raw:
        return _extension_root()
    default_root = _default_extension_root()
    try:
        default_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    try:
        if default_root.is_symlink() or not default_root.is_dir():
            return None
        return default_root.resolve()
    except OSError:
        return None


def _extension_root_status() -> Tuple[Optional[Path], bool, bool]:
    """Return (root, configured, valid) without exposing the configured path.

    With no ``HERMES_WEBUI_EXTENSION_DIR`` the WebUI-managed default is always
    available as an install target, so ``configured`` is True (extensions are
    no longer "not configured" out of the box). ``valid`` reflects whether that
    managed directory currently exists — it is created on the first install.
    """
    raw = os.getenv(_EXTENSION_DIR_ENV, "").strip()
    if raw:
        root = Path(raw).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            return None, True, False
        return root, True, True
    root = _extension_root()
    return root, True, root is not None


def _new_diagnostics() -> Dict[str, Any]:
    return {"warnings": []}


def _add_diagnostic_warning(
    diagnostics: Optional[Dict[str, Any]], code: str, source: str
) -> None:
    """Record a sanitized diagnostic warning.

    Warnings intentionally carry only stable codes and coarse sources. They never
    include filesystem paths, raw environment values, or rejected URL strings.
    """
    if diagnostics is None:
        return
    warnings = diagnostics.setdefault("warnings", [])
    if not isinstance(warnings, list):
        return
    warning = {"code": code, "source": source}
    if warning not in warnings:
        warnings.append(warning)


def _valid_extension_id(value: object) -> bool:
    return isinstance(value, str) and bool(_EXTENSION_ID_RE.fullmatch(value.strip()))


def _extension_state_dir() -> Path:
    """Return the WebUI-managed state directory for extension overrides."""
    try:
        from api.config import STATE_DIR

        return Path(STATE_DIR)
    except Exception:
        return Path(os.getenv("HERMES_WEBUI_STATE_DIR", str(Path.home() / ".hermes" / "webui"))).expanduser()


def _extension_state_file() -> Path:
    return _extension_state_dir() / _EXTENSION_STATE_FILENAME


def _empty_extension_state() -> Dict[str, Any]:
    return {"version": 1, "disabled_extensions": [], "sidecar_proxy_consents": {}}


def _load_extension_state(diagnostics: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Load UI-managed extension overrides, failing safe without path leaks."""
    state_file = _extension_state_file()
    try:
        if not state_file.exists() or not state_file.is_file():
            return _empty_extension_state()
        with state_file.open("rb") as fh:
            raw = fh.read(_MAX_EXTENSION_STATE_BYTES + 1)
        if len(raw) > _MAX_EXTENSION_STATE_BYTES:
            _add_diagnostic_warning(
                diagnostics, "extension_state_oversized", _EXTENSION_STATE_WARNING_SOURCE
            )
            return _empty_extension_state()
        parsed = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        _add_diagnostic_warning(
            diagnostics, "extension_state_unreadable", _EXTENSION_STATE_WARNING_SOURCE
        )
        return _empty_extension_state()
    if not isinstance(parsed, dict):
        _add_diagnostic_warning(
            diagnostics, "extension_state_invalid", _EXTENSION_STATE_WARNING_SOURCE
        )
        return _empty_extension_state()
    disabled_raw = parsed.get("disabled_extensions", [])
    if not isinstance(disabled_raw, list):
        _add_diagnostic_warning(
            diagnostics, "extension_state_invalid", _EXTENSION_STATE_WARNING_SOURCE
        )
        return _empty_extension_state()
    disabled: List[str] = []
    seen: Set[str] = set()
    invalid = False
    for value in disabled_raw:
        if not _valid_extension_id(value):
            invalid = True
            continue
        ext_id = str(value).strip()
        if ext_id in seen:
            continue
        seen.add(ext_id)
        disabled.append(ext_id)
        if len(disabled) >= _MAX_DISABLED_EXTENSION_IDS:
            _add_diagnostic_warning(
                diagnostics, "extension_state_truncated", _EXTENSION_STATE_WARNING_SOURCE
            )
            break
    consents_raw = parsed.get("sidecar_proxy_consents", {})
    consents: Dict[str, str] = {}
    consents_invalid = False
    if consents_raw is not None:
        if not isinstance(consents_raw, dict):
            invalid = True
            consents_invalid = True
        else:
            for raw_ext_id, raw_origin in consents_raw.items():
                if not _valid_extension_id(raw_ext_id):
                    invalid = True
                    consents_invalid = True
                    continue
                origin = _normalize_loopback_sidecar_origin(raw_origin)
                if origin is None:
                    invalid = True
                    consents_invalid = True
                    continue
                ext_id = str(raw_ext_id).strip()
                if ext_id in consents:
                    continue
                consents[ext_id] = origin
                if len(consents) >= _MAX_SIDECAR_PROXY_CONSENTS:
                    _add_diagnostic_warning(
                        diagnostics, "extension_state_truncated", _EXTENSION_STATE_WARNING_SOURCE
                    )
                    break
    if consents_invalid:
        consents = {}
    if invalid:
        _add_diagnostic_warning(
            diagnostics, "extension_state_invalid_entries", _EXTENSION_STATE_WARNING_SOURCE
        )
    return {
        "version": 1,
        "disabled_extensions": disabled,
        "sidecar_proxy_consents": consents,
    }


def _write_extension_state(state: Dict[str, Any]) -> None:
    """Persist extension overrides with an atomic same-directory replace."""
    disabled_raw = state.get("disabled_extensions", [])
    disabled: List[str] = []
    seen: Set[str] = set()
    if isinstance(disabled_raw, list):
        for value in disabled_raw:
            if not _valid_extension_id(value):
                continue
            ext_id = str(value).strip()
            if ext_id in seen:
                continue
            seen.add(ext_id)
            disabled.append(ext_id)
            if len(disabled) >= _MAX_DISABLED_EXTENSION_IDS:
                break
    consents_raw = state.get("sidecar_proxy_consents", {})
    consents: Dict[str, str] = {}
    if isinstance(consents_raw, dict):
        for raw_ext_id, raw_origin in consents_raw.items():
            if not _valid_extension_id(raw_ext_id):
                continue
            origin = _normalize_loopback_sidecar_origin(raw_origin)
            if origin is None:
                continue
            ext_id = str(raw_ext_id).strip()
            if ext_id in consents:
                continue
            consents[ext_id] = origin
            if len(consents) >= _MAX_SIDECAR_PROXY_CONSENTS:
                break
    payload = {
        "version": 1,
        "disabled_extensions": disabled,
        "sidecar_proxy_consents": consents,
    }
    target = _extension_state_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        with tmp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _manifest_path_with_status(root: Path) -> Tuple[Optional[Path], str]:
    raw = os.getenv(_EXTENSION_MANIFEST_ENV, "").strip()
    if not raw:
        return None, "not_configured"
    if raw.startswith(("/", "~")):
        _log.warning("Rejected extension manifest path from %s", _EXTENSION_MANIFEST_ENV)
        return None, "invalid_path"
    rel = _fully_unquote_path(raw)
    if not _is_safe_relative_path(rel):
        _log.warning("Rejected extension manifest path from %s", _EXTENSION_MANIFEST_ENV)
        return None, "invalid_path"
    manifest = (root / rel).resolve()
    try:
        manifest.relative_to(root)
    except ValueError:
        _log.warning("Rejected extension manifest path from %s", _EXTENSION_MANIFEST_ENV)
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

def _manifest_entry_storage_owned(entry: Dict[str, object]) -> bool:
    permissions = entry.get("permissions")
    if not isinstance(permissions, dict):
        return False
    storage = permissions.get("storage")
    return isinstance(storage, dict) and storage.get("owned") is True

def _settings_text(value: object, *, max_len: int = 160) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    return text[:max_len]

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
            label = _settings_text(option.get("label")) or value
        else:
            return None
        if not value or value in seen:
            return None
        seen.add(value)
        normalized.append({"value": value, "label": label})
    return normalized

_SETTINGS_DEFAULT_MISSING = object()

def _normalize_settings_default(field_type: str, raw_default: object, options: Optional[List[Dict[str, str]]] = None) -> Tuple[bool, object]:
    if field_type == "boolean":
        if raw_default is _SETTINGS_DEFAULT_MISSING:
            return True, False
        return (True, raw_default) if isinstance(raw_default, bool) else (False, None)
    if field_type == "string":
        if raw_default is _SETTINGS_DEFAULT_MISSING:
            return True, ""
        return (True, raw_default) if isinstance(raw_default, str) else (False, None)
    if field_type == "number":
        if raw_default is _SETTINGS_DEFAULT_MISSING:
            return True, 0
        return (
            (True, raw_default)
            if isinstance(raw_default, (int, float)) and not isinstance(raw_default, bool) and math.isfinite(raw_default)
            else (False, None)
        )
    if field_type == "integer":
        if raw_default is _SETTINGS_DEFAULT_MISSING:
            return True, 0
        return (True, raw_default) if isinstance(raw_default, int) and not isinstance(raw_default, bool) else (False, None)
    if field_type == "enum" and options:
        values = [option["value"] for option in options]
        if raw_default is _SETTINGS_DEFAULT_MISSING:
            return True, values[0]
        return (True, raw_default) if isinstance(raw_default, str) and raw_default in values else (False, None)
    return False, None

def _settings_schema_values(raw_schema: object) -> List[object]:
    if isinstance(raw_schema, list):
        return raw_schema
    if isinstance(raw_schema, dict) and isinstance(raw_schema.get("fields"), list):
        return raw_schema["fields"]
    return []

def _sanitize_settings_schema(entry: Dict[str, object]) -> List[Dict[str, object]]:
    if not _manifest_entry_storage_owned(entry):
        return []
    fields: List[Dict[str, object]] = []
    seen_keys: Set[str] = set()
    for raw_field in _settings_schema_values(entry.get("settings_schema")):
        if not isinstance(raw_field, dict):
            continue
        if raw_field.get("sensitive") is True:
            continue
        key = raw_field.get("key")
        if not isinstance(key, str):
            continue
        key = key.strip()
        if not _EXTENSION_SETTINGS_KEY_RE.fullmatch(key):
            continue
        field_type = raw_field.get("type")
        if not isinstance(field_type, str):
            continue
        field_type = field_type.strip().lower()
        if field_type not in _EXTENSION_SETTING_TYPES:
            continue
        options = _normalize_enum_options(raw_field.get("options")) if field_type == "enum" else None
        if field_type == "enum" and options is None:
            continue
        ok, default = _normalize_settings_default(field_type, raw_field.get("default", _SETTINGS_DEFAULT_MISSING), options)
        if not ok:
            continue
        if key in seen_keys:
            continue
        seen_keys.add(key)
        field: Dict[str, object] = {
            "key": key,
            "type": field_type,
            "label": _settings_text(raw_field.get("label")) or key,
            "description": _settings_text(raw_field.get("description"), max_len=300),
            "default": default,
        }
        if options is not None:
            field["options"] = options
        fields.append(field)
    return fields


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
        if _valid_extension_id(ext_id) and ext_id in disabled_ids:
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
    install_manifest = _load_install_manifest()
    installed = install_manifest.get("installed", {})
    if not isinstance(installed, dict):
        return None
    entries: List[Dict[str, object]] = []
    for ext_id in sorted(installed):
        if not _valid_extension_id(ext_id):
            continue
        manifest_file = root / ext_id / "manifest.json"
        try:
            if not manifest_file.exists() or not manifest_file.is_file():
                _add_diagnostic_warning(diagnostics, "gallery_manifest_missing", "gallery")
                continue
            manifest = json.loads(_read_manifest_text(manifest_file))
        except _ManifestTooLarge:
            _add_diagnostic_warning(diagnostics, "gallery_manifest_oversized", "gallery")
            continue
        except json.JSONDecodeError:
            _add_diagnostic_warning(diagnostics, "gallery_manifest_malformed", "gallery")
            continue
        except RecursionError:
            _add_diagnostic_warning(diagnostics, "gallery_manifest_too_deeply_nested", "gallery")
            continue
        except (OSError, UnicodeDecodeError):
            _add_diagnostic_warning(diagnostics, "gallery_manifest_unreadable", "gallery")
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
            if not _valid_extension_id(copied.get("id")):
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
            _add_diagnostic_warning(diagnostics, "manifest_invalid_path", "manifest")
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
            _add_diagnostic_warning(diagnostics, "manifest_missing", "manifest")
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
        _add_diagnostic_warning(diagnostics, "manifest_oversized", "manifest")
    except json.JSONDecodeError:
        _log.warning("Configured extension manifest is not valid JSON")
        manifest_status["status"] = "malformed"
        _add_diagnostic_warning(diagnostics, "manifest_malformed", "manifest")
    except RecursionError:
        # A <=64KB but deeply-nested manifest makes json.loads exceed the
        # interpreter recursion limit. Without this, the RecursionError escapes
        # into the app-shell route and every page load 503s. Fail safe.
        _log.warning("Configured extension manifest is too deeply nested")
        manifest_status["status"] = "too_deeply_nested"
        _add_diagnostic_warning(diagnostics, "manifest_too_deeply_nested", "manifest")
    except (OSError, UnicodeDecodeError):
        _log.warning("Configured extension manifest could not be read")
        manifest_status["status"] = "unreadable"
        _add_diagnostic_warning(diagnostics, "manifest_unreadable", "manifest")
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
        if not _valid_extension_id(raw_id):
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
        settings_schema = _sanitize_settings_schema(entry)
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
                "storage_owned": _manifest_entry_storage_owned(entry),
                "settings_schema": settings_schema,
                "status": (
                    "manifest_disabled"
                    if not manifest_enabled
                    else ("user_disabled" if user_disabled else "enabled")
                ),
            }
        )
    if invalid_seen:
        _add_diagnostic_warning(diagnostics, "manifest_extension_id_invalid", "manifest:extensions")
    if duplicate_seen:
        _add_diagnostic_warning(diagnostics, "manifest_extension_id_duplicate", "manifest:extensions")
    stale_ids = sorted((disabled_ids | (consent_ids or set())) - known_ids)
    if stale_ids:
        _add_diagnostic_warning(diagnostics, "extension_state_unknown_ids", _EXTENSION_STATE_WARNING_SOURCE)
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
        if not _valid_extension_id(raw_id):
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
                "storage_owned": _manifest_entry_storage_owned(entry),
                "settings_schema": _sanitize_settings_schema(entry),
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


def get_extension_config() -> Dict[str, Any]:
    """Return public extension config without exposing filesystem paths."""
    root = _extension_root()
    if root is None:
        return {"enabled": False, "script_urls": [], "stylesheet_urls": []}
    state = _load_extension_state()
    disabled_ids = set(state.get("disabled_extensions") or [])
    manifest, manifest_status = _load_manifest_with_status(root)
    manifest_scripts, manifest_stylesheets, _, _ = _read_manifest_urls_with_diagnostics(
        root,
        disabled_ids=disabled_ids,
        manifest=manifest,
        manifest_status=manifest_status,
    )
    config = {
        "enabled": True,
        "script_urls": _read_url_list(
            _EXTENSION_SCRIPT_URLS_ENV, manifest_scripts or None
        ),
        "stylesheet_urls": _read_url_list(
            _EXTENSION_STYLESHEET_URLS_ENV, manifest_stylesheets or None
        ),
    }
    runtime_entries = _extension_runtime_entries(manifest, disabled_ids) if manifest is not None else []
    if runtime_entries:
        config["extensions"] = runtime_entries
    return config



def get_extension_status() -> Dict[str, Any]:
    """Return sanitized extension diagnostics for administrators."""
    diagnostics = _new_diagnostics()
    root, dir_configured, dir_valid = _extension_root_status()
    state = _load_extension_state(diagnostics)
    disabled_ids = set(state.get("disabled_extensions") or [])
    manifest_configured = bool(os.getenv(_EXTENSION_MANIFEST_ENV, "").strip())
    manifest_status: Dict[str, Any] = {
        "configured": manifest_configured,
        "loaded": False,
        "status": "extension_disabled" if manifest_configured else "not_configured",
        "entry_count": 0,
        "script_count": 0,
        "stylesheet_count": 0,
        "sidecar_count": 0,
    }
    # Only warn about an unavailable directory when the admin explicitly set
    # HERMES_WEBUI_EXTENSION_DIR to a path that is missing/not-a-dir. The
    # WebUI-managed default simply not existing yet (pre-first-install) is the
    # normal opt-in state, not a misconfiguration worth surfacing.
    env_dir_set = bool(os.getenv(_EXTENSION_DIR_ENV, "").strip())
    if env_dir_set and dir_configured and not dir_valid:
        _add_diagnostic_warning(diagnostics, "extension_dir_unavailable", "extension_dir")

    if root is None:
        return {
            "enabled": False,
            "extension_dir_configured": dir_configured,
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

    manifest, manifest_status = _load_manifest_with_status(root, diagnostics)
    consent_ids = set((state.get("sidecar_proxy_consents") or {}).keys())
    extension_state = _manifest_extension_state(
        manifest,
        disabled_ids,
        diagnostics,
        consent_ids=consent_ids,
    ) if manifest is not None else {
        "extensions": [],
        "known_ids": set(),
        "manifest_disabled_ids": set(),
    }
    manifest_scripts, manifest_stylesheets, sidecars, manifest_status = _read_manifest_urls_with_diagnostics(
        root,
        diagnostics,
        disabled_ids=disabled_ids,
        manifest=manifest,
        manifest_status=manifest_status,
        state=state,
    )
    extensions = extension_state["extensions"]
    known_ids = extension_state["known_ids"]
    user_disabled_count = len(disabled_ids & known_ids)
    script_urls = _read_url_list(
        _EXTENSION_SCRIPT_URLS_ENV,
        manifest_scripts or None,
        diagnostics=diagnostics,
    )
    stylesheet_urls = _read_url_list(
        _EXTENSION_STYLESHEET_URLS_ENV,
        manifest_stylesheets or None,
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
            "user_disabled": user_disabled_count,
        },
        "manifest": public_manifest_status,
        "extensions": extensions,
        "gallery_installed": _load_install_manifest().get("installed", {}),
        "warnings": diagnostics["warnings"],
    }


def set_extension_user_enabled(extension_id: object, enabled: object) -> Dict[str, Any]:
    """Set the UI-managed enabled override for an installed manifest extension."""
    if not _valid_extension_id(extension_id):
        raise ExtensionToggleError("Invalid extension id", status=400)
    ext_id = str(extension_id).strip()
    if not isinstance(enabled, bool):
        raise ExtensionToggleError("enabled must be a boolean", status=400)
    root = _extension_root()
    if root is None:
        raise ExtensionToggleError("Extensions are not configured", status=404)
    with _EXTENSION_STATE_LOCK:
        diagnostics = _new_diagnostics()
        state = _load_extension_state(diagnostics)
        disabled_ids = set(state.get("disabled_extensions") or [])
        manifest, manifest_status = _load_manifest_with_status(root, diagnostics)
        if manifest is None or not manifest_status.get("loaded", False):
            raise ExtensionToggleError("Extension manifest is not loaded", status=409)
        consent_map = state.get("sidecar_proxy_consents") or {}
        extension_state = _manifest_extension_state(
            manifest,
            disabled_ids,
            diagnostics,
            consent_ids=set(consent_map.keys()),
        )
        known_ids: Set[str] = extension_state["known_ids"]
        manifest_disabled_ids: Set[str] = extension_state["manifest_disabled_ids"]
        if ext_id not in known_ids:
            raise ExtensionToggleError("Extension not found", status=404)
        if ext_id in manifest_disabled_ids:
            raise ExtensionToggleError("Extension is disabled by its manifest", status=409)
        if enabled:
            disabled_ids.discard(ext_id)
        else:
            disabled_ids.add(ext_id)
        _write_extension_state(
            {
                "disabled_extensions": sorted(disabled_ids),
                "sidecar_proxy_consents": {
                    consent_ext_id: origin
                    for consent_ext_id, origin in consent_map.items()
                    if consent_ext_id in known_ids
                },
            }
        )
    # Return a fresh status snapshot after the atomic write is visible. Keeping
    # the readback outside the lock avoids doing the full manifest/status parse
    # while blocking other toggles; a concurrent toggle may be reflected too,
    # which is fine because the UI re-renders from the current effective state.
    return get_extension_status()
