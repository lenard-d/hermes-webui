"""HTTP observability owners for logs, insights, and runtime health.

The route composition root delegates to this module through a small handler
interface. Runtime and persistence state are consumed through their package
interfaces instead of through the legacy ``api.routes`` namespace.
"""

from __future__ import annotations

from contextlib import closing
import logging
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import parse_qs

from api.config import SERVER_START_TIME, SESSIONS
from api.helpers import bad, j, sanitize_error
from api.insights import build_insights
from api.runs import (
    runtime_last_run_finished_at,
    runtime_transport_count,
    runtime_transport_items,
    runtime_worker_items,
)
from api.sessions import SESSION_DIR, active_state_db_path, all_sessions, load_projects


logger = logging.getLogger(__name__)

_LOG_FILE_WHITELIST = {
    "agent": "agent.log",
    "errors": "errors.log",
    "gateway": "gateway.log",
}
_LOG_TAIL_VALUES = {100, 200, 500, 1000}
_LOG_DEFAULT_TAIL = 200
_LOG_MAX_BYTES = 4 * 1024 * 1024


def normalize_logs_tail(raw_tail) -> int:
    try:
        tail = int(str(raw_tail or "").strip())
    except (TypeError, ValueError):
        return _LOG_DEFAULT_TAIL
    return tail if tail in _LOG_TAIL_VALUES else _LOG_DEFAULT_TAIL


def handle_logs(handler, parsed) -> bool:
    """Return a bounded tail window for an active-profile Hermes log file."""
    query = parse_qs(parsed.query)
    file_key = (query.get("file", ["agent"])[0] or "agent").strip().lower()
    filename = _LOG_FILE_WHITELIST.get(file_key)
    if not filename:
        return bad(handler, "Unknown log file", status=400)

    tail = normalize_logs_tail(query.get("tail", [None])[0])
    try:
        from api.profiles import get_active_hermes_home

        hermes_home = Path(get_active_hermes_home()).expanduser()
    except Exception:
        hermes_home = Path(
            os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")
        ).expanduser()

    log_dir = hermes_home / "logs"
    log_path = log_dir / filename
    try:
        if log_path.resolve(strict=False).parent != log_dir.resolve(strict=False):
            return bad(handler, "Invalid log file", status=400)
        if not log_path.exists() or not log_path.is_file():
            return j(
                handler,
                {
                    "file": file_key,
                    "tail": tail,
                    "lines": [],
                    "truncated": False,
                    "total_bytes": 0,
                    "mtime": None,
                    "hint": f"Log file for {file_key} not found yet.",
                },
            )
        stat = log_path.stat()
        total_bytes = int(stat.st_size)
        read_bytes = min(total_bytes, _LOG_MAX_BYTES)
        with log_path.open("rb") as handle:
            if total_bytes > read_bytes:
                handle.seek(total_bytes - read_bytes)
            raw = handle.read(read_bytes)
        lines = raw.decode("utf-8", errors="replace").splitlines()[-tail:]
        return j(
            handler,
            {
                "file": file_key,
                "tail": tail,
                "lines": lines,
                "truncated": total_bytes > read_bytes,
                "total_bytes": total_bytes,
                "mtime": stat.st_mtime,
                "hint": "",
            },
        )
    except Exception as exc:
        logger.exception("Failed to read whitelisted log file %s", file_key)
        return bad(handler, sanitize_error(exc), status=500)


def handle_insights(handler, parsed) -> bool:
    """Return usage analytics from local WebUI and Hermes session data."""
    payload = build_insights(
        parsed.query,
        session_dir=SESSION_DIR,
        state_db_path=active_state_db_path,
    )
    return j(handler, payload)


def accept_loop_health(handler) -> dict:
    server = getattr(handler, "server", None)
    return {
        "requests_total": int(getattr(server, "accept_loop_requests_total", 0) or 0),
        "last_request_at": round(
            float(getattr(server, "accept_loop_last_request_at", 0.0) or 0.0), 3
        ),
    }


def streams_lock_health(timeout_seconds: float = 0.5) -> dict:
    started_at = time.time()
    active_streams = runtime_transport_count(timeout=timeout_seconds)
    elapsed_ms = round((time.time() - started_at) * 1000, 1)
    if active_streams is None:
        return {
            "status": "blocked",
            "timeout_seconds": timeout_seconds,
            "ms": elapsed_ms,
        }
    return {
        "status": "ok",
        "active_streams": active_streams,
        "ms": elapsed_ms,
    }


def stream_runtime_diagnostics() -> dict:
    """Return non-sensitive counts for active SSE transports."""
    streams = []
    total_subscribers = 0
    total_offline_buffered_events = 0
    for stream_id, stream in runtime_transport_items():
        snapshot = {}
        diagnostic_snapshot = getattr(stream, "diagnostic_snapshot", None)
        if callable(diagnostic_snapshot):
            try:
                raw_snapshot = diagnostic_snapshot()
                if isinstance(raw_snapshot, dict):
                    snapshot = raw_snapshot
            except Exception:
                snapshot = {}
        subscriber_count = int(snapshot.get("subscriber_count") or 0)
        offline_buffered_events = int(snapshot.get("offline_buffered_events") or 0)
        total_subscribers += subscriber_count
        total_offline_buffered_events += offline_buffered_events
        streams.append(
            {
                "stream_id": str(stream_id),
                "subscriber_count": subscriber_count,
                "offline_buffered_events": offline_buffered_events,
            }
        )
    streams.sort(key=lambda item: item["stream_id"])
    return {
        "active_streams": len(streams),
        "total_subscribers": total_subscribers,
        "total_offline_buffered_events": total_offline_buffered_events,
        "streams": streams,
    }


