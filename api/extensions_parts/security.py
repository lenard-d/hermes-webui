"""Asset containment, app-shell injection, and static delivery policy.

Every filesystem path and injected URL crosses this module's fail-closed seam.
It deliberately owns both validation and the action that consumes the validated
value, keeping the security decision and point of use together.
"""

import html
import json
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlsplit

from api.extensions_parts.facade import extensions_api


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
    for segment in rel.split("/"):
        if not segment or segment in (".", "..") or segment.startswith("."):
            return False
    return True


def _is_safe_asset_url(value: str) -> bool:
    """Allow only same-origin extension/static asset URLs."""
    ext = extensions_api()
    if not value or any(
        char in value for char in ('\x00', '\r', '\n', '"', "'", "<", ">", "\\")
    ):
        return False
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        return False
    decoded_path = ext._fully_unquote_path(parsed.path)
    for prefix in ext._ALLOWED_ASSET_PREFIXES:
        if decoded_path.startswith(prefix):
            return ext._is_safe_relative_path(decoded_path[len(prefix):])
    return False


def _warn_rejected_url(value: str, source: str) -> None:
    ext = extensions_api()
    if value in ext._warned_urls:
        return
    ext._warned_urls.add(value)
    ext._log.warning(
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
    ext = extensions_api()
    value = value.strip() if isinstance(value, str) else ""
    if not value:
        return True
    if not ext._is_safe_asset_url(value):
        ext._warn_rejected_url(value, source)
        ext._add_diagnostic_warning(diagnostics, "asset_url_rejected", source)
        return True
    if dedupe and value in urls:
        return True
    if len(urls) >= ext._MAX_URL_LIST:
        if source not in ext._warned_urls:
            ext._warned_urls.add(source)
            ext._log.warning(
                "Extension URL list %s truncated at %d entries",
                source,
                ext._MAX_URL_LIST,
            )
        ext._add_diagnostic_warning(diagnostics, "asset_url_list_truncated", source)
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
    ext = extensions_api()
    raw = ext.os.getenv(env_name, "")
    urls = list(existing or [])
    dedupe = existing is not None
    for item in raw.split(","):
        if not ext._append_safe_asset_url(
            urls,
            item,
            env_name,
            dedupe=dedupe,
            diagnostics=diagnostics,
        ):
            break
    return urls


def inject_extension_tags(index_html: str) -> str:
    """Inject only the sanitized runtime config and same-origin asset tags."""
    ext = extensions_api()
    config = ext.get_extension_config()
    if not config["enabled"]:
        return index_html

    result = index_html
    stylesheet_tags = [
        '<link rel="stylesheet" href="{}">'.format(html.escape(url, quote=True))
        for url in config["stylesheet_urls"]
    ]
    script_tags = [
        '<script src="{}" defer></script>'.format(html.escape(url, quote=True))
        for url in config["script_urls"]
    ]
    runtime_config = {"extensions": config.get("extensions", [])}
    runtime_json = json.dumps(
        runtime_config,
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("<", "\\u003c")
    runtime_tag = (
        "<script>window.__HERMES_EXTENSION_CONFIG__={};"
        "if(window.HermesExtensionSettings)window.HermesExtensionSettings.primeFromStatus(window.__HERMES_EXTENSION_CONFIG__);"
        "</script>"
    ).format(runtime_json)

    if stylesheet_tags:
        block = "\n".join(stylesheet_tags) + "\n"
        if "</head>" in result:
            result = result.replace("</head>", block + "</head>", 1)
        else:
            result = block + result
    if runtime_config["extensions"] or script_tags:
        block = runtime_tag + "\n"
        if script_tags:
            block += "\n".join(script_tags) + "\n"
        if "</body>" in result:
            result = result.replace("</body>", block + "</body>", 1)
        else:
            result = result + "\n" + block
    return result


def _not_found(handler) -> bool:
    ext = extensions_api()
    ext.j(handler, {"error": "not found"}, status=404)
    return True


def serve_extension_static(handler, parsed) -> bool:
    """Serve a contained extension asset or return one generic 404."""
    ext = extensions_api()
    root = ext._extension_root()
    if root is None:
        return ext._not_found(handler)

    rel = unquote(parsed.path[len(ext.EXTENSION_ROUTE_PREFIX):])
    if not ext._is_safe_relative_path(rel):
        return ext._not_found(handler)
    static_file = (root / rel).resolve()
    try:
        static_file.relative_to(root)
    except ValueError:
        return ext._not_found(handler)
    if not static_file.exists() or not static_file.is_file():
        return ext._not_found(handler)

    content_type = ext._EXTENSION_MIME.get(
        static_file.suffix.lower().lstrip("."),
        "text/plain",
    )
    content_type_header = (
        f"{content_type}; charset=utf-8"
        if content_type in ext._TEXT_MIME_TYPES
        else content_type
    )
    try:
        raw = static_file.read_bytes()
    except OSError:
        return ext._not_found(handler)

    handler.send_response(200)
    handler.send_header("Content-Type", content_type_header)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(raw)))
    ext._security_headers(handler)
    handler.end_headers()
    handler.wfile.write(raw)
    return True


__all__ = [
    "_fully_unquote_path",
    "_is_safe_relative_path",
    "_is_safe_asset_url",
    "_warn_rejected_url",
    "_append_safe_asset_url",
    "_read_url_list",
    "inject_extension_tags",
    "_not_found",
    "serve_extension_static",
]
