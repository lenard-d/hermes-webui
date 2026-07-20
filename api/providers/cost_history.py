"""OpenRouter cost-history collection and snapshot persistence.

The module owns upstream sanitization, atomic daily snapshots, cross-thread and
cross-process locking, and cumulative-to-daily delta calculation behind one
public provider-history interface.  It is installed into :mod:`api.providers`
to preserve the established import and monkeypatch seam.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows-only import fallback
    fcntl = None  # type: ignore[assignment]

from api.config import (
    PROVIDER_DISPLAY as _PROVIDER_DISPLAY,
    coerce_provider_cost_budget as _coerce_provider_cost_budget,
)
from api.providers.account_usage import (
    _OPENROUTER_KEY_URL,
    _PROVIDER_QUOTA_TIMEOUT_SECONDS,
    _quota_number,
    _sanitize_openrouter_quota,
)
from api.providers.credentials import _get_provider_api_key

logger = logging.getLogger(__name__)


def _get_hermes_home() -> Path:
    from api import profiles

    return profiles.get_active_hermes_home()

# ── OpenRouter cost-history snapshot helpers (#692) ──────────────────────────

_COST_SNAPSHOTS_DIR_NAME = "cost-snapshots"
_COST_SNAPSHOT_MAX_DAYS = 365  # hard cap to prevent unbounded growth
_COST_SNAPSHOT_LOCK = threading.Lock()


def _get_provider_cost_budget() -> float | None:
    """Return the user-configured monthly spend budget, or None if unset."""
    try:
        from api.config import load_settings
        raw = load_settings().get("provider_cost_budget")
        return _coerce_provider_cost_budget(raw)
    except Exception:
        return None


def _cost_snapshots_dir() -> Path:
    """Return the directory for cost-snapshot JSON files.

    Uses the Hermes home directory (profile-aware) so snapshots are
    isolated per profile, matching the existing STATE_DIR convention.
    """
    return _get_hermes_home() / _COST_SNAPSHOTS_DIR_NAME


@contextmanager
def _cost_snapshot_file_lock(provider: str):
    """Serialize cost snapshot read-modify-write across worker processes."""
    if fcntl is None:
        with nullcontext():
            yield
        return

    snap_dir = _cost_snapshots_dir()
    snap_dir.mkdir(parents=True, exist_ok=True)
    lock_path = snap_dir / f"{provider}.lock"
    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        yield


def _fetch_openrouter_key_usage(api_key: str) -> dict[str, Any] | None:
    """Fetch current usage/limit from the OpenRouter ``/auth/key`` endpoint.

    Returns a dict with ``usage``, ``limit``, ``label`` on success, or
    ``None`` on any failure.  Never raises; callers handle the None case.
    """
    req = urllib.request.Request(
        _OPENROUTER_KEY_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_PROVIDER_QUOTA_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
        payload = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
        sanitized = _sanitize_openrouter_quota(payload)
        label = None
        if isinstance(payload, dict):
            data = payload.get("data", payload)
            if isinstance(data, dict):
                label = str(data.get("label") or "").strip() or None
        return {
            "usage": sanitized.get("usage"),
            "limit": sanitized.get("limit"),
            "label": label,
        }
    except Exception:
        logger.debug("OpenRouter key usage fetch failed for cost-history", exc_info=True)
        return None


def _read_cost_snapshots(provider: str) -> list[dict[str, Any]]:
    """Read persisted daily snapshots for *provider* from disk.

    Returns a list of ``{date, used, limit}`` dicts sorted by date
    ascending.  Returns an empty list if the file does not exist or is
    corrupt.
    """
    path = _cost_snapshots_dir() / f"{provider}.json"
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    snapshots = data.get("snapshots")
    if not isinstance(snapshots, list):
        return []
    # Validate and sort
    valid = []
    for entry in snapshots:
        if not isinstance(entry, dict):
            continue
        date = str(entry.get("date") or "").strip()
        if not date:
            continue
        valid.append({
            "date": date,
            "used": _quota_number(entry.get("used")),
            "limit": _quota_number(entry.get("limit")),
        })
    valid.sort(key=lambda e: e["date"])
    return valid


def _write_cost_snapshots(provider: str, snapshots: list[dict[str, Any]]) -> None:
    """Persist daily snapshots for *provider* to disk atomically."""
    snap_dir = _cost_snapshots_dir()
    snap_dir.mkdir(parents=True, exist_ok=True)
    path = snap_dir / f"{provider}.json"
    payload = {"provider": provider, "snapshots": snapshots}
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    import tempfile as _tempfile
    _tmp_fd, _tmp_path = _tempfile.mkstemp(
        dir=str(snap_dir), prefix=f".{provider}_", suffix=".tmp"
    )
    try:
        with os.fdopen(_tmp_fd, "w", encoding="utf-8") as _f:
            _f.write(body)
            _f.flush()
            os.fsync(_f.fileno())
        os.replace(_tmp_path, path)
    except BaseException:
        try:
            os.unlink(_tmp_path)
        except OSError:
            pass
        raise


def _append_cost_snapshot(provider: str, usage: int | float | None, limit: int | float | None) -> list[dict[str, Any]]:
    """Append today's snapshot and return the updated list.

    If a snapshot for today already exists it is updated in-place so
    repeated calls within the same day are idempotent.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Serialize the read-modify-write cycle.  The atomic os.replace in
    # _write_cost_snapshots protects the file write itself, but without these
    # locks two concurrent requests can both read the same old snapshot list and
    # race to replace it with stale data.  The threading lock covers current
    # single-process deployments; the file lock covers future multi-worker
    # deployments that share one Hermes home/state directory.
    with _COST_SNAPSHOT_LOCK:
        with _cost_snapshot_file_lock(provider):
            snapshots = _read_cost_snapshots(provider)
            # Update or append today's entry
            updated = False
            for entry in snapshots:
                if entry["date"] == today:
                    entry["used"] = usage
                    entry["limit"] = limit
                    updated = True
                    break
            if not updated:
                snapshots.append({"date": today, "used": usage, "limit": limit})
            snapshots.sort(key=lambda e: e["date"])
            # Cap to _COST_SNAPSHOT_MAX_DAYS entries (keep most recent)
            if len(snapshots) > _COST_SNAPSHOT_MAX_DAYS:
                snapshots = snapshots[-_COST_SNAPSHOT_MAX_DAYS:]
            _write_cost_snapshots(provider, snapshots)
            return snapshots