def run_lifecycle_health() -> dict:
    """Return active worker state without exposing session or workspace identity."""
    now = time.time()
    runs = []
    for _stream_id, raw in runtime_worker_items():
        item = dict(raw or {})
        item.pop("session_id", None)
        item.pop("stream_id", None)
        item.pop("workspace", None)
        try:
            age = max(0.0, now - float(item.get("started_at")))
        except Exception:
            age = 0.0
        item["age_seconds"] = round(age, 1)
        runs.append(item)
    last_finished = runtime_last_run_finished_at()
    runs.sort(key=lambda item: float(item.get("started_at") or 0.0))
    payload = {
        "active_runs": len(runs),
        "runs": runs,
        "last_run_finished_at": last_finished,
    }
    if runs:
        payload["oldest_run_age_seconds"] = runs[0].get("age_seconds", 0.0)
    elif last_finished:
        payload["idle_seconds_since_last_run"] = round(
            max(0.0, now - float(last_finished)), 1
        )
    return payload


def deep_health_checks(stream_check: dict | None = None) -> tuple[dict, bool]:
    """Probe state paths used by the UI shell without mutating them."""
    checks: dict[str, dict] = {}
    checks["streams_lock"] = (
        stream_check if stream_check is not None else streams_lock_health()
    )
    checks["stream_runtime"] = {"status": "ok", **stream_runtime_diagnostics()}
    if checks["streams_lock"].get("status") != "ok":
        return checks, False

    started_at = time.time()
    try:
        sessions = all_sessions()
        checks["sessions"] = {
            "status": "ok",
            "count": len(sessions),
            "ms": round((time.time() - started_at) * 1000, 1),
        }
    except Exception as exc:
        checks["sessions"] = {
            "status": "error",
            "error": type(exc).__name__,
            "ms": round((time.time() - started_at) * 1000, 1),
        }

    started_at = time.time()
    try:
        projects = load_projects(_migrate=False)
        checks["projects"] = {
            "status": "ok",
            "count": len(projects),
            "ms": round((time.time() - started_at) * 1000, 1),
        }
    except Exception as exc:
        checks["projects"] = {
            "status": "error",
            "error": type(exc).__name__,
            "ms": round((time.time() - started_at) * 1000, 1),
        }

    started_at = time.time()
    try:
        db_path = active_state_db_path()
        if not db_path.exists():
            checks["state_db"] = {
                "status": "missing",
                "ms": round((time.time() - started_at) * 1000, 1),
            }
        else:
            with closing(sqlite3.connect(str(db_path))) as connection:
                connection.execute("PRAGMA schema_version").fetchone()
            checks["state_db"] = {
                "status": "ok",
                "ms": round((time.time() - started_at) * 1000, 1),
            }
    except Exception as exc:
        checks["state_db"] = {
            "status": "error",
            "error": type(exc).__name__,
            "ms": round((time.time() - started_at) * 1000, 1),
        }

    healthy = all(check.get("status") in {"ok", "missing"} for check in checks.values())
    return checks, healthy


def handle_health(handler, parsed):
    deep = parse_qs(parsed.query or "").get("deep", [""])[0].lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    stream_check = streams_lock_health()
    run_check = run_lifecycle_health()
    payload = {
        "status": "ok" if stream_check.get("status") == "ok" else "degraded",
        "sessions": len(SESSIONS),
        "active_streams": int(stream_check.get("active_streams") or 0),
        "active_runs": int(run_check.get("active_runs") or 0),
        "runs": run_check.get("runs", []),
        "last_run_finished_at": run_check.get("last_run_finished_at"),
        "server_started_at": SERVER_START_TIME,
        "uptime_seconds": round(time.time() - SERVER_START_TIME, 1),
        "accept_loop": accept_loop_health(handler),
    }
    if "oldest_run_age_seconds" in run_check:
        payload["oldest_run_age_seconds"] = run_check["oldest_run_age_seconds"]
    if "idle_seconds_since_last_run" in run_check:
        payload["idle_seconds_since_last_run"] = run_check[
            "idle_seconds_since_last_run"
        ]
    if deep:
        if stream_check.get("status") != "ok":
            payload["checks"] = {"streams_lock": stream_check}
            return j(handler, payload, status=503)
        checks, healthy = deep_health_checks(stream_check=stream_check)
        payload["checks"] = checks
        if not healthy:
            payload["status"] = "degraded"
            return j(handler, payload, status=503)
    if payload["status"] != "ok":
        return j(handler, payload, status=503)
    return j(handler, payload)


__all__ = (
    "accept_loop_health",
    "deep_health_checks",
    "handle_health",
    "handle_insights",
    "handle_logs",
    "normalize_logs_tail",
    "run_lifecycle_health",
    "stream_runtime_diagnostics",
    "streams_lock_health",
)
