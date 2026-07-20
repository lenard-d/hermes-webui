"""Registry discovery and verified extension package lifecycle.

This module owns the remote-registry adapter and the local install transaction:
allowlisted HTTPS, bounded downloads, digest and archive validation, extraction
rollback, atomic install records, and uninstall cleanup.  The public interface
remains :mod:`api.extensions`.
"""

import hashlib
import http.client
import io
import json
import os
import re
import socket
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, build_opener

from api.extensions_parts.facade import extensions_api


_GALLERY_INSTALL_STATE_FILENAME = "extension-install-manifest.json"
_MAX_INSTALL_MANIFEST_BYTES = 128 * 1024
_MAX_GALLERY_INSTALLED_IDS = 256
_MAX_ZIP_DOWNLOAD_BYTES = 32 * 1024 * 1024
_REGISTRY_URL = "https://hermes-webui.github.io/hermes-webui-extensions/registry.json"
_REGISTRY_ALLOWED_DOWNLOAD_HOSTS = frozenset({"hermes-webui.github.io"})
_REGISTRY_CACHE: dict = {}
_REGISTRY_LOCK = threading.Lock()
_REGISTRY_TTL_SECONDS = 300


class _AllowlistRedirectHandler(HTTPRedirectHandler):
    """Reject redirects to hosts not in the download allowlist."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        ext = extensions_api()
        parsed = urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname not in ext._REGISTRY_ALLOWED_DOWNLOAD_HOSTS:
            raise ext.ExtensionInstallError("Download redirected to disallowed host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _connect_ipv4_first(
    address,
    timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
    source_address=None,
):
    """Connect to *address*, preferring IPv4 before the IPv6 fallback."""
    host, port = address
    if timeout is socket._GLOBAL_DEFAULT_TIMEOUT:
        timeout = socket.getdefaulttimeout()
    last_error: OSError | None = None
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            infos = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)
        except socket.gaierror:
            continue
        for family_value, socktype, proto, _canon, sockaddr in infos:
            sock = None
            try:
                sock = socket.socket(family_value, socktype, proto)
                if timeout is not None:
                    sock.settimeout(timeout)
                if source_address is not None:
                    sock.bind(source_address)
                sock.connect(sockaddr)
                return sock
            except OSError as exc:
                last_error = exc
                if sock is not None:
                    sock.close()
    if last_error is not None:
        raise last_error
    raise OSError(f"Could not connect to {host}:{port}")


class _IPv4FirstHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection whose resolver tries IPv4 addresses first."""

    _create_connection = staticmethod(_connect_ipv4_first)


class _IPv4FirstHTTPSHandler(HTTPSHandler):
    """Build IPv4-first HTTPS connections for gallery traffic."""

    def https_open(self, req):
        return self.do_open(_IPv4FirstHTTPSConnection, req)


def _build_gallery_opener():
    """Return an opener with IPv4-first HTTPS and redirect containment."""
    return build_opener(_IPv4FirstHTTPSHandler, _AllowlistRedirectHandler)


def _safe_download(url: str, max_bytes: int, timeout: int = 30) -> bytes:
    """Download through the gallery adapter and close the response reliably."""
    opener = extensions_api()._build_gallery_opener()
    response = opener.open(url, timeout=timeout)
    try:
        return response.read(max_bytes + 1)
    finally:
        response.close()


def _install_manifest_file() -> Path:
    ext = extensions_api()
    return ext._extension_state_dir() / ext._GALLERY_INSTALL_STATE_FILENAME


def _empty_install_manifest() -> Dict[str, Any]:
    return {"version": 1, "installed": {}}


def _load_install_manifest() -> Dict[str, Any]:
    """Load the gallery install record, failing closed on any invalid shape."""
    ext = extensions_api()
    manifest_file = ext._install_manifest_file()
    try:
        if not manifest_file.exists() or not manifest_file.is_file():
            return ext._empty_install_manifest()
        with manifest_file.open("rb") as handle:
            raw = handle.read(ext._MAX_INSTALL_MANIFEST_BYTES + 1)
        if len(raw) > ext._MAX_INSTALL_MANIFEST_BYTES:
            return ext._empty_install_manifest()
        parsed = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return ext._empty_install_manifest()
    if not isinstance(parsed, dict) or not isinstance(parsed.get("installed"), dict):
        return ext._empty_install_manifest()
    installed: Dict[str, Any] = {}
    for extension_id, entry in parsed["installed"].items():
        if not ext._valid_extension_id(extension_id) or not isinstance(entry, dict):
            continue
        files = entry.get("files", [])
        if not isinstance(files, list):
            continue
        installed[extension_id] = {
            "version": str(entry.get("version", "unknown")),
            "files": [value for value in files if isinstance(value, str)],
            "installed_at": str(entry.get("installed_at", "")),
        }
        if len(installed) >= ext._MAX_GALLERY_INSTALLED_IDS:
            break
    return {"version": 1, "installed": installed}


