"""Loopback sidecar declaration, consent, and proxy-target resolution.

The module owns the full sidecar security lifecycle: canonical loopback-only
origins, hostile path rejection, effective extension state, exact-origin user
consent, and final upstream target resolution.  Unknown or ambiguous state
fails closed before the route adapter can contact anything.
"""

from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit

from . import configuration
from .errors import ExtensionSidecarProxyError
from .security import _MAX_URL_LIST, _fully_unquote_path

_SIDECAR_WARNING_SOURCE = "manifest:sidecars"
_DEFAULT_SIDECAR_HEALTH_PATH = "/health"
_LOOPBACK_SIDECAR_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _normalize_loopback_sidecar_origin(value: object) -> Optional[str]:
    """Return a canonical browser-addressable loopback origin when safe."""
    if not isinstance(value, str):
        return None
    origin = value.strip()
    if not origin or any(
        char in origin for char in ("\x00", "\r", "\n", '"', "'", "<", ">", "\\")
    ):
        return None
    parsed = urlsplit(origin)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        return None
    host = (parsed.hostname or "").lower()
    if host not in _LOOPBACK_SIDECAR_HOSTS:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    display_host = f"[{host}]" if ":" in host else host
    return f"{parsed.scheme}://{display_host}{':' + str(port) if port is not None else ''}"


def _normalize_sidecar_health_path(value: object) -> Optional[str]:
    """Return a query-free, traversal-free sidecar health path when safe."""
    if not isinstance(value, str):
        return None
    path = value.strip()
    if not path or not path.startswith("/") or path.startswith("//"):
        return None
    if any(char in path for char in ("\x00", "\r", "\n", '"', "'", "<", ">", "\\")):
        return None
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return None
    decoded_path = _fully_unquote_path(parsed.path)
    if any(
        char in decoded_path
        for char in ("\x00", "\r", "\n", '"', "'", "<", ">", "\\", "?", "#")
    ):
        return None
    if any(char.isspace() for char in decoded_path):
        return None
    if not decoded_path.startswith("/") or decoded_path.startswith("//"):
        return None
    segments = decoded_path.split("/")[1:]
    if not segments or any(not segment or segment in (".", "..") for segment in segments):
        return None
    return decoded_path


def _is_valid_sidecar_proxy_path(decoded_path: str) -> bool:
    if any(char in decoded_path for char in ("?", "#")):
        return False
    if any(char.isspace() for char in decoded_path):
        return False
    if not decoded_path.startswith("/") or decoded_path.startswith("//"):
        return False
    segments = decoded_path.split("/")[1:]
    return bool(segments) and all(
        segment and segment not in (".", "..") for segment in segments
    )


def _sidecar_from_manifest_entry(
    entry: Dict[str, object],
    diagnostics: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, str]]:
    """Sanitize one manifest sidecar declaration without echoing rejected data."""
    raw = entry.get("sidecar")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        configuration._add_diagnostic_warning(
            diagnostics,
            "sidecar_invalid",
            _SIDECAR_WARNING_SOURCE,
        )
        return None
    if raw.get("type") != "loopback":
        configuration._add_diagnostic_warning(
            diagnostics,
            "sidecar_type_unsupported",
            _SIDECAR_WARNING_SOURCE,
        )
        return None
    origin = _normalize_loopback_sidecar_origin(raw.get("origin"))
    if origin is None:
        configuration._add_diagnostic_warning(
            diagnostics,
            "sidecar_origin_rejected",
            _SIDECAR_WARNING_SOURCE,
        )
        return None
    if "health_path" in raw:
        health_path = _normalize_sidecar_health_path(raw.get("health_path"))
        if health_path is None:
            configuration._add_diagnostic_warning(
                diagnostics,
                "sidecar_health_path_rejected",
                _SIDECAR_WARNING_SOURCE,
            )
            return None
    else:
        health_path = _DEFAULT_SIDECAR_HEALTH_PATH
    sidecar_id = configuration._manifest_entry_text(entry, "id")
    name = configuration._manifest_entry_text(entry, "name")
    return {
        "id": sidecar_id,
        "name": name,
        "type": "loopback",
        "origin": origin,
        "health_path": health_path,
        "health_url": f"{origin}{health_path}",
    }


def _extension_sidecar_proxy_path(extension_id: str) -> str:
    return f"/api/extensions/{extension_id}/sidecar/"


