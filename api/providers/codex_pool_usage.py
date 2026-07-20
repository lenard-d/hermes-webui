"""Codex credential-pool account-limit probing and safe aggregation."""

from __future__ import annotations

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from urllib import request as urllib_request


_CODEX_DEFAULT_BASE_URL = "https://chatgpt.com/backend-api/codex"
_CODEX_POOL_USAGE_TIMEOUT_SECONDS = 4.0
_CODEX_POOL_MAX_WORKERS = 6


def _iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat().replace("+00:00", "Z")
    text = str(value).strip()
    return text or None


def _number(value: Any) -> int | float | None:
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
    except Exception:
        return None


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _title_case_slug(value: Any) -> str | None:
    cleaned = str(value or "").strip()
    if not cleaned:
        return None
    return cleaned.replace("_", " ").replace("-", " ").title()


def _resolve_codex_usage_url(base_url: Any) -> str:
    normalized = str(base_url or "").strip().rstrip("/") or _CODEX_DEFAULT_BASE_URL
    if normalized.endswith("/codex"):
        normalized = normalized[: -len("/codex")]
    if "/backend-api" in normalized:
        return normalized + "/wham/usage"
    return normalized + "/api/codex/usage"


def _jwt_claims(token: Any) -> dict[str, Any]:
    if not isinstance(token, str) or token.count(".") != 2:
        return {}
    payload = token.split(".")[1]
    payload += "=" * ((4 - len(payload) % 4) % 4)
    try:
        claims = json.loads(
            base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8")
        )
    except Exception:
        return {}
    return claims if isinstance(claims, dict) else {}


def _codex_usage_headers(access_token: str) -> dict[str, str]:
    headers = {
        "Authorization": "Bearer " + access_token,
        "Accept": "application/json",
        "User-Agent": "codex_cli_rs/0.0.0 (Hermes WebUI)",
        "originator": "codex_cli_rs",
    }
    auth_claim = _jwt_claims(access_token).get("https://api.openai.com/auth")
    account_id = (
        auth_claim.get("chatgpt_account_id") if isinstance(auth_claim, dict) else None
    )
    if isinstance(account_id, str) and account_id.strip():
        headers["ChatGPT-Account-ID"] = account_id.strip()
    return headers


def _entry_value(entry: Any, *names: str) -> str | None:
    for name in names:
        try:
            value = getattr(entry, name)
        except Exception:
            value = None
        if value in (None, ""):
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _codex_snapshot_from_usage_payload(payload: Any) -> Any:
    payload = payload if isinstance(payload, dict) else {}
    rate_limit = payload.get("rate_limit")
    rate_limit = rate_limit if isinstance(rate_limit, dict) else {}
    windows = []
    for key, label in (("primary_window", "Session"), ("secondary_window", "Weekly")):
        window = rate_limit.get(key)
        if not isinstance(window, dict):
            continue
        used = _number(window.get("used_percent"))
        if used is None:
            continue
        windows.append(
            SimpleNamespace(
                label=label,
                used_percent=float(used),
                reset_at=_parse_dt(window.get("reset_at")),
                detail=None,
            )
        )

    details = []
    credits = payload.get("credits")
    if isinstance(credits, dict) and credits.get("has_credits"):
        balance = _number(credits.get("balance"))
        if balance is not None:
            details.append("Credits balance: $" + format(float(balance), ".2f"))
        elif credits.get("unlimited"):
            details.append("Credits balance: unlimited")

    return SimpleNamespace(
        provider="openai-codex",
        source="usage_api",
        title="Account limits",
        plan=_title_case_slug(payload.get("plan_type")),
        windows=tuple(windows),
        details=tuple(details),
        available=bool(windows or details),
        unavailable_reason=None,
        fetched_at=datetime.now(timezone.utc),
    )


def _snapshot_windows_payload(snapshot: Any) -> list[dict[str, Any]]:
    windows = []
    for window in getattr(snapshot, "windows", ()) or ():
        label = str(getattr(window, "label", "") or "").strip()
        if not label:
            continue
        used_percent = _number(getattr(window, "used_percent", None))
        remaining_percent = None
        if used_percent is not None:
            remaining_percent = max(0.0, min(100.0, 100.0 - float(used_percent)))
        windows.append(
            {
                "label": label,
                "used_percent": used_percent,
                "remaining_percent": remaining_percent,
                "reset_at": _iso(getattr(window, "reset_at", None)),
                "detail": getattr(window, "detail", None),
            }
        )
    return windows