def _write_install_manifest(manifest: Dict[str, Any]) -> None:
    """Persist the gallery install record with a same-directory replace."""
    ext = extensions_api()
    target = ext._install_manifest_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    data = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        with tmp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _remove_extracted_files(paths: List[Path], extension_dir: Path) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        if extension_dir.exists() and not any(extension_dir.iterdir()):
            extension_dir.rmdir()
    except OSError:
        pass


def install_extension(id: object, download_url: object, sha256: object) -> Dict[str, Any]:
    """Download, verify, and transactionally extract a gallery extension."""
    ext = extensions_api()
    if not ext._valid_extension_id(id):
        raise ext.ExtensionInstallError("Invalid extension id")
    extension_id = str(id).strip()
    if not isinstance(download_url, str) or not download_url.startswith("https://"):
        raise ext.ExtensionInstallError("Invalid download URL")
    parsed_url = urlsplit(download_url)
    if parsed_url.hostname not in ext._REGISTRY_ALLOWED_DOWNLOAD_HOSTS:
        raise ext.ExtensionInstallError("Invalid download URL")
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ext.ExtensionInstallError("Invalid sha256")
    root = ext._writable_extension_root()
    if root is None:
        raise ext.ExtensionInstallError("Extensions not configured", 404)
    try:
        raw_data = ext._safe_download(download_url, ext._MAX_ZIP_DOWNLOAD_BYTES)
    except ext.ExtensionInstallError:
        raise
    except Exception as exc:
        raise ext.ExtensionInstallError("Download failed", 502) from exc
    if len(raw_data) > ext._MAX_ZIP_DOWNLOAD_BYTES:
        raise ext.ExtensionInstallError("Download too large")
    if hashlib.sha256(raw_data).hexdigest() != sha256:
        raise ext.ExtensionInstallError("SHA-256 mismatch")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw_data))
    except zipfile.BadZipFile as exc:
        raise ext.ExtensionInstallError("Invalid zip archive") from exc

    extension_dir = root / extension_id
    member_names = archive.namelist()
    total_uncompressed = sum(
        info.file_size for info in archive.infolist() if not info.is_dir()
    )
    if total_uncompressed > ext._MAX_ZIP_DOWNLOAD_BYTES * 10:
        raise ext.ExtensionInstallError("Archive uncompressed size exceeds limit")
    file_members = [name for name in member_names if name and not name.endswith("/")]
    if len(file_members) > 1024:
        raise ext.ExtensionInstallError("Archive contains too many files")

    prefix_candidate = extension_id + "/"
    strip_prefix = (
        prefix_candidate
        if file_members and all(name.startswith(prefix_candidate) for name in file_members)
        else ""
    )

    def stripped(name: str) -> str:
        if strip_prefix and name.startswith(strip_prefix):
            return name[len(strip_prefix):]
        return name

    root_resolved = root.resolve()
    extension_dir_resolved = extension_dir.resolve()
    for member_name in file_members:
        decoded = ext._fully_unquote_path(stripped(member_name))
        if not decoded or not ext._is_safe_relative_path(decoded):
            raise ext.ExtensionInstallError("Unsafe archive member")
        resolved = (extension_dir / decoded).resolve()
        try:
            resolved.relative_to(root_resolved)
            resolved.relative_to(extension_dir_resolved)
        except ValueError as exc:
            raise ext.ExtensionInstallError("Zip-slip detected") from exc

    version = "unknown"
    for version_file in ("extension.json", "manifest.json"):
        candidate_name = strip_prefix + version_file if strip_prefix else version_file
        if candidate_name not in member_names:
            continue
        try:
            metadata = json.loads(archive.read(candidate_name).decode("utf-8"))
            if isinstance(metadata, dict) and isinstance(metadata.get("version"), str):
                version = metadata["version"]
                break
        except Exception:
            pass

    with ext._EXTENSION_STATE_LOCK:
        extension_dir.mkdir(parents=True, exist_ok=True)
        if extension_dir.is_symlink():
            raise ext.ExtensionInstallError("Extension directory is a symlink", 400)
        extracted: List[Path] = []
        try:
            for member_name in file_members:
                decoded = ext._fully_unquote_path(stripped(member_name))
                destination = (extension_dir / decoded).resolve()
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(member_name))
                extracted.append(destination)
        except Exception as exc:
            _remove_extracted_files(extracted, extension_dir)
            raise ext.ExtensionInstallError("Extraction failed", 500) from exc
        try:
            manifest = ext._load_install_manifest()
            relative_files = [
                path.relative_to(extension_dir_resolved).as_posix()
                for path in extracted
            ]
            manifest["installed"][extension_id] = {
                "version": version,
                "files": relative_files,
                "installed_at": datetime.now(timezone.utc).isoformat(),
            }
            encoded = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            if len(encoded) > ext._MAX_INSTALL_MANIFEST_BYTES:
                raise ext.ExtensionInstallError("Install manifest would exceed size limit")
            ext._write_install_manifest(manifest)
        except ext.ExtensionInstallError:
            _remove_extracted_files(extracted, extension_dir)
            raise
        except Exception as exc:
            _remove_extracted_files(extracted, extension_dir)
            raise ext.ExtensionInstallError("Failed to record install", 500) from exc
    return {"installed": True, "id": extension_id, "version": version}