def _compute_deltas(snapshots: list[dict[str, Any]], window_days: int) -> list[dict[str, Any]]:
    """Compute daily deltas from cumulative usage snapshots.

    Each snapshot carries cumulative ``used``; the delta for a day is
    the difference between that day's cumulative value and the previous
    day's.  The oldest day in the window has ``delta=None`` (no
    previous baseline).  If the cumulative value drops, treat that day
    as the start of a fresh series (for example after an API-key rotation)
    and use the current value as that day's delta instead of emitting a
    negative spend bar.
    """
    # Take only the last *window_days* entries
    window = snapshots[-window_days:] if len(snapshots) > window_days else list(snapshots)
    result: list[dict[str, Any]] = []
    for i, entry in enumerate(window):
        delta = None
        if i > 0 and entry.get("used") is not None and window[i - 1].get("used") is not None:
            delta = float(entry["used"]) - float(window[i - 1]["used"])
            if delta < 0:
                delta = float(entry["used"])
            # Rounding: avoid -0.0 and tiny floating-point noise
            if abs(delta) < 1e-9:
                delta = 0.0
            else:
                delta = round(delta, 6)
        result.append({
            "date": entry["date"],
            "used": entry.get("used"),
            "delta": delta,
        })
    return result


def get_provider_cost_history(provider_id: str | None = None, days: int = 7) -> dict[str, Any]:
    """Return daily cost-history snapshots with deltas for a provider.

    Currently only ``openrouter`` is supported.  On each call the
    endpoint fetches the current cumulative usage from the OpenRouter
    ``/auth/key`` endpoint, appends/updates today's snapshot, and
    returns the last *days* snapshots with per-day deltas.

    Returns a dict matching the existing API style (``ok``, ``provider``,
    ``status``, ``message``, …).
    """
    provider = (provider_id or "").strip().lower()
    if not provider:
        return {
            "ok": False,
            "provider": None,
            "status": "missing_provider",
            "message": "Provider parameter is required.  Use ?provider=openrouter",
        }

    if provider != "openrouter":
        display_name = _PROVIDER_DISPLAY.get(provider, provider.replace("-", " ").title())
        return {
            "ok": False,
            "provider": provider,
            "display_name": display_name,
            "supported": False,
            "status": "unsupported",
            "message": f"Cost history is not available for {display_name}. Only openrouter is supported in this release.",
        }

    display_name = _PROVIDER_DISPLAY.get("openrouter", "OpenRouter")
    monthly_budget = _get_provider_cost_budget()
    api_key = _get_provider_api_key("openrouter")
    if not api_key:
        return {
            "ok": False,
            "provider": "openrouter",
            "display_name": display_name,
            "supported": True,
            "status": "no_key",
            "monthly_budget": monthly_budget,
            "message": "OpenRouter cost history needs an OPENROUTER_API_KEY configured on the server.",
        }

    # Fetch current cumulative usage from OpenRouter
    key_info = _fetch_openrouter_key_usage(api_key)
    if key_info is None:
        # Upstream failure — still return any previously persisted snapshots
        # so the chart degrades gracefully instead of going blank.
        snapshots = _read_cost_snapshots("openrouter")
        deltas = _compute_deltas(snapshots, days)
        return {
            "ok": False,
            "provider": "openrouter",
            "display_name": display_name,
            "supported": True,
            "status": "unavailable",
            "window_days": days,
            "snapshots": deltas,
            "limit": None,
            "label": None,
            "monthly_budget": monthly_budget,
            "message": "OpenRouter cost history is temporarily unavailable. Showing last known data.",
        }

    # Persist today's snapshot
    try:
        snapshots = _append_cost_snapshot("openrouter", key_info["usage"], key_info["limit"])
    except Exception:
        logger.debug("Failed to persist cost snapshot for openrouter", exc_info=True)
        snapshots = _read_cost_snapshots("openrouter")

    deltas = _compute_deltas(snapshots, days)
    return {
        "ok": True,
        "provider": "openrouter",
        "display_name": display_name,
        "supported": True,
        "status": "available",
        "window_days": days,
        "snapshots": deltas,
        "limit": key_info.get("limit"),
        "label": key_info.get("label") or "OpenRouter credits",
        "monthly_budget": monthly_budget,
        "message": "OpenRouter cost history loaded.",
    }


__provider_exports__ = (
    "_COST_SNAPSHOTS_DIR_NAME",
    "_COST_SNAPSHOT_MAX_DAYS",
    "_COST_SNAPSHOT_LOCK",
    "_get_provider_cost_budget",
    "_cost_snapshots_dir",
    "_cost_snapshot_file_lock",
    "_fetch_openrouter_key_usage",
    "_read_cost_snapshots",
    "_write_cost_snapshots",
    "_append_cost_snapshot",
    "_compute_deltas",
    "get_provider_cost_history",
)

__all__ = __provider_exports__
