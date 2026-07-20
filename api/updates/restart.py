"""Restart admission, gateway handoff, and process replacement ownership."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from api.agent_ops import (
    get_active_profile_gateway_running_pid as _default_gateway_pid,
)
from api.agent_ops import restart_active_profile_gateway as _default_restart_gateway
from api.config import STREAMS, STREAMS_LOCK
from api.profiles import get_active_profile_name as _default_active_profile_name

from .cleanup import purge_python_bytecode
from . import transaction_state

logger = logging.getLogger(__name__)
_AGENT_GATEWAY_RESTART_RETRY_DELAY_S = 1.0


def restart_blocker_snapshot() -> dict:
    """Return active chat work that should block a self-restart."""
    with STREAMS_LOCK:
        stream_ids = [str(key) for key in STREAMS]
    run_ids: list[str] = []
    try:
        from api import config as _config

        active_runs = getattr(_config, "ACTIVE_RUNS", {})
        active_runs_lock = getattr(_config, "ACTIVE_RUNS_LOCK", None)
        if active_runs_lock is not None:
            with active_runs_lock:
                run_ids = [str(key) for key in active_runs]
        else:
            run_ids = [str(key) for key in active_runs]
    except Exception:
        run_ids = []
    return {
        "active_streams": len(stream_ids),
        "active_runs": len(run_ids),
        "blocking_stream_ids": stream_ids[:10],
        "blocking_run_ids": run_ids[:10],
        "restart_blocked": bool(stream_ids or run_ids),
    }


def active_stream_count() -> int:
    """Compatibility diagnostic for callers that only need stream count."""
    return int(restart_blocker_snapshot().get("active_streams") or 0)


def restart_blocked_response(target: str, snapshot: dict | int) -> dict:
    """Describe why an update cannot safely replace the running process."""
    if isinstance(snapshot, int):
        snapshot = {
            "active_streams": snapshot,
            "active_runs": 0,
            "blocking_stream_ids": [],
            "blocking_run_ids": [],
            "restart_blocked": bool(snapshot),
        }
    active_streams = int(snapshot.get("active_streams") or 0)
    active_runs = int(snapshot.get("active_runs") or 0)
    parts = []
    if active_streams:
        parts.append(
            f"{active_streams} active chat stream"
            f'{"s" if active_streams != 1 else ""}'
        )
    if active_runs:
        suffix = "s" if active_runs != 1 else ""
        parts.append(f"{active_runs} active agent run{suffix}")
    detail = " and ".join(parts) or "active chat work"
    return {
        "ok": False,
        "message": (
            f"Cannot update {target} while {detail} is running. "
            "Wait for the response to finish, then retry the update."
        ),
        "target": target,
        "restart_blocked": True,
        "active_streams": active_streams,
        "active_runs": active_runs,
        "blocking_stream_ids": snapshot.get("blocking_stream_ids") or [],
        "blocking_run_ids": snapshot.get("blocking_run_ids") or [],
    }


def wait_until_restart_safe(
    poll_seconds: float = 2.0,
    max_wait_seconds: float = 300.0,
) -> dict:
    """Wait boundedly for active work before process replacement."""
    snapshot = restart_blocker_snapshot()
    deadline = time.monotonic() + max(0.0, max_wait_seconds)
    while snapshot.get("restart_blocked"):
        if time.monotonic() >= deadline:
            logger.warning(
                "restart-safety wait exceeded %.0fs with work still in flight (%s); "
                "proceeding with re-exec anyway",
                max_wait_seconds,
                snapshot,
            )
            snapshot = dict(snapshot)
            snapshot["wait_timed_out"] = True
            return snapshot
        time.sleep(max(0.1, poll_seconds))
        snapshot = restart_blocker_snapshot()
    return snapshot


def schedule_restart(delay: float = 2.0) -> None:
    """Replace this process after pending update transactions have completed."""
    import os
    import sys

    def _do():
        import time as _time

        _time.sleep(delay)
        with transaction_state.apply_lock():
            wait_until_restart_safe()
            agent_dir = transaction_state._AGENT_DIR
            if agent_dir is not None:
                purge_python_bytecode(Path(agent_dir))
            purge_python_bytecode(Path(transaction_state.REPO_ROOT))
            try:
                if sys.platform == "win32":
                    import subprocess

                    args = (
                        sys.argv
                        if getattr(sys, "frozen", False)
                        else [sys.executable] + sys.argv
                    )
                    executable = sys.executable
                    if executable.lower().endswith("python.exe"):
                        pythonw = executable[:-4] + "w.exe"
                        if os.path.isfile(pythonw) and not getattr(
                            sys, "frozen", False
                        ):
                            args = [pythonw] + sys.argv
                    subprocess.Popen(
                        args,
                        cwd=os.getcwd(),
                        creationflags=(
                            subprocess.DETACHED_PROCESS
                            | subprocess.CREATE_NEW_PROCESS_GROUP
                            | subprocess.CREATE_NO_WINDOW
                        ),
                        close_fds=True,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    os._exit(0)
                if getattr(sys, "frozen", False):
                    os.execv(sys.executable, sys.argv)
                else:
                    os.execv(sys.executable, [sys.executable] + sys.argv)
            except Exception:
                os._exit(0)

    threading.Thread(target=_do, daemon=True).start()


def ensure_gateway_restart_for_agent_update() -> tuple[bool, dict]:
    """Restart the active-profile gateway and prove a successful handoff."""
    profile = str(_default_active_profile_name() or "default").strip() or "default"
    pid_before = _default_gateway_pid(profile=profile)
    first = _default_restart_gateway(profile=profile)
    first_status = str(first.get("status") or "")
    if first_status in {"completed", "in_progress"}:
        return True, first
    if first_status != "failed":
        return False, first

    time.sleep(_AGENT_GATEWAY_RESTART_RETRY_DELAY_S)
    retry = _default_restart_gateway(profile=profile)
    retry_status = str(retry.get("status") or "")
    if retry_status in {"completed", "in_progress"}:
        return True, {
            **retry,
            "retry_attempted": True,
            "initial_failure": first.get("message"),
        }
    if retry_status != "failed":
        return False, {
            **retry,
            "retry_attempted": True,
            "initial_failure": first.get("message"),
        }

    time.sleep(_AGENT_GATEWAY_RESTART_RETRY_DELAY_S)
    pid_after = _default_gateway_pid(profile=profile)
    if pid_before is not None and pid_after is not None and pid_after != pid_before:
        return True, {
            "status": "completed",
            "message": "Gateway service recovered after a transient restart failure",
            "retry_attempted": True,
            "process_replaced": True,
            "initial_failure": first.get("message"),
            "retry_failure": retry.get("message"),
        }

    first_message = str(first.get("message") or "Restart failed")
    retry_message = str(retry.get("message") or "retry did not complete")
    return False, {
        **retry,
        "message": (
            f"{first_message}; recovery retry did not complete: {retry_message}"
        ),
        "retry_attempted": True,
        "initial_failure": first.get("message"),
    }


def gateway_restart_failure_message(target: str, result: dict) -> str:
    """Describe the manual recovery step after an applied Agent update."""
    if result.get("message"):
        return (
            f'{target} updated, but gateway restart did not complete: '
            f'{result["message"]}. Run `hermes gateway restart` manually.'
        )
    return (
        f"{target} updated, but gateway restart did not complete. "
        "Run `hermes gateway restart` manually."
    )


__all__ = [
    "active_stream_count",
    "ensure_gateway_restart_for_agent_update",
    "gateway_restart_failure_message",
    "restart_blocked_response",
    "restart_blocker_snapshot",
    "schedule_restart",
    "wait_until_restart_safe",
]