def _snapshot_details_payload(snapshot: Any) -> list[str]:
    return [
        str(detail).strip()
        for detail in (getattr(snapshot, "details", ()) or ())
        if str(detail).strip()
    ]


def _safe_entry_label(entry: Any, index: int) -> str:
    label = _entry_value(entry, "label", "source") or "Credential " + str(index)
    label = " ".join(label.split())
    return label[:61].rstrip() + "..." if len(label) > 64 else label


def _safe_unavailable_reason(reason: Any) -> str | None:
    text = " ".join(str(reason or "").split())
    if not text:
        return None
    lowered = text.lower()
    sensitive_terms = (
        "access_token",
        "refresh_token",
        "authorization",
        "bearer ",
        "jwt",
        "secret",
    )
    if any(term in lowered for term in sensitive_terms):
        return "Usage unavailable for this credential."
    return text[:180]


def _entry_exhausted_ttl_seconds(error_code: Any) -> int:
    return 5 * 60 if str(error_code or "").strip() == "401" else 60 * 60


def _entry_pool_exhausted_until(entry: Any) -> datetime | None:
    if str(_entry_value(entry, "last_status") or "").strip().lower() != "exhausted":
        return None
    reset_at = _parse_dt(getattr(entry, "last_error_reset_at", None))
    if reset_at is not None:
        return reset_at
    status_at = _parse_dt(getattr(entry, "last_status_at", None))
    if status_at is None:
        return None
    return status_at + timedelta(
        seconds=_entry_exhausted_ttl_seconds(_entry_value(entry, "last_error_code"))
    )


def _entry_is_pool_exhausted(entry: Any) -> bool:
    exhausted_until = _entry_pool_exhausted_until(entry)
    return exhausted_until is not None and datetime.now(timezone.utc) < exhausted_until


def _entry_pool_retry_after(entry: Any) -> str | None:
    return _iso(_entry_pool_exhausted_until(entry))


def _entry_pool_exhausted_reason(entry: Any) -> str:
    code = _entry_value(entry, "last_error_code")
    reset_at = _entry_pool_retry_after(entry)
    reason = "Credential pool marked this credential exhausted"
    if code:
        reason += " after provider status " + code
    if reset_at:
        reason += "; retry after " + reset_at
    return reason + "."


def _fetch_codex_entry_snapshot(entry: Any) -> tuple[Any, bool, str | None]:
    access_token = _entry_value(entry, "runtime_api_key", "access_token")
    if not access_token:
        return None, False, "No runtime token available."
    base_url = (
        _entry_value(entry, "runtime_base_url", "base_url") or _CODEX_DEFAULT_BASE_URL
    )
    request = urllib_request.Request(
        _resolve_codex_usage_url(base_url),
        headers=_codex_usage_headers(access_token),
    )
    with urllib_request.urlopen(
        request, timeout=_CODEX_POOL_USAGE_TIMEOUT_SECONDS
    ) as response:
        payload = json.loads(response.read().decode("utf-8") or "{}")
    return _codex_snapshot_from_usage_payload(payload), True, None


