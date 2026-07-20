"""Same-origin extension asset URL and relative-path policy."""

import logging
import os
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlsplit

from .diagnostics import add_diagnostic_warning


_log = logging.getLogger("api.extensions")
_MAX_URL_LIST = 32
_ALLOWED_ASSET_PREFIXES = ("/extensions/", "/static/")
_warned_urls: set = set()


def _fully_unquote_path(path: str) -> str:
    """Decode percent-encoding until stable, with a defensive iteration cap."""
    previous = path
    for _ in range(10):
        current = unquote(previous)
        if current == previous:
            return current
        previous = current
    return previous


def _is_safe_relative_path(rel: str) -> bool:
    """Accept only non-hidden relative paths with no traversal segments."""
    if not rel or "\x00" in rel or "\\" in rel:
        return False
    return all(
        segment and segment not in (".", "..") and not segment.startswith(".")
        for segment in rel.split("/")
    )


def _is_safe_asset_url(value: str) -> bool:
    """Allow only same-origin extension/static asset URLs."""
    if not value or any(
        char in value for char in ('\x00', '\r', '\n', '"', "'", "<", ">", "\\")
    ):
        return False
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        return False
    decoded_path = _fully_unquote_path(parsed.path)
    for prefix in _ALLOWED_ASSET_PREFIXES:
        if decoded_path.startswith(prefix):
            return _is_safe_relative_path(decoded_path[len(prefix) :])
    return False


def _warn_rejected_url(value: str, source: str) -> None:
    if value in _warned_urls:
        return
    _warned_urls.add(value)
    _log.warning(
        "Rejected extension URL %r from %s (not a same-origin "
        "/extensions/ or /static/ path, or contains unsafe chars)",
        value,
        source,
    )


def _append_safe_asset_url(
    urls: List[str],
    value: str,
    source: str,
    *,
    dedupe: bool = True,
    diagnostics: Optional[Dict[str, Any]] = None,
) -> bool:
    """Append one validated asset URL, preserving order and the global cap."""
    value = value.strip() if isinstance(value, str) else ""
    if not value:
        return True
    if not _is_safe_asset_url(value):
        _warn_rejected_url(value, source)
        add_diagnostic_warning(diagnostics, "asset_url_rejected", source)
        return True
    if dedupe and value in urls:
        return True
    if len(urls) >= _MAX_URL_LIST:
        if source not in _warned_urls:
            _warned_urls.add(source)
            _log.warning(
                "Extension URL list %s truncated at %d entries", source, _MAX_URL_LIST
            )
        add_diagnostic_warning(diagnostics, "asset_url_list_truncated", source)
        return False
    urls.append(value)
    return True


def _read_url_list(
    env_name: str,
    existing: Optional[List[str]] = None,
    *,
    diagnostics: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Read, validate, and cap a comma-separated environment URL list."""
    urls = list(existing or [])
    dedupe = existing is not None
    for item in os.getenv(env_name, "").split(","):
        if not _append_safe_asset_url(
            urls,
            item,
            env_name,
            dedupe=dedupe,
            diagnostics=diagnostics,
        ):
            break
    return urls


__all__ = [
    "_MAX_URL_LIST",
    "_append_safe_asset_url",
    "_fully_unquote_path",
    "_is_safe_asset_url",
    "_is_safe_relative_path",
    "_read_url_list",
    "_warn_rejected_url",
    "_warned_urls",
]
