"""Asset containment, app-shell injection, and static delivery policy.

Every filesystem path and injected URL crosses this module's fail-closed seam.
It deliberately owns both validation and the action that consumes the validated
value, keeping the security decision and point of use together.
"""

import html
import json
from urllib.parse import unquote

from api.helpers import _security_headers, j

from .asset_urls import _is_safe_relative_path
from .roots import EXTENSION_ROUTE_PREFIX, extension_root

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
_TEXT_MIME_TYPES = {
    "text/css",
    "application/javascript",
    "text/html",
    "image/svg+xml",
    "text/plain",
}


def inject_extension_tags(index_html: str) -> str:
    """Inject only the sanitized runtime config and same-origin asset tags."""
    from .configuration import get_extension_config

    config = get_extension_config()
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
    j(handler, {"error": "not found"}, status=404)
    return True


def serve_extension_static(handler, parsed) -> bool:
    """Serve a contained extension asset or return one generic 404."""
    root = extension_root()
    if root is None:
        return _not_found(handler)

    rel = unquote(parsed.path[len(EXTENSION_ROUTE_PREFIX):])
    if not _is_safe_relative_path(rel):
        return _not_found(handler)
    static_file = (root / rel).resolve()
    try:
        static_file.relative_to(root)
    except ValueError:
        return _not_found(handler)
    if not static_file.exists() or not static_file.is_file():
        return _not_found(handler)

    content_type = _EXTENSION_MIME.get(
        static_file.suffix.lower().lstrip("."),
        "text/plain",
    )
    content_type_header = (
        f"{content_type}; charset=utf-8"
        if content_type in _TEXT_MIME_TYPES
        else content_type
    )
    try:
        raw = static_file.read_bytes()
    except OSError:
        return _not_found(handler)

    handler.send_response(200)
    handler.send_header("Content-Type", content_type_header)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(raw)))
    _security_headers(handler)
    handler.end_headers()
    handler.wfile.write(raw)
    return True


__all__ = [
    "inject_extension_tags",
    "_not_found",
    "serve_extension_static",
]