def _best_remaining_by_window(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("status") != "available":
            continue
        label = row.get("label") or "Credential"
        for window in row.get("windows") or []:
            if not isinstance(window, dict):
                continue
            window_label = str(window.get("label") or "").strip()
            remaining = _number(window.get("remaining_percent"))
            if not window_label or remaining is None:
                continue
            candidate = {
                "label": window_label,
                "remaining_percent": remaining,
                "used_percent": window.get("used_percent"),
                "reset_at": window.get("reset_at"),
                "detail": window.get("detail"),
                "credential_label": label,
            }
            current = best.get(window_label.lower())
            if current is None or float(remaining) > float(
                current.get("remaining_percent") or -1
            ):
                best[window_label.lower()] = candidate
    return list(best.values())


def _next_reset_at(rows: list[dict[str, Any]]) -> str | None:
    best_dt = None
    best_text = None
    for row in rows:
        for window in row.get("windows") or []:
            if not isinstance(window, dict):
                continue
            dt = _parse_dt(window.get("reset_at"))
            if dt is not None and (best_dt is None or dt < best_dt):
                best_dt = dt
                best_text = _iso(dt)
    return best_text


def _codex_pool_snapshot(
    entries: list[Any], rows: list[dict[str, Any]], queried: int
) -> Any:
    available_rows = [row for row in rows if row.get("status") == "available"]
    exhausted_rows = [row for row in rows if row.get("status") == "exhausted"]
    failed_rows = [
        row for row in rows if row.get("status") not in {"available", "exhausted"}
    ]
    plans = []
    for row in rows:
        plan = row.get("plan")
        if plan and plan not in plans:
            plans.append(plan)
    best_windows = _best_remaining_by_window(rows)
    pool = {
        "total_credentials": len(entries),
        "queried_credentials": queried,
        "available_credentials": len(available_rows),
        "exhausted_credentials": len(exhausted_rows),
        "failed_credentials": len(failed_rows),
        "plans": plans,
        "next_reset_at": _next_reset_at(rows),
        "best_remaining_by_window": best_windows,
        "credentials": rows,
    }
    details = [f"{len(available_rows)}/{len(entries)} credentials available"]
    if exhausted_rows:
        details.append(f"{len(exhausted_rows)} exhausted")
    if failed_rows:
        details.append(f"{len(failed_rows)} failed to load")
    if plans:
        details.append("Plans: " + ", ".join(plans))
    windows = tuple(
        SimpleNamespace(
            label=window.get("label"),
            used_percent=window.get("used_percent"),
            reset_at=window.get("reset_at"),
            detail=f"Best of {len(available_rows)} available credentials",
        )
        for window in best_windows
    )
    return SimpleNamespace(
        provider="openai-codex",
        source="usage_api_pool",
        title="Account limits",
        plan=plans[0] if len(plans) == 1 else None,
        windows=windows,
        details=tuple(details),
        available=bool(available_rows),
        unavailable_reason=(
            None
            if available_rows
            else "No Codex pool credentials returned available account limits."
        ),
        fetched_at=datetime.now(timezone.utc),
        pool=pool,
    )


def _codex_pool_exhausted_row(entry: Any, index: int) -> dict[str, Any]:
    return {
        "label": _safe_entry_label(entry, index),
        "status": "exhausted",
        "plan": None,
        "windows": [],
        "details": [],
        "unavailable_reason": _entry_pool_exhausted_reason(entry),
        "retry_after": _entry_pool_retry_after(entry),
        "fetched_at": None,
    }


def _probe_codex_pool_entry(item: tuple[int, Any]) -> tuple[int, dict[str, Any], int]:
    index, entry = item
    label = _safe_entry_label(entry, index)
    did_query_count = 0
    try:
        snapshot, did_query, reason = _fetch_codex_entry_snapshot(entry)
        if did_query:
            did_query_count = 1
    except Exception as exc:
        snapshot = None
        reason = str(exc)
    windows = _snapshot_windows_payload(snapshot) if snapshot is not None else []
    details = _snapshot_details_payload(snapshot) if snapshot is not None else []
    available = (
        bool(getattr(snapshot, "available", False)) if snapshot is not None else False
    )
    row = {
        "label": label,
        "status": "available" if available else "unavailable",
        "plan": getattr(snapshot, "plan", None) if snapshot is not None else None,
        "windows": windows,
        "details": details,
        "unavailable_reason": (
            None
            if available
            else _safe_unavailable_reason(
                reason or getattr(snapshot, "unavailable_reason", None)
            )
        ),
        "fetched_at": _iso(getattr(snapshot, "fetched_at", None))
        if snapshot is not None
        else None,
    }
    return index, row, did_query_count


def fetch_codex_account_usage_from_pool() -> Any:
    """Return an aggregated safe snapshot, or ``None`` when no pool is usable."""
    try:
        from agent.credential_pool import load_pool

        pool = load_pool("openai-codex")
        entries = (
            list(pool.entries())
            if pool is not None and hasattr(pool, "entries")
            else []
        )
        if not entries:
            return None
        rows_by_index: dict[int, dict[str, Any]] = {}
        probe_items = []
        queried = 0
        for index, entry in enumerate(entries, start=1):
            if _entry_is_pool_exhausted(entry):
                rows_by_index[index] = _codex_pool_exhausted_row(entry, index)
            else:
                probe_items.append((index, entry))
        if probe_items:
            with ThreadPoolExecutor(
                max_workers=min(_CODEX_POOL_MAX_WORKERS, len(probe_items))
            ) as executor:
                for index, row, did_query_count in executor.map(
                    _probe_codex_pool_entry, probe_items
                ):
                    rows_by_index[index] = row
                    queried += did_query_count
        rows = [rows_by_index[index] for index in range(1, len(entries) + 1)]
        return _codex_pool_snapshot(entries, rows, queried)
    except Exception:
        return None


__all__ = ("fetch_codex_account_usage_from_pool",)