def uninstall_extension(id: object) -> Dict[str, Any]:
    """Remove only files recorded as gallery-installed and then its record."""
    ext = extensions_api()
    if not ext._valid_extension_id(id):
        raise ext.ExtensionInstallError("Invalid extension id")
    extension_id = str(id).strip()
    root = ext._extension_root()
    if root is None:
        raise ext.ExtensionInstallError("Extensions not configured", 404)
    with ext._EXTENSION_STATE_LOCK:
        manifest = ext._load_install_manifest()
        entry = manifest["installed"].get(extension_id)
        if entry is None:
            raise ext.ExtensionInstallError("Extension not installed", 404)
        extension_dir = root / extension_id
        for relative_path in entry.get("files", []):
            if not ext._is_safe_relative_path(relative_path):
                continue
            target = (extension_dir / relative_path).resolve()
            try:
                target.relative_to(extension_dir.resolve())
            except ValueError:
                continue
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        if extension_dir.exists():
            for directory in sorted(
                (path for path in extension_dir.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts),
                reverse=True,
            ):
                try:
                    if not any(directory.iterdir()):
                        directory.rmdir()
                except OSError:
                    pass
            try:
                if not any(extension_dir.iterdir()):
                    extension_dir.rmdir()
            except OSError:
                pass
        del manifest["installed"][extension_id]
        ext._write_install_manifest(manifest)
    return {"uninstalled": True, "id": extension_id}


def get_extension_registry() -> Dict[str, Any]:
    """Fetch the extension registry through the bounded five-minute cache."""
    ext = extensions_api()
    with ext._REGISTRY_LOCK:
        now = time.monotonic()
        cached = ext._REGISTRY_CACHE.get("data")
        cached_at = ext._REGISTRY_CACHE.get("fetched_at", 0.0)
        if cached is not None and (now - cached_at) < ext._REGISTRY_TTL_SECONDS:
            return {"entries": cached}
        try:
            opener = ext._build_gallery_opener()
            response = opener.open(ext._REGISTRY_URL, timeout=10)
            raw = response.read(2 * 1024 * 1024)
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, list):
                entries = data
            elif isinstance(data, dict):
                entries = data.get("extensions") or data.get("entries") or []
            else:
                entries = []
            if not isinstance(entries, list):
                entries = []
            ext._REGISTRY_CACHE["data"] = entries
            ext._REGISTRY_CACHE["fetched_at"] = now
            return {"entries": entries}
        except Exception:
            return {"entries": [], "error": "registry_unavailable"}


__all__ = [
    "_GALLERY_INSTALL_STATE_FILENAME",
    "_MAX_INSTALL_MANIFEST_BYTES",
    "_MAX_GALLERY_INSTALLED_IDS",
    "_MAX_ZIP_DOWNLOAD_BYTES",
    "_REGISTRY_URL",
    "_REGISTRY_ALLOWED_DOWNLOAD_HOSTS",
    "_REGISTRY_CACHE",
    "_REGISTRY_LOCK",
    "_REGISTRY_TTL_SECONDS",
    "_AllowlistRedirectHandler",
    "_connect_ipv4_first",
    "_IPv4FirstHTTPSConnection",
    "_IPv4FirstHTTPSHandler",
    "_build_gallery_opener",
    "_safe_download",
    "_install_manifest_file",
    "_empty_install_manifest",
    "_load_install_manifest",
    "_write_install_manifest",
    "install_extension",
    "uninstall_extension",
    "get_extension_registry",
]