def _sidecar_proxy_public_status(
    extension_id: str,
    origin: str,
    approved_origin: Optional[str],
    *,
    available: bool,
) -> Dict[str, Any]:
    consented = bool(available and approved_origin == origin)
    return {
        "available": available,
        "consented": consented,
        "consent_required": bool(available and not consented),
        "path": _extension_sidecar_proxy_path(extension_id),
        "origin_changed": bool(available and approved_origin and approved_origin != origin),
    }


def _extension_sidecar_records(
    manifest: object,
    disabled_ids: Optional[Set[str]] = None,
    state: Optional[Dict[str, Any]] = None,
    diagnostics: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Project unique, effectively enabled sidecars and their consent state."""
    disabled_ids = disabled_ids or set()
    consent_map = {}
    if isinstance(state, dict) and isinstance(state.get("sidecar_proxy_consents"), dict):
        consent_map = state["sidecar_proxy_consents"]
    id_counts: Dict[str, int] = {}
    by_id: Dict[str, Dict[str, Any]] = {}
    for _source, _index, entry in configuration._manifest_extension_entries(manifest):
        raw_id = configuration._manifest_entry_text(entry, "id")
        if not configuration._valid_extension_id(raw_id):
            continue
        extension_id = raw_id.strip()
        id_counts[extension_id] = id_counts.get(extension_id, 0) + 1
        if extension_id in by_id:
            continue
        manifest_enabled = entry.get("enabled", True) is not False
        user_disabled = extension_id in disabled_ids
        effective_enabled = manifest_enabled and not user_disabled
        sidecar = (
            _sidecar_from_manifest_entry(entry, diagnostics)
            if effective_enabled
            else None
        )
        approved_origin = (
            consent_map.get(extension_id)
            if isinstance(consent_map.get(extension_id), str)
            else None
        )
        by_id[extension_id] = {
            "id": extension_id,
            "name": configuration._manifest_entry_text(entry, "name"),
            "manifest_enabled": manifest_enabled,
            "user_disabled": user_disabled,
            "effective_enabled": effective_enabled,
            "sidecar": sidecar,
            "approved_origin": approved_origin,
        }

    records: List[Dict[str, Any]] = []
    for extension_id, item in by_id.items():
        sidecar = item.get("sidecar")
        if sidecar is None:
            continue
        available = bool(
            item["effective_enabled"] and id_counts.get(extension_id, 0) == 1
        )
        proxy = _sidecar_proxy_public_status(
            extension_id,
            sidecar["origin"],
            item.get("approved_origin"),
            available=available,
        )
        item["duplicate_id"] = id_counts.get(extension_id, 0) > 1
        item["proxy"] = proxy
        if len(records) < _MAX_URL_LIST:
            records.append({**sidecar, "proxy": proxy})
        else:
            configuration._add_diagnostic_warning(
                diagnostics,
                "sidecar_list_truncated",
                _SIDECAR_WARNING_SOURCE,
            )
            break
    return records, by_id


def _normalize_sidecar_proxy_path(value: object) -> Optional[str]:
    if value is None or str(value) == "":
        return "/"
    raw = str(value)
    if raw.startswith("/"):
        return None
    candidate = f"/{raw}"
    if not _is_valid_sidecar_proxy_path(_fully_unquote_path(candidate)):
        return None
    return candidate


def set_extension_sidecar_proxy_consent(
    extension_id: object,
    approved: object,
) -> Dict[str, Any]:
    """Persist or revoke consent bound to the currently declared exact origin."""
    if not configuration._valid_extension_id(extension_id):
        raise ExtensionSidecarProxyError("Invalid extension id", status=400)
    ext_id = str(extension_id).strip()
    if not isinstance(approved, bool):
        raise ExtensionSidecarProxyError("approved must be a boolean", status=400)
    root = configuration._extension_root()
    if root is None:
        raise ExtensionSidecarProxyError("Extensions are not configured", status=404)
    with configuration._EXTENSION_STATE_LOCK:
        diagnostics = configuration._new_diagnostics()
        state = configuration._load_extension_state(diagnostics)
        disabled_ids = set(state.get("disabled_extensions") or [])
        consent_map = dict(state.get("sidecar_proxy_consents") or {})
        manifest, manifest_status = configuration._load_manifest_with_status(root, diagnostics)
        if manifest is None or not manifest_status.get("loaded", False):
            raise ExtensionSidecarProxyError(
                "Extension manifest is not loaded",
                status=409,
            )
        extension_state = configuration._manifest_extension_state(
            manifest,
            disabled_ids,
            diagnostics,
            consent_ids=set(consent_map.keys()),
        )
        known_ids: Set[str] = extension_state["known_ids"]
        if ext_id not in known_ids:
            raise ExtensionSidecarProxyError("Extension not found", status=404)
        _sidecars, by_id = _extension_sidecar_records(
            manifest,
            disabled_ids=disabled_ids,
            state=state,
            diagnostics=diagnostics,
        )
        item = by_id.get(ext_id) or {}
        sidecar = item.get("sidecar")
        proxy = item.get("proxy") or {}
        if approved:
            if sidecar is None or proxy.get("available") is not True:
                raise ExtensionSidecarProxyError(
                    "Extension sidecar proxy is unavailable",
                    status=409,
                )
            consent_map[ext_id] = sidecar["origin"]
        else:
            consent_map.pop(ext_id, None)
        configuration._write_extension_state(
            {
                "disabled_extensions": sorted(disabled_ids),
                "sidecar_proxy_consents": {
                    consent_ext_id: origin
                    for consent_ext_id, origin in consent_map.items()
                    if consent_ext_id in known_ids
                },
            }
        )
    return configuration.get_extension_status()


def resolve_extension_sidecar_proxy_target(
    extension_id: object,
    proxy_path: object,
    query: str = "",
) -> Dict[str, Any]:
    """Resolve one consented declaration to the exact upstream URL."""
    if not configuration._valid_extension_id(extension_id):
        raise ExtensionSidecarProxyError("Invalid extension id", status=400)
    normalized_path = _normalize_sidecar_proxy_path(proxy_path)
    if normalized_path is None:
        raise ExtensionSidecarProxyError("Invalid sidecar proxy path", status=400)
    ext_id = str(extension_id).strip()
    root = configuration._extension_root()
    if root is None:
        raise ExtensionSidecarProxyError("Extensions are not configured", status=404)
    diagnostics = configuration._new_diagnostics()
    state = configuration._load_extension_state(diagnostics)
    disabled_ids = set(state.get("disabled_extensions") or [])
    manifest, manifest_status = configuration._load_manifest_with_status(root, diagnostics)
    if manifest is None or not manifest_status.get("loaded", False):
        raise ExtensionSidecarProxyError(
            "Extension manifest is not loaded",
            status=409,
        )
    consent_ids = set((state.get("sidecar_proxy_consents") or {}).keys())
    extension_state = configuration._manifest_extension_state(
        manifest,
        disabled_ids,
        diagnostics,
        consent_ids=consent_ids,
    )
    if ext_id not in extension_state["known_ids"]:
        raise ExtensionSidecarProxyError("Extension not found", status=404)
    _sidecars, by_id = _extension_sidecar_records(
        manifest,
        disabled_ids=disabled_ids,
        state=state,
        diagnostics=diagnostics,
    )
    item = by_id.get(ext_id) or {}
    sidecar = item.get("sidecar")
    proxy = item.get("proxy") or {}
    if sidecar is None or proxy.get("available") is not True:
        raise ExtensionSidecarProxyError(
            "Extension sidecar proxy is unavailable",
            status=409,
        )
    if proxy.get("consented") is not True:
        raise ExtensionSidecarProxyError(
            "Extension sidecar proxy consent required",
            status=403,
        )
    upstream_url = f"{sidecar['origin']}{normalized_path}"
    if query:
        upstream_url = f"{upstream_url}?{query}"
    return {
        "extension_id": ext_id,
        "origin": sidecar["origin"],
        "proxy_path": proxy["path"],
        "upstream_url": upstream_url,
    }


__all__ = [
    "_normalize_loopback_sidecar_origin",
    "_normalize_sidecar_health_path",
    "_is_valid_sidecar_proxy_path",
    "_sidecar_from_manifest_entry",
    "_extension_sidecar_proxy_path",
    "_sidecar_proxy_public_status",
    "_extension_sidecar_records",
    "_normalize_sidecar_proxy_path",
    "set_extension_sidecar_proxy_consent",
    "resolve_extension_sidecar_proxy_target",
]
