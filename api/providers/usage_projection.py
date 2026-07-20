"""Sanitized provider usage projections shared by quota and cost views."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any


def _quota_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        text = str(value).strip()
        if not text:
            return None
        number = float(text)
        return int(number) if number.is_integer() else number
    except (TypeError, ValueError):
        return None


def _sanitize_openrouter_quota(payload: Any) -> dict[str, int | float | None]:
    """Project only non-secret numeric fields from OpenRouter's key response."""
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    if not isinstance(payload, dict):
        payload = {}
    return {
        "limit_remaining": _quota_number(payload.get("limit_remaining")),
        "usage": _quota_number(payload.get("usage")),
        "limit": _quota_number(payload.get("limit")),
    }


def _isoformat_utc(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    text = str(value).strip()
    return text or None


def _serialize_account_usage_snapshot(snapshot: Any) -> dict[str, Any] | None:
    """Serialize an agent usage snapshot without exposing credential material."""
    if snapshot is None:
        return None
    windows: list[dict[str, Any]] = []
    for window in getattr(snapshot, "windows", ()) or ():
        label = str(getattr(window, "label", "") or "").strip()
        if not label:
            continue
        used_percent = _quota_number(getattr(window, "used_percent", None))
        remaining_percent = None
        if used_percent is not None:
            remaining_percent = max(0.0, min(100.0, 100.0 - float(used_percent)))
        windows.append(
            {
                "label": label,
                "used_percent": used_percent,
                "remaining_percent": remaining_percent,
                "reset_at": _isoformat_utc(getattr(window, "reset_at", None)),
                "detail": str(getattr(window, "detail", "") or "").strip() or None,
            }
        )

    details = [
        str(detail).strip()
        for detail in (getattr(snapshot, "details", ()) or ())
        if str(detail).strip()
    ]
    unavailable_reason = (
        str(getattr(snapshot, "unavailable_reason", "") or "").strip() or None
    )
    result = {
        "provider": str(getattr(snapshot, "provider", "") or "").strip() or None,
        "source": str(getattr(snapshot, "source", "") or "").strip() or None,
        "title": str(getattr(snapshot, "title", "") or "").strip() or "Account limits",
        "plan": str(getattr(snapshot, "plan", "") or "").strip() or None,
        "windows": windows,
        "details": details,
        "available": bool(getattr(snapshot, "available", bool(windows or details)))
        and not unavailable_reason,
        "unavailable_reason": unavailable_reason,
        "fetched_at": _isoformat_utc(getattr(snapshot, "fetched_at", None)),
    }
    pool = getattr(snapshot, "pool", None)
    if isinstance(pool, dict):
        result["pool"] = pool
    return result


def _account_usage_payload_to_snapshot(payload: Any) -> Any:
    """Rehydrate the JSON-safe child protocol payload into the agent shape."""
    if not isinstance(payload, dict):
        return None
    windows = tuple(
        SimpleNamespace(
            label=window.get("label"),
            used_percent=window.get("used_percent"),
            reset_at=window.get("reset_at"),
            detail=window.get("detail"),
        )
        for window in (payload.get("windows") or ())
        if isinstance(window, dict)
    )
    return SimpleNamespace(
        provider=payload.get("provider"),
        source=payload.get("source"),
        title=payload.get("title"),
        plan=payload.get("plan"),
        windows=windows,
        details=tuple(payload.get("details") or ()),
        available=bool(payload.get("available")),
        unavailable_reason=payload.get("unavailable_reason"),
        fetched_at=payload.get("fetched_at"),
        pool=payload.get("pool") if isinstance(payload.get("pool"), dict) else None,
    )


__all__ = (
    "_quota_number",
    "_sanitize_openrouter_quota",
    "_isoformat_utc",
    "_serialize_account_usage_snapshot",
    "_account_usage_payload_to_snapshot",
)
