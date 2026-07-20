"""Cohesive HTTP route group used by the transport composition root."""

from __future__ import annotations

from api.http.context import RouteContext, UNHANDLED


def handle_get(handler, parsed, ctx: RouteContext):
    _dashboard_plugin_enabled = ctx["_dashboard_plugin_enabled"]

    if parsed.path.startswith("/plugins/"):
        from api.plugins import _get_plugin_base

        plugin_base = _get_plugin_base()
        rel = parsed.path[len("/plugins/") :]
        allowed = {"plugin.css"}
        if rel not in allowed:
            return False  # 404
        safe = (plugin_base / rel).resolve()
        try:
            safe.relative_to(plugin_base.resolve())
        except ValueError:
            return False  # path traversal — 404
        if safe.is_file():
            import os as _os

            data = safe.read_bytes()
            ext = _os.path.splitext(rel.lower())[1]
            ct = {
                ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".json": "application/json; charset=utf-8",
                ".png": "image/png",
                ".svg": "image/svg+xml",
            }.get(ext, "application/octet-stream")
            handler.send_response(200)
            handler.send_header("Content-Type", ct)
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
            return True

    # ── Plugin static assets ──
    if parsed.path.startswith("/dashboard-plugins/"):
        parts = parsed.path.split("/", 3)
        if len(parts) >= 3:
            plugin_name = parts[2]
            rel_path = parts[3] if len(parts) > 3 else ""
            # Server-side enable-gate: a plugin disabled in Settings must have its
            # entire URL surface shut off, not merely hidden in the UI.
            if not _dashboard_plugin_enabled(plugin_name):
                return False  # 404 — disabled plugins serve nothing
            from api.plugins import serve_plugin_static

            result = serve_plugin_static(plugin_name, rel_path)
            if result:
                data, content_type = result
                handler.send_response(200)
                handler.send_header("Content-Type", content_type)
                # Defense-in-depth: plugin-controlled assets are served from the
                # WebUI's own origin. Sandbox them (null origin) so a plugin's
                # .html/.svg can't run privileged same-origin script if navigated
                # to directly (the in-panel iframe sandbox doesn't cover direct
                # navigation). nosniff prevents content-type confusion.
                handler.send_header(
                    "Content-Security-Policy",
                    "sandbox allow-scripts allow-forms allow-popups",
                )
                handler.send_header("X-Content-Type-Options", "nosniff")
                handler.send_header("Content-Length", str(len(data)))
                handler.end_headers()
                handler.wfile.write(data)
                return True

    # ── Plugin pages (HTML shell) ──
    from api.plugins import PLUGIN_MANIFESTS, _PLUGIN_STATIC_ROOTS

    for name, manifest in PLUGIN_MANIFESTS.items():
        tab = manifest.get("tab", {})
        tab_path = tab.get("path", f"/{name}")
        if parsed.path == tab_path:
            # Server-side enable-gate (opt-in): a disabled plugin's page 404s.
            if not _dashboard_plugin_enabled(name):
                return False
            dashboard_dir = _PLUGIN_STATIC_ROOTS.get(name)
            if dashboard_dir:
                # 1) dashboard/dist/index.html (full SPA build)
                index_html = dashboard_dir / "dist" / "index.html"
                if index_html.is_file():
                    data = index_html.read_bytes()
                    handler.send_response(200)
                    handler.send_header("Content-Type", "text/html; charset=utf-8")
                    handler.send_header(
                        "Content-Security-Policy",
                        "sandbox allow-scripts allow-forms allow-popups",
                    )
                    handler.send_header("Content-Length", str(len(data)))
                    handler.end_headers()
                    handler.wfile.write(data)
                    return True
                # 2) static/index.html in plugin root (content page for IIFE loader)
                plugin_root = dashboard_dir.parent
                static_html = plugin_root / "static" / "index.html"
                if static_html.is_file():
                    data = static_html.read_bytes()
                    handler.send_response(200)
                    handler.send_header("Content-Type", "text/html; charset=utf-8")
                    handler.send_header(
                        "Content-Security-Policy",
                        "sandbox allow-scripts allow-forms allow-popups",
                    )
                    handler.send_header("Content-Length", str(len(data)))
                    handler.end_headers()
                    handler.wfile.write(data)
                    return True
                # 3) Fallback: generate shell that loads the IIFE bundle
                index_js = dashboard_dir / "dist" / "index.js"
                if index_js.is_file():
                    import html

                    label = html.escape(manifest.get("label") or name)
                    css = html.escape(manifest.get("css", ""))
                    name_escaped = html.escape(name)
                    css_tag = (
                        f'<link rel="stylesheet" href="/dashboard-plugins/{name_escaped}/{css}">'
                        if css
                        else ""
                    )
                    html_content = (
                        f"<!doctype html>\n"
                        f'<html lang="en">\n'
                        f"<head>\n"
                        f'  <meta charset="utf-8">\n'
                        f"  <title>{label}</title>\n"
                        f"  {css_tag}\n"
                        f"</head>\n"
                        f"<body>\n"
                        f'  <div id="pluginPageContainer"></div>\n'
                        f'  <script src="/dashboard-plugins/{name_escaped}/dist/index.js"></script>\n'
                        f"</body>\n"
                        f"</html>\n"
                    ).encode("utf-8")
                    handler.send_response(200)
                    handler.send_header("Content-Type", "text/html; charset=utf-8")
                    handler.send_header(
                        "Content-Security-Policy",
                        "sandbox allow-scripts allow-forms allow-popups",
                    )
                    handler.send_header("Content-Length", str(len(html_content)))
                    handler.end_headers()
                    handler.wfile.write(html_content)
                    return True
    return UNHANDLED
