"""Application-shell rendering and process-level HTTP controls."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import signal
import threading
import time
from urllib.parse import quote

from api import config
from api.agent_ops import restart_active_profile_gateway
from api.config import MAX_UPLOAD_BYTES
from api.helpers import j, t
from api.updates import WEBUI_VERSION


# Preserve the established operational log stream while ownership moves.
logger = logging.getLogger("api.routes")

_SHELL_ERROR_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Hermes is restarting</title>
</head>
<body style="margin:0;padding:2rem;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#111827;color:#e5e7eb;">
  <main style="max-width:40rem;margin:10vh auto;line-height:1.5;">
    <h1 style="font-size:1.5rem;margin:0 0 0.75rem;">Hermes is restarting…</h1>
    <p style="margin:0;color:#cbd5e1;">The WebUI shell could not load cleanly. Refresh in a moment if this page does not update automatically.</p>
  </main>
</body>
</html>"""
_SHUTDOWN_LOG_VALUE_RE = re.compile(r"[\x00-\x1f\x7f]+")
_INDEX_SHELL_CACHE: dict = {}
_INDEX_SHELL_CACHE_LOCK = threading.Lock()


def serve_unavailable(handler, exc: Exception) -> bool:
    """Return HTML for shell-route failures so ``/`` never renders JSON."""
    logger.warning("Failed to serve WebUI shell route: %s", exc)
    t(
        handler,
        _SHELL_ERROR_HTML,
        status=503,
        content_type="text/html; charset=utf-8",
    )
    return True


def shutdown_log_value(value, *, default: str = "unknown", max_len: int = 160) -> str:
    """Return a bounded single-line value safe for shutdown diagnostics."""
    if value is None:
        return default
    try:
        text = str(value)
    except Exception:
        return default
    text = _SHUTDOWN_LOG_VALUE_RE.sub("?", text).strip()
    if not text:
        return default
    if len(text) > max_len:
        text = f"{text[:max_len]}…"
    return text


def handle_shutdown(handler) -> bool:
    """Acknowledge a shutdown request and interrupt the WebUI process."""
    headers = getattr(handler, "headers", {})
    user_agent = (
        headers.get("User-Agent", "no-ua") if hasattr(headers, "get") else "no-ua"
    )
    remote = "unknown"
    if getattr(handler, "client_address", None):
        remote = getattr(handler, "client_address", ("unknown",))[0]
    logger.info(
        "[shutdown-request] remote=%s method=%s path=%s ua=%s",
        shutdown_log_value(remote),
        shutdown_log_value(getattr(handler, "command", None)),
        shutdown_log_value(getattr(handler, "path", None), max_len=240),
        shutdown_log_value(user_agent, default="no-ua", max_len=240),
    )
    j(handler, {"status": "shutting_down"})

    def do_shutdown():
        time.sleep(0.3)
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=do_shutdown, daemon=True).start()
    return True


def handle_health_restart(handler) -> bool:
    """Restart the active profile's Hermes messaging gateway."""
    outcome = restart_active_profile_gateway()
    status = outcome.get("status")
    if status == "completed":
        return j(
            handler,
            {"ok": True, "message": "Gateway service restarted successfully"},
        )
    if status == "in_progress":
        return j(
            handler,
            {"ok": True, "message": "Gateway service restart initiated (in progress)"},
        )
    if status == "busy":
        return j(
            handler,
            {
                "ok": False,
                "error": outcome.get(
                    "message",
                    "Restart already in progress. Please wait a moment and try again.",
                ),
            },
            status=429,
        )
    return j(
        handler,
        {
            "ok": False,
            "error": outcome.get("message", "Internal error running restart"),
        },
        status=500,
    )


def serve_manifest(handler) -> bool:
    """Serve the PWA manifest with its required content type."""
    manifest_path = (config.get_static_root() / "manifest.json").resolve()
    if manifest_path.exists():
        data = manifest_path.read_bytes()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/manifest+json; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
        return True
    return j(handler, {"error": "not found"}, status=404)


def saved_prompts_path() -> Path:
    try:
        from api.profiles import get_active_hermes_home

        home = Path(get_active_hermes_home()).expanduser()
    except Exception:
        home = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
    return home / "webui" / "saved_prompts.json"


def load_saved_prompts() -> list:
    path = saved_prompts_path()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_saved_prompts(prompts: list) -> None:
    path = saved_prompts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prompts, ensure_ascii=False, indent=2), encoding="utf-8")


def render_index_shell_base() -> str:
    """Render and cache the process-constant part of the application shell."""
    index_path = config.get_index_html_path()
    stat = index_path.stat()
    signature = (index_path, stat.st_size, stat.st_mtime_ns)
    with _INDEX_SHELL_CACHE_LOCK:
        cached = _INDEX_SHELL_CACHE.get("base")
        if cached and cached[0] == signature:
            return cached[1]
    version_token = quote(WEBUI_VERSION, safe="")
    base = (
        index_path.read_text(encoding="utf-8")
        .replace("__WEBUI_VERSION__", version_token)
        .replace("__MAX_UPLOAD_BYTES__", str(MAX_UPLOAD_BYTES))
    )
    with _INDEX_SHELL_CACHE_LOCK:
        _INDEX_SHELL_CACHE["base"] = (signature, base)
    return base


__all__ = (
    "handle_health_restart",
    "handle_shutdown",
    "load_saved_prompts",
    "render_index_shell_base",
    "save_saved_prompts",
    "saved_prompts_path",
    "serve_manifest",
    "serve_unavailable",
    "shutdown_log_value",
)
