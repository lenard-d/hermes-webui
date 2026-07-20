"""JSON-lines child process for profile-isolated provider usage probes."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

try:
    from .codex_pool_usage import fetch_codex_account_usage_from_pool
except ImportError:  # direct script execution avoids importing api.providers
    from codex_pool_usage import fetch_codex_account_usage_from_pool


def _iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat().replace("+00:00", "Z")
    text = str(value).strip()
    return text or None


def _snapshot_payload(snapshot: Any) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    windows = [
        {
            "label": str(getattr(window, "label", "") or ""),
            "used_percent": getattr(window, "used_percent", None),
            "reset_at": _iso(getattr(window, "reset_at", None)),
            "detail": getattr(window, "detail", None),
        }
        for window in (getattr(snapshot, "windows", ()) or ())
    ]
    payload = {
        "provider": str(getattr(snapshot, "provider", "") or ""),
        "source": str(getattr(snapshot, "source", "") or ""),
        "title": str(getattr(snapshot, "title", "") or ""),
        "plan": getattr(snapshot, "plan", None),
        "windows": windows,
        "details": list(getattr(snapshot, "details", ()) or ()),
        "available": bool(getattr(snapshot, "available", bool(windows))),
        "unavailable_reason": getattr(snapshot, "unavailable_reason", None),
        "fetched_at": _iso(getattr(snapshot, "fetched_at", None)),
    }
    pool = getattr(snapshot, "pool", None)
    if isinstance(pool, dict):
        payload["pool"] = pool
    return payload


def fetch_snapshot_payload(
    provider: Any,
    api_key: Any,
    *,
    env_var: Any = None,
) -> dict[str, Any] | None:
    """Fetch one snapshot while restoring any temporary provider env value."""
    previous = os.environ.get(env_var) if env_var else None
    had_previous = bool(env_var and env_var in os.environ)
    if env_var and api_key:
        os.environ[env_var] = api_key
    try:
        from agent.account_usage import fetch_account_usage

        try:
            snapshot = fetch_account_usage(provider, api_key=api_key)
        except Exception:
            snapshot = None
        if str(provider or "").strip().lower() == "openai-codex":
            pool_snapshot = fetch_codex_account_usage_from_pool()
            if isinstance(getattr(pool_snapshot, "pool", None), dict):
                snapshot = pool_snapshot
        return _snapshot_payload(snapshot)
    finally:
        if env_var and api_key:
            if had_previous:
                os.environ[env_var] = previous
            else:
                os.environ.pop(env_var, None)


def run_worker() -> None:
    for raw_line in sys.stdin:
        try:
            request = json.loads(raw_line)
            payload = fetch_snapshot_payload(
                request.get("provider"),
                request.get("api_key") or None,
                env_var=request.get("env_var") or None,
            )
        except Exception:
            payload = None
        print(json.dumps(payload), flush=True)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--worker"]:
        run_worker()
        return 0
    if not args:
        return 2
    provider = args[0]
    api_key = args[1] if len(args) > 1 else None
    print(json.dumps(fetch_snapshot_payload(provider, api_key or None)), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
