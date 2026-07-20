"""Extension root and state-directory ownership."""

import os
from pathlib import Path
from typing import Optional, Tuple


EXTENSION_ROUTE_PREFIX = "/extensions/"
EXTENSION_DIR_ENV = "HERMES_WEBUI_EXTENSION_DIR"
EXTENSION_SCRIPT_URLS_ENV = "HERMES_WEBUI_EXTENSION_SCRIPT_URLS"
EXTENSION_STYLESHEET_URLS_ENV = "HERMES_WEBUI_EXTENSION_STYLESHEET_URLS"
EXTENSION_MANIFEST_ENV = "HERMES_WEBUI_EXTENSION_MANIFEST"


def extension_state_dir() -> Path:
    """Return the WebUI-owned state directory used by extension metadata."""
    try:
        from api.config import STATE_DIR

        return Path(STATE_DIR)
    except Exception:
        fallback = Path.home() / ".hermes" / "webui"
        return Path(os.getenv("HERMES_WEBUI_STATE_DIR", str(fallback))).expanduser()


def default_extension_root() -> Path:
    """Return the managed extension directory without creating it."""
    return extension_state_dir() / "extensions"


def extension_root() -> Optional[Path]:
    """Resolve the active readable root, failing closed on invalid paths."""
    raw = os.getenv(EXTENSION_DIR_ENV, "").strip()
    if raw:
        root = Path(raw).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            return None
        return root
    root = default_extension_root()
    try:
        if root.is_dir() and not root.is_symlink():
            return root.resolve()
    except OSError:
        return None
    return None


def writable_extension_root() -> Optional[Path]:
    """Resolve the write root, creating only the WebUI-managed default."""
    if os.getenv(EXTENSION_DIR_ENV, "").strip():
        return extension_root()
    root = default_extension_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            return None
        return root.resolve()
    except OSError:
        return None


def extension_root_status() -> Tuple[Optional[Path], bool, bool]:
    """Return ``(root, configured, valid)`` without exposing configured paths."""
    raw = os.getenv(EXTENSION_DIR_ENV, "").strip()
    if raw:
        root = Path(raw).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            return None, True, False
        return root, True, True
    root = extension_root()
    return root, True, root is not None


__all__ = [
    "EXTENSION_DIR_ENV",
    "EXTENSION_MANIFEST_ENV",
    "EXTENSION_ROUTE_PREFIX",
    "EXTENSION_SCRIPT_URLS_ENV",
    "EXTENSION_STYLESHEET_URLS_ENV",
    "default_extension_root",
    "extension_root",
    "extension_root_status",
    "extension_state_dir",
    "writable_extension_root",
]
