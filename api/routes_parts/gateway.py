"""Gateway status and lifecycle route domain."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    from api.agent_ops import build_agent_health_payload
    from api.helpers import _sanitize_error, bad, j
    from api.routes import (
        _MESSAGING_RAW_SOURCES,
        _MESSAGING_SESSION_METADATA_CACHE,
        _MESSAGING_SESSION_METADATA_LOCK,
        logger,
    )

def _normalize_messaging_source(raw_source) -> str:
    return str(raw_source or "").strip().lower()


def _is_known_messaging_source(raw_source) -> bool:
    return _normalize_messaging_source(raw_source) in _MESSAGING_RAW_SOURCES


def _safe_first(*values):
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _gateway_session_metadata_path():
    try:
        from api.profiles import get_active_hermes_home
        hermes_home = Path(get_active_hermes_home()).expanduser().resolve()
    except Exception:
        hermes_home = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser().resolve()
    return hermes_home / "sessions" / "sessions.json"


def _load_gateway_session_identity_map() -> dict[str, dict]:
    path = _gateway_session_metadata_path()
    if not path.exists():
        return {}

    try:
        st = path.stat()
        cache = _MESSAGING_SESSION_METADATA_CACHE
        with _MESSAGING_SESSION_METADATA_LOCK:
            if cache["path"] == str(path) and cache["mtime"] == st.st_mtime:
                return cache["identity"].copy()
    except Exception:
        return {}

    try:
        raw_sessions = json.loads(path.read_text(encoding="utf-8"))
    except Exception as _json_err:
        logger.debug("Failed to parse gateway sessions metadata from %s: %s", path, _json_err)
        return {}

    mapping: dict[str, dict] = {}
    if isinstance(raw_sessions, dict):
        for _entry in raw_sessions.values():
            if not isinstance(_entry, dict):
                continue
            session_id = _safe_first(_entry.get("session_id"))
            if not session_id:
                continue
            origin = _entry.get("origin") if isinstance(_entry.get("origin"), dict) else {}
            platform = _safe_first(origin.get("platform"), _entry.get("platform"))
            mapping[session_id] = {
                "session_key": _safe_first(_entry.get("session_key"), _entry.get("key")),
                "chat_id": _safe_first(origin.get("chat_id"), _entry.get("chat_id")),
                "thread_id": _safe_first(origin.get("thread_id"), _entry.get("thread_id")),
                "chat_type": _safe_first(origin.get("chat_type"), _entry.get("chat_type")),
                "user_id": _safe_first(origin.get("user_id"), _entry.get("user_id")),
                "platform": platform,
                "raw_source": platform,
            }

    with _MESSAGING_SESSION_METADATA_LOCK:
        _MESSAGING_SESSION_METADATA_CACHE["path"] = str(path)
        _MESSAGING_SESSION_METADATA_CACHE["mtime"] = st.st_mtime
        _MESSAGING_SESSION_METADATA_CACHE["identity"] = mapping
    return mapping.copy()


def _gateway_status_payload() -> dict:
    import datetime

    identity_map = _load_gateway_session_identity_map()
    sessions_path = _gateway_session_metadata_path()

    # Detect whether the gateway process is alive, independent of connected
    # messaging platforms. An empty identity_map means zero connected
    # platforms, not necessarily a stopped gateway.
    health = build_agent_health_payload()
    alive = health.get("alive")
    details = health.get("details") if isinstance(health.get("details"), dict) else {}
    health_reason = details.get("reason")
    health_state = details.get("state")
    health_gateway_state = details.get("gateway_state")
    if alive is True:
        running = True
        configured = True
    elif alive is False:
        running = False
        configured = True
    else:
        gateway_running_metadata = (
            health_reason == "gateway_stale_running_state"
            or health_gateway_state == "running"
        )
        configured = True if gateway_running_metadata else bool(identity_map)
        running = bool(identity_map)

    platforms_set: set[str] = set()
    for meta in identity_map.values():
        raw = meta.get("raw_source") or meta.get("platform") or ""
        norm = _normalize_messaging_source(raw)
        if norm:
            platforms_set.add(norm)
    platform_labels = {
        "telegram": "Telegram",
        "discord": "Discord",
        "slack": "Slack",
        "email": "Email",
        "web": "Web",
        "api": "API",
    }
    platforms = sorted(
        [{"name": p, "label": platform_labels.get(p, p.title())} for p in platforms_set],
        key=lambda x: x["label"],
    )
    last_active = ""
    if running and sessions_path.exists():
        try:
            mtime = sessions_path.stat().st_mtime
            last_active = datetime.datetime.fromtimestamp(mtime).isoformat()
        except Exception:
            pass
    return {
        "running": running,
        "configured": configured,
        "platforms": platforms,
        "last_active": last_active,
        "session_count": len(identity_map),
        "health": {
            "state": health_state,
            "reason": health_reason,
            "gateway_state": health_gateway_state,
        },
    }


_GATEWAY_LIFECYCLE_TIMEOUT_SECONDS = 60

# Server-side single-flight guard for gateway lifecycle actions. The client
# disables its button while a request is in flight, but a scripted authed
# client could still fire overlapping start/stop/restart calls, spawning
# concurrent `hermes gateway` subprocesses. Serialize them here (mirrors the
# self-update _apply_lock pattern): a non-blocking acquire returns 409 on
# contention rather than launching a second overlapping subprocess.
_GATEWAY_ACTION_LOCK = threading.Lock()


def _run_gateway_lifecycle_command(action: str) -> subprocess.CompletedProcess:
    if action not in {"start", "stop", "restart"}:
        raise ValueError("unsupported gateway action")

    from api import config as api_config
    from api.profiles import get_active_profile_name

    agent_dir = getattr(api_config, "_AGENT_DIR", None)
    if not agent_dir:
        raise FileNotFoundError("Hermes agent checkout not found")
    agent_dir = Path(agent_dir).expanduser().resolve()
    main_py = agent_dir / "hermes_cli" / "main.py"
    if not main_py.exists():
        raise FileNotFoundError("Hermes agent CLI entrypoint not found")

    cmd = [str(getattr(api_config, "PYTHON_EXE", sys.executable)), str(main_py)]
    profile_name = ""
    try:
        profile_name = str(get_active_profile_name() or "").strip()
    except Exception as exc:
        logger.debug("Could not resolve active profile for gateway lifecycle: %s", exc)
    if profile_name and profile_name != "default":
        cmd.extend(["--profile", profile_name])
    cmd.extend(["gateway", action])

    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("BROWSER", "echo")
    return subprocess.run(
        cmd,
        cwd=str(agent_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=_GATEWAY_LIFECYCLE_TIMEOUT_SECONDS,
    )


def _handle_gateway_lifecycle(handler, action: str, body: dict):
    del body  # Reserved for future per-gateway naming without changing the route contract.
    # Reject overlapping lifecycle actions instead of spawning concurrent
    # `hermes gateway` subprocesses (a non-blocking acquire — the action holds
    # the lock for at most _GATEWAY_LIFECYCLE_TIMEOUT_SECONDS).
    if action not in {"start", "stop", "restart"}:
        return bad(handler, "unsupported gateway action", 400)
    if not _GATEWAY_ACTION_LOCK.acquire(blocking=False):
        return j(
            handler,
            {
                "ok": False,
                "error": "Another gateway action is already in progress; try again shortly.",
                "action": action,
            },
            status=409,
        )
    try:
        result = _run_gateway_lifecycle_command(action)
    except ValueError as exc:
        return bad(handler, str(exc), 400)
    except FileNotFoundError as exc:
        return j(handler, {"ok": False, "error": _sanitize_error(exc), "action": action}, status=500)
    except subprocess.TimeoutExpired as exc:
        logger.warning(
            "Gateway %s command timed out after %ss; stdout=%r stderr=%r",
            action,
            _GATEWAY_LIFECYCLE_TIMEOUT_SECONDS,
            exc.stdout,
            exc.stderr,
        )
        return j(
            handler,
            {
                "ok": False,
                "error": f"Gateway {action} timed out after {_GATEWAY_LIFECYCLE_TIMEOUT_SECONDS} seconds",
                "action": action,
            },
            status=504,
        )
    except Exception as exc:
        logger.exception("Gateway %s command failed before completion", action)
        return j(handler, {"ok": False, "error": _sanitize_error(exc), "action": action}, status=500)
    finally:
        _GATEWAY_ACTION_LOCK.release()

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if result.returncode != 0:
        logger.warning(
            "Gateway %s command failed with exit code %s; stdout=%r stderr=%r",
            action,
            result.returncode,
            stdout,
            stderr,
        )
        return j(
            handler,
            {
                "ok": False,
                "error": f"Gateway {action} failed with exit code {result.returncode}",
                "action": action,
                "returncode": result.returncode,
            },
            status=500,
        )

    return j(
        handler,
        {
            "ok": True,
            "action": action,
            # Do NOT return captured stdout/stderr — the `hermes gateway` CLI
            # prints service/PID/status details the browser shouldn't receive
            # (mirrors the failure path, which already suppresses them). The
            # frontend localizes its own success copy; the refreshed status
            # payload carries the user-facing state.
            "message": f"Gateway {action} completed.",
            "status": _gateway_status_payload(),
        },
    )

__routes_exports__ = ('_normalize_messaging_source', '_is_known_messaging_source', '_safe_first', '_gateway_session_metadata_path', '_load_gateway_session_identity_map', '_gateway_status_payload', '_GATEWAY_LIFECYCLE_TIMEOUT_SECONDS', '_GATEWAY_ACTION_LOCK', '_run_gateway_lifecycle_command', '_handle_gateway_lifecycle')
