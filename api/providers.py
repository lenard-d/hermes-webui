"""Hermes Web UI -- provider management endpoints.

Provides CRUD operations for configuring provider API keys post-onboarding.
Closes #586 (allow provider key update) and part of #604 (model picker
multi-provider support).
"""

from __future__ import annotations

import atexit
import base64  # noqa: F401 -- credential-store facade dependency
import copy
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager, nullcontext  # noqa: F401 -- provider-part facade dependencies
from datetime import datetime, timedelta, timezone  # noqa: F401 -- credential-store facade dependency
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

try:  # POSIX-only; Windows-style environments fall back to process-local locking.
    import fcntl
except ImportError:  # pragma: no cover - exercised only where fcntl is unavailable
    fcntl = None  # type: ignore[assignment]

from api.config import (
    _coerce_provider_cost_budget,  # noqa: F401 -- cost-history facade dependency
    _custom_provider_slug_from_name,
    _get_label_for_model,  # noqa: F401 -- compatibility re-export
    _models_from_live_provider_ids,
    _pool_entry_payloads,  # noqa: F401 -- credential-store facade dependency
    _read_live_provider_model_ids,
    _read_visible_codex_cache_model_ids,
    _save_yaml_config_file,
    _thread_local_env_value,
    get_config,
    invalidate_models_cache,
    reload_config,
)
from api.model_catalog import (
    PROVIDER_DISPLAY as _PROVIDER_DISPLAY,
    PROVIDER_MODELS as _PROVIDER_MODELS,
)
from api.plugin_providers import (
    effective_provider_display_name,
    effective_provider_env_var,  # noqa: F401 -- credential-store facade dependency
    is_plugin_model_provider,
    plugin_model_provider_ids,
)
from api.provider_parts import cost_history as _cost_history_part
from api.provider_parts import credential_store as _credential_store_part
from api.provider_parts._binding import install_provider_part

logger = logging.getLogger(__name__)


# Extracted provider modules are rebound into this compatibility facade so
# existing imports and monkeypatches continue to observe one canonical seam.
install_provider_part(globals(), _credential_store_part)
install_provider_part(globals(), _cost_history_part)

if TYPE_CHECKING:
    _PROVIDER_CREDENTIAL_ENV_VARS = ()
    _PROVIDER_ENV_VAR_ALIASES = {}
    _OAUTH_PROVIDERS = frozenset()
    _SELF_HOSTED_PROVIDER_IDS = frozenset()
    _provider_env_var_for = _credential_store_part._provider_env_var_for
    _provider_is_oauth = _credential_store_part._provider_is_oauth
    _provider_has_key = _credential_store_part._provider_has_key
    _provider_value_counts_as_api_key = _credential_store_part._provider_value_counts_as_api_key
    _provider_has_shadowed_codex_oauth_value = _credential_store_part._provider_has_shadowed_codex_oauth_value
    _get_provider_api_key = _credential_store_part._get_provider_api_key
    _local_pool_snapshot = _credential_store_part._local_pool_snapshot
    _load_env_file = _credential_store_part._load_env_file
    _write_env_file = _credential_store_part._write_env_file
    _custom_provider_name_matches = _credential_store_part._custom_provider_name_matches



_OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
_PROVIDER_QUOTA_TIMEOUT_SECONDS = 3.0
_ACCOUNT_USAGE_SUBPROCESS_TIMEOUT_SECONDS = 35.0
_ACCOUNT_USAGE_CACHE_TTL_SECONDS = 45.0
_PROVIDERS_CACHE_TTL_SECONDS = 30.0
_ACCOUNT_USAGE_CACHE_MAX_ENTRIES = 64
_ACCOUNT_USAGE_WORKER_IDLE_SECONDS = 5 * 60
_ACCOUNT_USAGE_PROVIDERS = frozenset({"openai-codex", "anthropic"})

# Upper bound on simultaneous profile-isolated quota probe subprocesses.
# Each probe runs a Python child for up to 35 s; capping concurrency prevents
# resource exhaustion when the UI polls all providers rapidly. The limit is
# deliberately low (2) since _ACCOUNT_USAGE_SUBPROCESS_TIMEOUT_SECONDS is
# already 35 s and probe I/O is lightweight HTTP calls.
_MAX_CONCURRENT_ACCOUNT_USAGE_PROBES = 2

# Parent-death-signal setup: on Linux, arrange for the quota-probe child to
# receive SIGTERM when the WebUI parent dies (e.g. systemctl restart, OOM kill).
# This prevents probe children from becoming orphaned zombies that continue
# calling the provider API indefinitely after the WebUI process is gone.
# We use prctl(PR_SET_DEATHSIG, SIGTERM) which is standard on modern Linux
# kernels and available via ctypes (no external C extension needed).
# If prctl is unavailable (non-Linux, or Linux without prctl support), the
# probe child exits normally when its parent (WebUI) terminates -- on macOS/
# Windows this is handled by OS-level process tree cleanup.
# Portable parent-death-signal bootstrap.  On Linux this arranges for the
# probe child to receive SIGTERM when the WebUI parent dies (systemctl
# restart, OOM kill, etc.), preventing orphaned zombie probes from continuing
# to call the provider API indefinitely.  Non-Linux platforms (macOS, Windows)
# rely on OS-level process-tree cleanup instead; this variable is then unused.
# prctl(PR_SET_DEATHSIG, SIGTERM) is available via ctypes without any C
# extension — the same technique used throughout the Hermes codebase.
_ACCOUNT_USAGE_PARENT_DEATHSIG_BOOTSTRAP = (
    # fmt: off
    # Lines are written as string literals so this block passes
    # `python3 -m py_compile` cleanly and is safe to include verbatim
    # inside the single argument string passed to `python -c ...`.
    'import sys\n'
    'try:\n'
    '    import ctypes, signal\n'
    '    libc = ctypes.CDLL(None)\n'
    '    libc.prctl(1, signal.SIGTERM)   # PR_SET_DEATHSIG=1, SIGTERM=15\n'
    'except Exception:\n'
    '    pass\n'
    # fmt: on
)


# Module-level cap on concurrent quota-probe subprocesses.
# Lazily created so this module compiles even when threading isn't ready.
_account_usage_probe_semaphore: threading.BoundedSemaphore | None = None

# Short-lived account-usage cache. The Codex pooled probe may check multiple
# credentials, so cache sanitized snapshots briefly to avoid re-querying the
# provider on every Settings repaint/profile-panel refresh. Pool composition
# changes can be stale for at most this TTL; that is preferred to hammering the
# provider usage API while the Settings panel is open. Transient None probe
# results are intentionally not cached; known exhausted/unavailable states are
# represented as non-None snapshots and remain cacheable.
_account_usage_status_cache: dict[tuple[str, str, str], tuple[float, Any]] = {}
_account_usage_status_cache_lock = threading.Lock()
_providers_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
_providers_cache_lock = threading.Lock()
_account_usage_worker_pool: dict[str, list["_AccountUsageProbeWorker"]] = {}
_account_usage_worker_pool_lock = threading.Lock()

# Per-home worker pool configuration for probe tail-latency reduction (#3787)
_ACCOUNT_USAGE_WORKERS_PER_HOME = 2


def _get_account_usage_probe_semaphore() -> threading.BoundedSemaphore:
    global _account_usage_probe_semaphore
    if _account_usage_probe_semaphore is None:
        _account_usage_probe_semaphore = threading.BoundedSemaphore(
            _MAX_CONCURRENT_ACCOUNT_USAGE_PROBES
        )
    return _account_usage_probe_semaphore


# ── preexec_fn: parent-death signal for the probe subprocess ─────────────────
# On POSIX/Linux, arrange for the child to receive SIGTERM when the WebUI
# parent dies (systemctl restart, OOM kill, etc.).  The parent's bootstrap
# code (_ACCOUNT_USAGE_PARENT_DEATHSIG_BOOTSTRAP) also covers the grandchild
# fork inside the child, but this preexec_fn handles the direct child-process
# case.  Returns None on non-POSIX or when prctl is unavailable so that
# subprocess startup works on Windows/macOS without changes.
def _account_usage_preexec_fn() -> None:
    try:
        import ctypes
        libc = ctypes.CDLL(None)
        libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG=1, SIGTERM=15
    except Exception:
        pass


_ACCOUNT_USAGE_SUBPROCESS_CODE = r"""
import base64
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib import request as urllib_request

from agent.account_usage import fetch_account_usage


_CODEX_DEFAULT_BASE_URL = "https://chatgpt.com/backend-api/codex"
_CODEX_POOL_USAGE_TIMEOUT_SECONDS = 4.0
_CODEX_POOL_MAX_WORKERS = 6


def _iso(value):
    if value in (None, ""):
        return None
    if hasattr(value, "isoformat"):
        text = value.isoformat()
        return text.replace("+00:00", "Z")
    text = str(value).strip()
    return text or None


def _snapshot_payload(snapshot):
    if snapshot is None:
        return None
    windows = []
    for window in getattr(snapshot, "windows", ()) or ():
        windows.append({
            "label": str(getattr(window, "label", "") or ""),
            "used_percent": getattr(window, "used_percent", None),
            "reset_at": _iso(getattr(window, "reset_at", None)),
            "detail": getattr(window, "detail", None),
        })
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


def _snapshot_available(snapshot):
    if snapshot is None:
        return False
    try:
        return bool(getattr(snapshot, "available", False))
    except Exception:
        return False


def _number(value):
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


def _parse_dt(value):
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


def _title_case_slug(value):
    cleaned = str(value or "").strip()
    if not cleaned:
        return None
    return cleaned.replace("_", " ").replace("-", " ").title()


def _resolve_codex_usage_url(base_url):
    normalized = str(base_url or "").strip().rstrip("/") or _CODEX_DEFAULT_BASE_URL
    if normalized.endswith("/codex"):
        normalized = normalized[: -len("/codex")]
    if "/backend-api" in normalized:
        return normalized + "/wham/usage"
    return normalized + "/api/codex/usage"


def _jwt_claims(token):
    if not isinstance(token, str) or token.count(".") != 2:
        return {}
    payload = token.split(".")[1]
    payload += "=" * ((4 - len(payload) % 4) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8"))
    except Exception:
        return {}
    return claims if isinstance(claims, dict) else {}


def _codex_usage_headers(access_token):
    headers = {
        "Authorization": "Bearer " + access_token,
        "Accept": "application/json",
        "User-Agent": "codex_cli_rs/0.0.0 (Hermes WebUI)",
        "originator": "codex_cli_rs",
    }
    auth_claim = _jwt_claims(access_token).get("https://api.openai.com/auth")
    account_id = None
    if isinstance(auth_claim, dict):
        account_id = auth_claim.get("chatgpt_account_id")
    if isinstance(account_id, str) and account_id.strip():
        headers["ChatGPT-Account-ID"] = account_id.strip()
    return headers


def _entry_value(entry, *names):
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


def _codex_snapshot_from_usage_payload(payload):
    if not isinstance(payload, dict):
        payload = {}
    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        rate_limit = {}
    windows = []
    for key, label in (("primary_window", "Session"), ("secondary_window", "Weekly")):
        window = rate_limit.get(key)
        if not isinstance(window, dict):
            continue
        used = _number(window.get("used_percent"))
        if used is None:
            continue
        windows.append(SimpleNamespace(
            label=label,
            used_percent=float(used),
            reset_at=_parse_dt(window.get("reset_at")),
            detail=None,
        ))

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


def _snapshot_windows_payload(snapshot):
    windows = []
    for window in getattr(snapshot, "windows", ()) or ():
        label = str(getattr(window, "label", "") or "").strip()
        if not label:
            continue
        used_percent = _number(getattr(window, "used_percent", None))
        remaining_percent = None
        if used_percent is not None:
            remaining_percent = max(0.0, min(100.0, 100.0 - float(used_percent)))
        windows.append({
            "label": label,
            "used_percent": used_percent,
            "remaining_percent": remaining_percent,
            "reset_at": _iso(getattr(window, "reset_at", None)),
            "detail": getattr(window, "detail", None),
        })
    return windows


def _snapshot_details_payload(snapshot):
    return [
        str(detail).strip()
        for detail in (getattr(snapshot, "details", ()) or ())
        if str(detail).strip()
    ]


def _safe_entry_label(entry, index):
    label = _entry_value(entry, "label", "source") or ""
    if not label:
        label = "Credential " + str(index)
    label = " ".join(str(label).split())
    if len(label) > 64:
        label = label[:61].rstrip() + "..."
    return label


def _safe_unavailable_reason(reason):
    text = " ".join(str(reason or "").split())
    if not text:
        return None
    lowered = text.lower()
    sensitive_terms = ("access_token", "refresh_token", "authorization", "bearer ", "jwt", "secret")
    if any(term in lowered for term in sensitive_terms):
        return "Usage unavailable for this credential."
    return text[:180]


def _entry_exhausted_ttl_seconds(error_code):
    code = str(error_code or "").strip()
    if code == "401":
        return 5 * 60
    return 60 * 60


def _entry_pool_exhausted_until(entry):
    if str(_entry_value(entry, "last_status") or "").strip().lower() != "exhausted":
        return None
    reset_at = _parse_dt(getattr(entry, "last_error_reset_at", None))
    if reset_at is not None:
        return reset_at
    status_at = _parse_dt(getattr(entry, "last_status_at", None))
    if status_at is None:
        return None
    return status_at + timedelta(seconds=_entry_exhausted_ttl_seconds(_entry_value(entry, "last_error_code")))


def _entry_is_pool_exhausted(entry):
    exhausted_until = _entry_pool_exhausted_until(entry)
    return exhausted_until is not None and datetime.now(timezone.utc) < exhausted_until


def _entry_pool_exhausted_reason(entry):
    code = _entry_value(entry, "last_error_code")
    reset_at = _entry_pool_retry_after(entry)
    reason = "Credential pool marked this credential exhausted"
    if code:
        reason += " after provider status " + code
    if reset_at:
        reason += "; retry after " + reset_at
    return reason + "."


def _entry_pool_retry_after(entry):
    return _iso(_entry_pool_exhausted_until(entry))


def _fetch_codex_entry_snapshot(entry):
    access_token = _entry_value(entry, "runtime_api_key", "access_token")
    if not access_token:
        return None, False, "No runtime token available."
    base_url = _entry_value(entry, "runtime_base_url", "base_url") or _CODEX_DEFAULT_BASE_URL
    request = urllib_request.Request(
        _resolve_codex_usage_url(base_url),
        headers=_codex_usage_headers(access_token),
    )
    with urllib_request.urlopen(request, timeout=_CODEX_POOL_USAGE_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8") or "{}")
    return _codex_snapshot_from_usage_payload(payload), True, None


def _best_remaining_by_window(rows):
    best = {}
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
            # The normalized Codex account-limit payload currently exposes
            # percentages, not absolute request/token capacity. If absolute
            # remaining capacity becomes available, prefer it here.
            if current is None or float(remaining) > float(current.get("remaining_percent") or -1):
                best[window_label.lower()] = candidate
    return list(best.values())


def _next_reset_at(rows):
    best_dt = None
    best_text = None
    for row in rows:
        for window in row.get("windows") or []:
            if not isinstance(window, dict):
                continue
            reset_text = window.get("reset_at")
            dt = _parse_dt(reset_text)
            if dt is None:
                continue
            if best_dt is None or dt < best_dt:
                best_dt = dt
                best_text = _iso(dt)
    return best_text


def _codex_pool_snapshot(entries, rows, queried):
    available_rows = [row for row in rows if row.get("status") == "available"]
    exhausted_rows = [row for row in rows if row.get("status") == "exhausted"]
    failed_rows = [row for row in rows if row.get("status") not in {"available", "exhausted"}]
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
    details = [str(len(available_rows)) + "/" + str(len(entries)) + " credentials available"]
    if exhausted_rows:
        details.append(str(len(exhausted_rows)) + " exhausted")
    if failed_rows:
        details.append(str(len(failed_rows)) + " failed to load")
    if plans:
        details.append("Plans: " + ", ".join(plans))
    plan = plans[0] if len(plans) == 1 else None
    windows = tuple(
        SimpleNamespace(
            label=window.get("label"),
            used_percent=window.get("used_percent"),
            reset_at=window.get("reset_at"),
            detail="Best of " + str(len(available_rows)) + " available credentials",
        )
        for window in best_windows
    )
    return SimpleNamespace(
        provider="openai-codex",
        source="usage_api_pool",
        title="Account limits",
        plan=plan,
        windows=windows,
        details=tuple(details),
        available=bool(available_rows),
        unavailable_reason=None if available_rows else "No Codex pool credentials returned available account limits.",
        fetched_at=datetime.now(timezone.utc),
        pool=pool,
    )


def _codex_pool_exhausted_row(entry, index):
    label = _safe_entry_label(entry, index)
    retry_after = _entry_pool_retry_after(entry)
    return {
        "label": label,
        "status": "exhausted",
        "plan": None,
        "windows": [],
        "details": [],
        "unavailable_reason": _entry_pool_exhausted_reason(entry),
        "retry_after": retry_after,
        "fetched_at": None,
    }


def _probe_codex_pool_entry(item):
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
    snapshot_available = _snapshot_available(snapshot)
    status = "available" if snapshot_available else "unavailable"
    row = {
        "label": label,
        "status": status,
        "plan": getattr(snapshot, "plan", None) if snapshot is not None else None,
        "windows": windows,
        "details": details,
        "unavailable_reason": None if snapshot_available else _safe_unavailable_reason(reason or getattr(snapshot, "unavailable_reason", None)),
        "fetched_at": _iso(getattr(snapshot, "fetched_at", None)) if snapshot is not None else None,
    }
    return index, row, did_query_count


def _fetch_codex_account_usage_from_pool():
    try:
        from agent.credential_pool import load_pool

        pool = load_pool("openai-codex")
        entries = list(pool.entries()) if pool is not None and hasattr(pool, "entries") else []
        if not entries:
            return None
        rows_by_index = {}
        probe_items = []
        queried = 0
        for index, entry in enumerate(entries, start=1):
            if _entry_is_pool_exhausted(entry):
                rows_by_index[index] = _codex_pool_exhausted_row(entry, index)
            else:
                probe_items.append((index, entry))
        if probe_items:
            max_workers = min(_CODEX_POOL_MAX_WORKERS, len(probe_items))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                for index, row, did_query_count in executor.map(_probe_codex_pool_entry, probe_items):
                    rows_by_index[index] = row
                    queried += did_query_count
        rows = [rows_by_index[index] for index in range(1, len(entries) + 1)]
        return _codex_pool_snapshot(entries, rows, queried)
    except Exception:
        return None


def _fetch_snapshot(provider, api_key, env_var=None):
    previous = os.environ.get(env_var) if env_var else None
    had_previous = bool(env_var and env_var in os.environ)
    if env_var and api_key:
        os.environ[env_var] = api_key
    try:
        try:
            snapshot = fetch_account_usage(provider, api_key=api_key)
        except Exception:
            snapshot = None
        if str(provider or "").strip().lower() == "openai-codex":
            pool_snapshot = _fetch_codex_account_usage_from_pool()
            if isinstance(getattr(pool_snapshot, "pool", None), dict):
                snapshot = pool_snapshot
        return _snapshot_payload(snapshot)
    finally:
        if env_var and api_key:
            if had_previous:
                os.environ[env_var] = previous
            else:
                os.environ.pop(env_var, None)


def _run_worker():
    for raw_line in sys.stdin:
        try:
            request = json.loads(raw_line)
            provider = request.get("provider")
            api_key = request.get("api_key") or None
            env_var = request.get("env_var") or None
            payload = _fetch_snapshot(provider, api_key, env_var=env_var)
        except Exception:
            payload = None
        print(json.dumps(payload), flush=True)


if len(sys.argv) > 1 and sys.argv[1] == "--worker":
    _run_worker()
else:
    provider = sys.argv[1]
    api_key = sys.argv[2] or None
    print(json.dumps(_fetch_snapshot(provider, api_key)), flush=True)
"""




def _get_hermes_home() -> Path:
    """Return the active Hermes home directory."""
    try:
        from api.profiles import get_active_hermes_home
        return get_active_hermes_home()
    except ImportError:
        return Path.home() / ".hermes"


def _providers_file_mtime_ns(path: Path) -> int:
    """Best-effort file mtime for providers-cache invalidation."""
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _providers_config_fingerprint(cfg: Any) -> str:
    """Stable fingerprint for config fields that shape the Providers response."""
    try:
        return hashlib.sha256(
            json.dumps(cfg, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    except Exception:
        return repr(cfg)


def _providers_cache_key(cfg: Any) -> tuple[Any, ...]:
    """Return a profile-scoped cache key for ``get_providers()`` (#6010).

    The endpoint reads provider state from the active Hermes home plus the
    current config.  Include the home path and the two files users commonly
    mutate from Settings so a short TTL never crosses profile boundaries or
    masks immediate credential/config changes.
    """
    home = _get_hermes_home()
    try:
        home_key = str(home.resolve())
    except OSError:
        home_key = str(home)
    return (
        home_key,
        _providers_file_mtime_ns(home / ".env"),
        _providers_file_mtime_ns(home / "config.yaml"),
        _providers_config_fingerprint(cfg),
    )


def _get_cached_providers(cache_key: tuple[Any, ...]) -> dict[str, Any] | None:
    now = time.monotonic()
    with _providers_cache_lock:
        cached = _providers_cache.get(cache_key)
        if cached is None:
            return None
        ts, payload = cached
        if now - ts >= _PROVIDERS_CACHE_TTL_SECONDS:
            _providers_cache.pop(cache_key, None)
            return None
        return copy.deepcopy(payload)


def _store_cached_providers(cache_key: tuple[Any, ...], payload: dict[str, Any]) -> dict[str, Any]:
    with _providers_cache_lock:
        # Single-entry by design: /api/providers is cacheable only for the
        # active profile/config snapshot, so clear older snapshots to avoid
        # retaining unbounded provider metadata across profile switches.
        _providers_cache.clear()
        _providers_cache[cache_key] = (time.monotonic(), copy.deepcopy(payload))
    return payload


def invalidate_providers_cache() -> None:
    """Clear cached ``GET /api/providers`` responses."""
    with _providers_cache_lock:
        _providers_cache.clear()




def _active_provider_id() -> str | None:
    cfg = get_config()
    model_cfg = cfg.get("model", {})
    if not isinstance(model_cfg, dict):
        return None
    provider = str(model_cfg.get("provider") or "").strip().lower()
    return provider or None


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
        windows.append({
            "label": label,
            "used_percent": used_percent,
            "remaining_percent": remaining_percent,
            "reset_at": _isoformat_utc(getattr(window, "reset_at", None)),
            "detail": str(getattr(window, "detail", "") or "").strip() or None,
        })

    details = [
        str(detail).strip()
        for detail in (getattr(snapshot, "details", ()) or ())
        if str(detail).strip()
    ]
    plan = str(getattr(snapshot, "plan", "") or "").strip() or None
    unavailable_reason = str(getattr(snapshot, "unavailable_reason", "") or "").strip() or None
    result = {
        "provider": str(getattr(snapshot, "provider", "") or "").strip() or None,
        "source": str(getattr(snapshot, "source", "") or "").strip() or None,
        "title": str(getattr(snapshot, "title", "") or "").strip() or "Account limits",
        "plan": plan,
        "windows": windows,
        "details": details,
        "available": bool(getattr(snapshot, "available", bool(windows or details))) and not unavailable_reason,
        "unavailable_reason": unavailable_reason,
        "fetched_at": _isoformat_utc(getattr(snapshot, "fetched_at", None)),
    }
    pool = getattr(snapshot, "pool", None)
    if isinstance(pool, dict):
        result["pool"] = pool
    return result


def _agent_fetch_account_usage(provider: str, *, base_url: str | None = None, api_key: str | None = None) -> Any:
    from agent.account_usage import fetch_account_usage

    return fetch_account_usage(provider, base_url=base_url, api_key=api_key)


def _account_usage_subprocess_env(home: Path, provider: str, api_key: str | None) -> dict[str, str]:
    env = dict(os.environ)
    try:
        from api.config import _thread_ctx
    except Exception:
        _thread_ctx = None
    if bool(getattr(_thread_ctx, "block_process_env_fallback", False)):
        # Rely on the centralized profile scrub set (api.profiles), which unions
        # the WebUI provider env vars + the agent auth registry + the non-registry
        # agent credential fallback (CUSTOM_API_KEY, AWS/Bedrock family). Falling
        # back to the WebUI-only set keeps the probe fail-closed if that import
        # fails. (#3961 — don't leave a partial local AWS set here.)
        _strip = set(_PROVIDER_CREDENTIAL_ENV_VARS)
        try:
            from api.profiles import _profile_secret_env_names, get_active_hermes_home
            _strip.update(_profile_secret_env_names(get_active_hermes_home()))
        except Exception:
            _strip.update({"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"})
        for env_name in _strip:
            env.pop(env_name, None)
    env["HERMES_HOME"] = str(Path(home))

    # Profile .env values should affect only the child quota probe, not the
    # WebUI process-global environment. This is especially important for
    # Anthropic account usage, where the agent resolver reads OAuth/API tokens
    # from environment variables.
    for key, value in _load_env_file(Path(home) / ".env").items():
        if value:
            env[key] = value

    env_var = _provider_env_var_for((provider or "").strip().lower())
    if env_var and api_key:
        env[env_var] = api_key

    try:
        from api.config import _AGENT_DIR
    except Exception:
        _AGENT_DIR = None
    pythonpath_parts: list[str] = []
    if _AGENT_DIR:
        pythonpath_parts.append(str(_AGENT_DIR))
    existing_pythonpath = env.get("PYTHONPATH", "")
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    if pythonpath_parts:
        env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    return env


def _account_usage_payload_to_snapshot(payload: Any) -> Any:
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


class _AccountUsageProbeWorker:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.last_used = time.monotonic()
        self._lock = threading.RLock()
        self._proc: subprocess.Popen[str] | None = None
        self._closed = False

    def close(self) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
            self._closed = True
        self._close_process(proc)

    @staticmethod
    def _close_process(proc: subprocess.Popen[str] | None) -> None:
        if proc is None:
            return
        for stream_name in ("stdin", "stdout"):
            stream = getattr(proc, stream_name, None)
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except Exception:
                    proc.kill()
        except Exception:
            pass

    def fetch(self, provider: str, *, api_key: str | None = None) -> Any:
        if not self._lock.acquire(blocking=False):
            return _fetch_account_usage_once_for_home(provider, self.home, api_key=api_key)
        try:
            return self._fetch_locked(provider, api_key=api_key)
        finally:
            self._lock.release()

    def _fetch_locked(self, provider: str, *, api_key: str | None = None) -> Any:
        self.last_used = time.monotonic()
        proc = self._ensure_process(provider)
        if proc is None or proc.stdin is None or proc.stdout is None:
            return None

        request = json.dumps({
            "provider": provider,
            "api_key": api_key or "",
            "env_var": _provider_env_var_for((provider or "").strip().lower()),
        }) + "\n"
        result: dict[str, Any] = {}

        def round_trip() -> None:
            try:
                proc.stdin.write(request)
                proc.stdin.flush()
                result["line"] = proc.stdout.readline()
            except Exception as exc:
                result["error"] = exc

        thread = threading.Thread(target=round_trip, daemon=True)
        thread.start()
        thread.join(_ACCOUNT_USAGE_SUBPROCESS_TIMEOUT_SECONDS)
        self.last_used = time.monotonic()
        if thread.is_alive():
            self.close()
            thread.join(timeout=1.0)
            logger.debug("Account usage worker for %s timed out", provider)
            return None
        if result.get("error") is not None:
            exc = result["error"]
            self.close()
            logger.debug(
                "Account usage worker for %s failed",
                provider,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return None

        line = str(result.get("line") or "").strip()
        if not line:
            self.close()
            logger.debug("Account usage worker for %s exited before responding", provider)
            return None
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            self.close()
            logger.debug("Account usage worker for %s returned invalid JSON", provider)
            return None
        return _account_usage_payload_to_snapshot(payload)

    def _ensure_process(self, provider: str) -> subprocess.Popen[str] | None:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        old_proc = self._proc
        self._proc = None
        self._close_process(old_proc)
        try:
            from api.config import PYTHON_EXE
        except Exception:
            PYTHON_EXE = sys.executable or "python3"

        kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "text": True,
            "bufsize": 1,
        }
        if hasattr(os, "fork"):  # POSIX
            kwargs["preexec_fn"] = _account_usage_preexec_fn

        try:
            self._proc = subprocess.Popen(
                [
                    PYTHON_EXE,
                    "-c",
                    _ACCOUNT_USAGE_PARENT_DEATHSIG_BOOTSTRAP + _ACCOUNT_USAGE_SUBPROCESS_CODE,
                    "--worker",
                ],
                env=_account_usage_subprocess_env(self.home, provider, None),
                **kwargs,
            )
            self._closed = False
        except Exception:
            self._proc = None
            logger.debug("Account usage worker for %s failed to launch", provider, exc_info=True)
        return self._proc


def _launch_account_usage_worker_process(
    home: Path,
    provider: str,
    *,
    stdin: Any = subprocess.PIPE,
    stdout: Any = subprocess.PIPE,
) -> subprocess.Popen[str] | None:
    try:
        from api.config import PYTHON_EXE
    except Exception:
        PYTHON_EXE = sys.executable or "python3"

    kwargs: dict[str, Any] = {
        "stdin": stdin,
        "stdout": stdout,
        "stderr": subprocess.DEVNULL,
        "text": True,
        "bufsize": 1,
    }
    if hasattr(os, "fork"):  # POSIX
        kwargs["preexec_fn"] = _account_usage_preexec_fn

    try:
        return subprocess.Popen(
            [
                PYTHON_EXE,
                "-c",
                _ACCOUNT_USAGE_PARENT_DEATHSIG_BOOTSTRAP + _ACCOUNT_USAGE_SUBPROCESS_CODE,
                "--worker",
            ],
            env=_account_usage_subprocess_env(home, provider, None),
            **kwargs,
        )
    except Exception:
        logger.debug("Account usage worker for %s failed to launch", provider, exc_info=True)
        return None


def _fetch_account_usage_once_for_home(provider: str, home: Path, *, api_key: str | None = None) -> Any:
    proc = _launch_account_usage_worker_process(Path(home), provider)
    if proc is None or proc.stdin is None or proc.stdout is None:
        _AccountUsageProbeWorker._close_process(proc)
        return None
    request = json.dumps({
        "provider": provider,
        "api_key": api_key or "",
        "env_var": _provider_env_var_for((provider or "").strip().lower()),
    }) + "\n"
    try:
        stdout, _stderr = proc.communicate(request, timeout=_ACCOUNT_USAGE_SUBPROCESS_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _AccountUsageProbeWorker._close_process(proc)
        return None
    except Exception:
        _AccountUsageProbeWorker._close_process(proc)
        return None
    try:
        line = str(stdout or "").splitlines()[0]
        payload = json.loads(line.strip())
    except json.JSONDecodeError:
        return None
    except IndexError:
        return None
    return _account_usage_payload_to_snapshot(payload)


def _get_account_usage_probe_worker(home: Path) -> "_AccountUsageProbeWorker | None":
    """Return a worker with its lock already held, or None if saturated.

    The caller MUST release worker._lock after use (typically via try/finally).
    Holding the lock across the handoff eliminates the TOCTOU window that would
    let two concurrent probes both observe the same worker as free.
    """
    key = str(Path(home))
    stale: list[_AccountUsageProbeWorker] = []
    claimed: _AccountUsageProbeWorker | None = None
    with _account_usage_worker_pool_lock:
        existing_workers = _account_usage_worker_pool.get(key)
        workers: list[_AccountUsageProbeWorker]
        if not existing_workers:
            workers = [_AccountUsageProbeWorker(Path(home)) for _ in range(_ACCOUNT_USAGE_WORKERS_PER_HOME)]
        else:
            stale = [worker for worker in existing_workers if worker._closed]
            workers = [worker for worker in existing_workers if not worker._closed]
            while len(workers) < _ACCOUNT_USAGE_WORKERS_PER_HOME:
                workers.append(_AccountUsageProbeWorker(Path(home)))
        _account_usage_worker_pool[key] = workers
        for worker in workers:
            if worker._lock.acquire(blocking=False):
                claimed = worker
                break
    for worker in stale:
        worker.close()
    return claimed


def _cleanup_account_usage_probe_workers(
    *,
    now: float | None = None,
    idle_seconds: float = _ACCOUNT_USAGE_WORKER_IDLE_SECONDS,
) -> None:
    cutoff = time.monotonic() if now is None else now
    stale: list[tuple[str, _AccountUsageProbeWorker]] = []
    with _account_usage_worker_pool_lock:
        for key, workers in list(_account_usage_worker_pool.items()):
            for worker in workers:
                if worker._lock.acquire(blocking=False):
                    try:
                        proc = worker._proc
                        is_dead = worker._closed or (proc is not None and proc.poll() is not None)
                        if is_dead or cutoff - worker.last_used >= idle_seconds:
                            stale.append((key, worker))
                    finally:
                        worker._lock.release()
            remaining_workers = [w for w in workers if not any(k == key and w == sw for k, sw in stale)]
            if not remaining_workers:
                _account_usage_worker_pool.pop(key, None)
            else:
                # Replenish to N so partial cleanup doesn't permanently shrink the pool
                while len(remaining_workers) < _ACCOUNT_USAGE_WORKERS_PER_HOME:
                    remaining_workers.append(_AccountUsageProbeWorker(Path(key)))
                _account_usage_worker_pool[key] = remaining_workers
    for _key, worker in stale:
        worker.close()


def _close_account_usage_probe_workers() -> None:
    with _account_usage_worker_pool_lock:
        workers = [w for wlist in _account_usage_worker_pool.values() for w in wlist]
        _account_usage_worker_pool.clear()
    _close_account_usage_probe_worker_list(workers)


def _close_account_usage_probe_worker_list(workers: list[_AccountUsageProbeWorker]) -> None:
    for worker in workers:
        worker.close()


def _close_account_usage_probe_workers_async(*, provider_id: str | None = None) -> None:
    with _account_usage_worker_pool_lock:
        if provider_id:
            active_home = str(_get_hermes_home())
            workers_to_close = []
            for key, wlist in list(_account_usage_worker_pool.items()):
                if key == active_home:
                    workers_to_close.extend(wlist)
                    _account_usage_worker_pool.pop(key, None)
        else:
            workers_to_close = [w for wlist in _account_usage_worker_pool.values() for w in wlist]
            _account_usage_worker_pool.clear()
    if not workers_to_close:
        return
    thread = threading.Thread(
        target=_close_account_usage_probe_worker_list,
        args=(workers_to_close,),
        daemon=True,
        name="account-usage-worker-close",
    )
    thread.start()


atexit.register(_close_account_usage_probe_workers)


def _account_usage_cache_key(provider: str, home: Path, api_key: str | None) -> tuple[str, str, str]:
    key_fingerprint = ""
    if api_key:
        key_fingerprint = hashlib.sha256(api_key.encode("utf-8", "ignore")).hexdigest()
    return ((provider or "").strip().lower(), str(Path(home)), key_fingerprint)


def _get_cached_account_usage(cache_key: tuple[str, str, str]) -> tuple[bool, Any]:
    now = time.monotonic()
    with _account_usage_status_cache_lock:
        cached = _account_usage_status_cache.get(cache_key)
        if cached is None:
            return False, None
        fetched_at, snapshot = cached
        if now - fetched_at <= _ACCOUNT_USAGE_CACHE_TTL_SECONDS:
            return True, snapshot
        _account_usage_status_cache.pop(cache_key, None)
    return False, None


def invalidate_account_usage_status_cache(provider_id: str | None = None) -> None:
    normalized = str(provider_id or "").strip().lower()
    with _account_usage_status_cache_lock:
        if not normalized:
            _account_usage_status_cache.clear()
        else:
            for key in list(_account_usage_status_cache):
                if key[0] == normalized:
                    _account_usage_status_cache.pop(key, None)
    _close_account_usage_probe_workers_async(provider_id=normalized or None)


def _set_cached_account_usage(
    cache_key: tuple[str, str, str],
    snapshot: Any,
) -> None:
    now = time.monotonic()
    with _account_usage_status_cache_lock:
        if snapshot is None:
            cached = _account_usage_status_cache.get(cache_key)
            if cached is not None and cached[1] is not None:
                return
            _account_usage_status_cache.pop(cache_key, None)
            return
        _account_usage_status_cache[cache_key] = (now, snapshot)
        expired = [
            key for key, (fetched_at, _snapshot) in _account_usage_status_cache.items()
            if now - fetched_at > _ACCOUNT_USAGE_CACHE_TTL_SECONDS
        ]
        for key in expired:
            _account_usage_status_cache.pop(key, None)
        while len(_account_usage_status_cache) > _ACCOUNT_USAGE_CACHE_MAX_ENTRIES:
            oldest_key = min(
                _account_usage_status_cache,
                key=lambda key: _account_usage_status_cache[key][0],
            )
            _account_usage_status_cache.pop(oldest_key, None)


def _agent_fetch_account_usage_for_home(provider: str, home: Path, *, api_key: str | None = None) -> Any:
    try:
        _cleanup_account_usage_probe_workers()
        worker = _get_account_usage_probe_worker(home)
        if worker is not None:
            try:
                return worker._fetch_locked(provider, api_key=api_key)
            finally:
                worker._lock.release()
        return _fetch_account_usage_once_for_home(provider, home, api_key=api_key)
    except Exception:
        logger.debug("Account usage probe for %s failed", provider, exc_info=True)
        return None


def _fetch_account_usage_with_profile_context(provider: str, *, refresh: bool = False) -> Any:
    """Fetch account usage for a provider within the active profile context.

    Concurrency is capped by the module-level BoundedSemaphore so that rapid
    UI polls (e.g. Settings page refresh) cannot exhaust file-descriptors or
    memory by spawning more than _MAX_CONCURRENT_ACCOUNT_USAGE_PROBES probe
    subprocesses simultaneously.  Each probe runs up to 35 s.

    Warm per-profile worker processes handle the actual probe requests.
    """
    home = _get_hermes_home()
    api_key = _get_provider_api_key(provider)
    cache_key = _account_usage_cache_key(provider, home, api_key)
    if not refresh:
        cache_hit, cached = _get_cached_account_usage(cache_key)
        if cache_hit:
            return cached
    sem = _get_account_usage_probe_semaphore()
    try:
        with sem:
            snapshot = _agent_fetch_account_usage_for_home(
                provider,
                home,
                api_key=api_key,
            )
            _set_cached_account_usage(cache_key, snapshot)
            return snapshot
    except Exception:
        logger.debug("Failed to fetch account usage for %s", provider, exc_info=True)
        _set_cached_account_usage(cache_key, None)
        return None


def _provider_account_usage_status(provider: str, display_name: str, *, refresh: bool = False) -> dict[str, Any]:
    snapshot = _fetch_account_usage_with_profile_context(provider, refresh=refresh)
    account_limits = _serialize_account_usage_snapshot(snapshot)
    if account_limits and account_limits.get("available"):
        return {
            "ok": True,
            "provider": provider,
            "display_name": display_name,
            "supported": True,
            "status": "available",
            "label": account_limits.get("title") or "Account limits",
            "quota": None,
            "account_limits": account_limits,
            "message": f"{display_name} account limits loaded.",
        }

    reason = ""
    if account_limits:
        reason = str(account_limits.get("unavailable_reason") or "").strip()
    message = (
        f"{display_name} account limits are unavailable. {reason}"
        if reason
        else f"{display_name} account limits are unavailable. Confirm provider authentication and try again."
    )
    return {
        "ok": False,
        "provider": provider,
        "display_name": display_name,
        "supported": True,
        "status": "unavailable",
        "quota": None,
        "account_limits": account_limits,
        "message": message,
    }


def get_provider_quota(provider_id: str | None = None, *, refresh: bool = False) -> dict[str, Any]:
    """Return sanitized quota/rate-limit status for the active provider.

    OpenRouter keeps its documented key endpoint. OAuth-backed account usage
    providers reuse Hermes Agent's /usage account-limits abstraction so WebUI
    stays aligned with CLI/Gateway provider semantics.
    """
    provider = (provider_id or _active_provider_id() or "").strip().lower()
    if not provider:
        return {
            "ok": False,
            "provider": None,
            "display_name": None,
            "supported": False,
            "status": "unavailable",
            "quota": None,
            "message": "No active provider is configured.",
        }

    display_name = _PROVIDER_DISPLAY.get(provider, provider.replace("-", " ").title())
    if provider in _ACCOUNT_USAGE_PROVIDERS:
        return _provider_account_usage_status(provider, display_name, refresh=refresh)

    if provider == "openrouter":
        api_key = _get_provider_api_key("openrouter")
        if not api_key:
            return {
                "ok": False,
                "provider": "openrouter",
                "display_name": display_name,
                "supported": True,
                "status": "no_key",
                "quota": None,
                "message": "OpenRouter quota status needs an OPENROUTER_API_KEY configured on the server.",
            }
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
            payload = json.loads(raw.decode("utf-8")) if isinstance(raw, (bytes, bytearray)) else json.loads(raw)
            quota = _sanitize_openrouter_quota(payload)
            return {
                "ok": True,
                "provider": "openrouter",
                "display_name": display_name,
                "supported": True,
                "status": "available",
                "label": "OpenRouter credits",
                "quota": quota,
                "message": "OpenRouter quota status loaded.",
            }
        except urllib.error.HTTPError as exc:
            status = "invalid_key" if exc.code in (401, 403) else "unavailable"
            message = (
                "OpenRouter rejected the configured API key."
                if status == "invalid_key"
                else "OpenRouter quota status is temporarily unavailable."
            )
            return {
                "ok": False,
                "provider": "openrouter",
                "display_name": display_name,
                "supported": True,
                "status": status,
                "quota": None,
                "message": message,
            }
        except (TimeoutError, urllib.error.URLError, json.JSONDecodeError, OSError, ValueError):
            return {
                "ok": False,
                "provider": "openrouter",
                "display_name": display_name,
                "supported": True,
                "status": "unavailable",
                "quota": None,
                "message": "OpenRouter quota status is temporarily unavailable.",
            }

    local_snapshot = _local_pool_snapshot(provider)
    if local_snapshot is not None:
        account_limits = _serialize_account_usage_snapshot(local_snapshot)
        if account_limits and account_limits.get("available"):
            return {
                "ok": True,
                "provider": provider,
                "display_name": display_name,
                "supported": True,
                "status": "available",
                "label": account_limits.get("title") or "Credential pool",
                "quota": None,
                "account_limits": account_limits,
                "message": f"{display_name} credential pool status loaded.",
            }
        return {
            "ok": False,
            "provider": provider,
            "display_name": display_name,
            "supported": True,
            "status": "unavailable",
            "quota": None,
            "account_limits": account_limits,
            "message": f"{display_name} credential pool: all credentials are unavailable.",
        }

    detail = "OpenAI/Anthropic rate-limit headers are a follow-up once WebUI captures provider response metadata."
    return {
        "ok": False,
        "provider": provider,
        "display_name": display_name,
        "supported": False,
        "status": "unsupported",
        "quota": None,
        "message": f"Quota status is not available for {display_name}. {detail}",
    }






# SECTION: Public API


def get_providers() -> dict[str, Any]:
    """Return a list of all known providers with their configuration status.

    Each entry contains:
    - ``id``: canonical provider slug
    - ``display_name``: human-readable name
    - ``has_key``: whether an API key is configured
    - ``configurable``: whether the key can be set from the WebUI
    - ``key_source``: where the key was found (``env_file``, ``env_var``,
      ``config_yaml``, ``oauth``, ``none``)
    - ``models``: list of known model IDs for this provider
    """
    # Collect all known provider IDs from multiple sources
    known_ids = set(_PROVIDER_DISPLAY.keys()) | set(_PROVIDER_MODELS.keys())
    known_ids.update(plugin_model_provider_ids())

    # Also detect providers from config.yaml providers section
    cfg = get_config()
    cache_key = _providers_cache_key(cfg)
    cached = _get_cached_providers(cache_key)
    if cached is not None:
        return cached

    providers = []
    providers_cfg = cfg.get("providers") or {}
    if isinstance(providers_cfg, dict):
        known_ids.update(providers_cfg.keys())

    # Add OAuth providers even if not in _PROVIDER_DISPLAY
    known_ids.update(_OAUTH_PROVIDERS)

    for pid in sorted(known_ids):
        display_name = effective_provider_display_name(pid, _PROVIDER_DISPLAY)
        is_oauth = _provider_is_oauth(pid)
        has_key = _provider_has_key(pid)
        plugin_auth_status: dict[str, Any] | None = None
        if not has_key and is_plugin_model_provider(pid):
            try:
                from hermes_cli.auth import get_auth_status as _gas_plugin
                _plugin_status = _gas_plugin(pid)
                if isinstance(_plugin_status, dict) and (
                    _plugin_status.get("logged_in") or _plugin_status.get("configured")
                ):
                    has_key = True
                    plugin_auth_status = _plugin_status
            except Exception:
                logger.debug("Plugin provider auth check failed for %s", pid, exc_info=True)

        # Determine key source
        key_source = "none"
        auth_error = None
        if is_oauth:
            key_source = "oauth"
            # Check if actually authenticated via hermes_cli.
            # IMPORTANT: do not unconditionally overwrite has_key from _provider_has_key().
            # A token in config.yaml is a valid credential even when get_auth_status()
            # returns logged_in=False (e.g. token not in the hermes credential pool,
            # or refresh token consumed by native Codex CLI / VS Code extension).
            try:
                from hermes_cli.auth import get_auth_status as _gas
                status = _gas(pid)
                if isinstance(status, dict) and status.get("logged_in"):
                    has_key = True
                    key_source = status.get("key_source", "oauth")
                elif has_key:
                    # _provider_has_key() found a token in config.yaml — respect it
                    # rather than hiding a working credential from the Settings UI.
                    key_source = "config_yaml"
                    auth_error = status.get("error") if isinstance(status, dict) else None
                else:
                    has_key = False
                    auth_error = status.get("error") if isinstance(status, dict) else None
            except Exception:
                # Import failed or auth check errored — don't override a known-good
                # key just because the hermes_cli auth module is unavailable.
                logger.debug("hermes_cli auth check failed for %s", pid, exc_info=True)
                # keep has_key from _provider_has_key()
        elif has_key:
            env_var = _provider_env_var_for(pid)
            if env_var:
                env_path = _get_hermes_home() / ".env"
                env_values = _load_env_file(env_path)
                if _provider_value_counts_as_api_key(pid, env_values.get(env_var)):
                    key_source = "env_file"
                elif _provider_value_counts_as_api_key(pid, _thread_local_env_value(env_var)):
                    key_source = "env_var"
                else:
                    # Canonical name not set; check legacy aliases (e.g. lmstudio's
                    # pre-#1500 LMSTUDIO_API_KEY) so existing users see "env_file"
                    # instead of being misreported as "config_yaml" when the key
                    # actually lives in .env under the old name.
                    aliased = False
                    for alias in _PROVIDER_ENV_VAR_ALIASES.get(pid, ()) or ():
                        if _provider_value_counts_as_api_key(pid, env_values.get(alias)):
                            key_source = "env_file"
                            aliased = True
                            break
                        if _provider_value_counts_as_api_key(pid, _thread_local_env_value(alias)):
                            key_source = "env_var"
                            aliased = True
                            break
                    if not aliased:
                        _plugin_ks = (
                            str(plugin_auth_status.get("key_source") or "").strip()
                            if isinstance(plugin_auth_status, dict)
                            else ""
                        )
                        key_source = _plugin_ks or "config_yaml"
            else:
                _plugin_ks = (
                    str(plugin_auth_status.get("key_source") or "").strip()
                    if isinstance(plugin_auth_status, dict)
                    else ""
                )
                key_source = _plugin_ks or "config_yaml"
        elif not _provider_env_var_for(pid):
            # Fallback: provider is not a known API-key provider and not in
            # the hardcoded _OAUTH_PROVIDERS set.  It may be a custom or
            # newly-added OAuth provider (e.g. Anthropic connected via OAuth).
            # Check live auth status so the Providers tab agrees with the
            # model picker (#1212).
            #
            # IMPORTANT: we skip providers with a known API-key env var because
            # they are pure API-key providers — calling get_auth_status() for
            # every unconfigured API-key provider would add unnecessary latency
            # (network round-trip per provider) on the Settings page.
            # Validate pid looks like a real provider before probing
            import re as _re
            if _re.match(r'^[a-z][a-z0-9_-]{0,63}$', pid):
                try:
                    from hermes_cli.auth import get_auth_status as _gas
                    status = _gas(pid)
                    if isinstance(status, dict) and status.get("logged_in"):
                        has_key = True
                        # Constrain key_source to a known-safe closed set
                        _raw_ks = status.get("key_source", "")
                        key_source = _raw_ks if _raw_ks in {"oauth", "env", "config", "token"} else "oauth"
                        is_oauth = True
                except Exception:
                    pass

        if pid == "openai" and not has_key and _provider_has_shadowed_codex_oauth_value(pid):
            continue

        models = list(_PROVIDER_MODELS.get(pid, []))
        models_total = len(models)
        # OpenAI Codex account catalogs drift independently from WebUI releases.
        # The model picker already prefers hermes_cli + Codex local cache for
        # this provider (the agent's `provider_model_ids("openai-codex")` filters
        # IDs with `supported_in_api: false`, but Codex CLI still surfaces some
        # of those — notably `gpt-5.3-codex-spark` from #1680 — in its picker).
        # Merge both sources here so the providers card matches the picker
        # exactly. Static entries remain the offline fallback when live
        # discovery and the local Codex cache are both unavailable. (#1807
        # follow-up to v0.51.19 #1812.)
        if pid == "openai-codex":
            live_ids = _read_live_provider_model_ids("openai-codex")
            live_id_set = set(live_ids)
            for mid in _read_visible_codex_cache_model_ids():
                if mid not in live_id_set:
                    live_id_set.add(mid)
                    live_ids.append(mid)
            live_models = _models_from_live_provider_ids(pid, live_ids)
            if live_models:
                models = live_models
                models_total = len(models)
        if pid == "xai-oauth":
            live_models = _models_from_live_provider_ids(
                pid,
                _read_live_provider_model_ids("xai-oauth"),
            )
            if live_models:
                models = live_models
                models_total = len(models)
        # Nous Portal: prefer the live catalog so the providers card matches
        # the dropdown picker (#1538). Same fallback shape as the static-only
        # case below — when hermes_cli is unavailable or its lookup raises,
        # we keep the four-entry curated list.
        #
        # On large-tier accounts (#1567 reporter Deor saw 396 entries), we
        # render the same featured subset the picker uses so the providers
        # card body doesn't become a 396-pill wall. The full count is still
        # reported via models_total — surfaced in the header line as
        # "396 models · OAuth" by static/panels.js — so the user knows the
        # complete catalog is reachable (via /model autocomplete or a future
        # "show all" disclosure if added).
        if pid == "nous":
            try:
                from hermes_cli.models import provider_model_ids as _provider_model_ids

                live_ids = _provider_model_ids("nous") or []
                if live_ids:
                    # Lazy-import to avoid circular dep with api.config.
                    from api.config import _format_nous_label, _build_nous_featured_set

                    featured_ids, _extras = _build_nous_featured_set(live_ids)
                    models = [
                        {"id": f"@nous:{mid}", "label": _format_nous_label(mid)}
                        for mid in featured_ids
                    ]
                    models_total = len(live_ids)
            except Exception:
                logger.debug("Failed to load Nous Portal models from hermes_cli")
        # LM Studio: fetch live locally-loaded models so the providers card
        # matches what's actually available on the user's server (#WebUI).
        if pid == "lmstudio":
            try:
                from hermes_cli.models import provider_model_ids as _pmi

                lm_live = _pmi("lmstudio") or []
                if lm_live:
                    models = [{"id": mid, "label": mid} for mid in lm_live]
                    models_total = len(models)
            except Exception:
                logger.debug("Failed to load LM Studio models from hermes_cli")
        if is_plugin_model_provider(pid):
            try:
                live_models = _models_from_live_provider_ids(
                    pid,
                    _read_live_provider_model_ids(pid),
                )
                if live_models:
                    models = live_models
                    models_total = len(models)
            except Exception:
                logger.debug(
                    "Failed to load plugin model-provider catalog for %s",
                    pid,
                    exc_info=True,
                )
        # Also include models from config.yaml providers section
        if isinstance(providers_cfg, dict):
            provider_cfg = providers_cfg.get(pid, {})
            if isinstance(provider_cfg, dict) and "models" in provider_cfg:
                cfg_models = provider_cfg["models"]
                if isinstance(cfg_models, dict):
                    models = models + [{"id": k, "label": k} for k in cfg_models.keys()]
                elif isinstance(cfg_models, list):
                    models = models + [{"id": k, "label": k} for k in cfg_models]
                # Recompute models_total when config.yaml contributes additional
                # entries on top of the live/static catalog. For non-Nous
                # providers models_total still equals len(models); for Nous
                # we keep the live count (which already includes any models
                # surfaced in the curated featured slice).
                if pid != "nous":
                    models_total = len(models)

        is_self_hosted = pid in _SELF_HOSTED_PROVIDER_IDS
        try:
            from api.config import _get_provider_base_url
            provider_base_url = _get_provider_base_url(pid) if is_self_hosted else None
        except Exception:
            provider_base_url = None
        _is_plugin = is_plugin_model_provider(pid)
        providers.append({
            "id": pid,
            "display_name": display_name,
            "has_key": has_key,
            "configurable": not is_oauth and bool(_provider_env_var_for(pid)),
            "is_self_hosted": is_self_hosted,
            "base_url": provider_base_url,
            "is_plugin_provider": _is_plugin,
            "is_oauth": is_oauth,
            "key_source": key_source,
            "auth_error": auth_error,
            "models": models,
            # models_total reflects the complete catalog size (e.g. 396 for
            # an enterprise Nous Portal account), even when "models" is
            # trimmed to a featured subset for UI scannability. The frontend
            # uses this for the header text "396 models · OAuth" so users
            # know the full catalog exists and is reachable via the slash
            # command. For providers that don't trim, models_total ==
            # len(models) and the frontend behaves identically to before.
            "models_total": models_total,
        })

    # Scan custom_providers from config.yaml (e.g. glmcode, timicc)
    custom_providers_cfg = cfg.get("custom_providers", [])
    if isinstance(custom_providers_cfg, list):
        for cp in custom_providers_cfg:
            if not isinstance(cp, dict) or not cp.get("name"):
                continue
            cp_name = str(cp["name"]).strip()
            cp_id = _custom_provider_slug_from_name(cp_name)
            if not cp_id:
                logger.warning(
                    "Custom provider entry %r produced empty slug; skipping",
                    cp_name,
                )
                continue
            # Collect models from `models` list or `model` single
            cp_models = []
            if isinstance(cp.get("models"), list):
                cp_models = [{"id": str(m), "label": str(m)} for m in cp["models"]]
            elif cp.get("model"):
                cp_models = [{"id": cp["model"], "label": cp["model"]}]
            # Check for env var reference (${VAR_NAME} pattern)
            cp_api_key = str(cp.get("api_key") or "")
            cp_has_key = bool(cp_api_key.strip())
            # Replace env var reference to check actual value
            if cp_api_key.startswith("${") and cp_api_key.endswith("}"):
                env_var = cp_api_key[2:-1]
                cp_has_key = bool(_thread_local_env_value(env_var).strip())
            # Fallback: check credential pool (key added via hermes auth add)
            if not cp_has_key:
                try:
                    from api.config import _has_explicit_pool_credentials
                    if _has_explicit_pool_credentials(cp_id):
                        cp_has_key = True
                except ImportError:
                    pass
            providers.append({
                "id": cp_id,
                "display_name": cp_name,
                "has_key": cp_has_key,
                "configurable": False,  # custom providers managed via config.yaml
                "is_custom": True,
                "key_source": "config_yaml" if cp_has_key else "none",
                "models": cp_models,
                "models_total": len(cp_models),
            })

    # Determine active provider
    active_provider = None
    model_cfg = cfg.get("model", {})
    if isinstance(model_cfg, dict):
        active_provider = model_cfg.get("provider")

    # Sort providers: active first, then custom:*, then has_key, then rest.
    def _provider_sort_key(p):
        pid = p.get("id") or ""
        if pid == active_provider:
            return (0, pid)
        if pid.startswith("custom:"):
            return (1, pid)
        if p.get("has_key"):
            return (2, pid)
        return (3, pid)
    providers.sort(key=_provider_sort_key)

    result = {
        "providers": providers,
        "active_provider": active_provider,
    }
    return _store_cached_providers(cache_key, result)


def set_provider_key(provider_id: str, api_key: str | None) -> dict[str, Any]:
    """Set or update the API key for a provider.

    Writes the key to ``~/.hermes/.env`` using the standard env var name.
    If ``api_key`` is None or empty, the key is removed.

    Returns a status dict with the operation result.
    """
    provider_id = provider_id.strip().lower()

    if not provider_id:
        return {"ok": False, "error": "Provider ID is required."}

    if _provider_is_oauth(provider_id):
        return {
            "ok": False,
            "error": f"'{_PROVIDER_DISPLAY.get(provider_id, provider_id)}' uses OAuth authentication. "
                     f"Use `hermes model` in the terminal to configure it.",
        }

    env_var = _provider_env_var_for(provider_id)
    if not env_var:
        return {
            "ok": False,
            "error": f"Cannot configure API key for '{effective_provider_display_name(provider_id, _PROVIDER_DISPLAY)}'. "
                     f"This provider does not have a known env var mapping.",
        }

    # Validate API key format (basic sanity check)
    if api_key:
        api_key = api_key.strip()
        if "\n" in api_key or "\r" in api_key:
            return {"ok": False, "error": "API key must not contain newline characters."}
        if len(api_key) < 8:
            return {"ok": False, "error": "API key appears too short."}

    env_path = _get_hermes_home() / ".env"
    try:
        _write_env_file(env_path, {env_var: api_key})
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.exception("Failed to write env file for provider %s", provider_id)
        return {"ok": False, "error": f"Failed to save API key: {exc}"}

    # Invalidate the model cache so the dropdown refreshes on next request.
    # Using invalidate_models_cache() instead of reload_config() to avoid
    # disrupting active streaming sessions that may be reading config.cfg.
    invalidate_models_cache()
    invalidate_account_usage_status_cache(provider_id)
    invalidate_providers_cache()

    return {
        "ok": True,
        "provider": provider_id,
        "display_name": _PROVIDER_DISPLAY.get(provider_id, provider_id),
        "action": "updated" if api_key else "removed",
    }


def remove_provider_key(provider_id: str) -> dict[str, Any]:
    """Remove the API key for a provider.

    Removes the key from ``~/.hermes/.env`` (via ``set_provider_key``)
    and also cleans up ``config.yaml`` if the key is stored there
    (``providers.<id>.api_key`` or top-level ``model.api_key`` when this
    provider is the active one).

    Returns a status dict with the operation result.
    """
    result = set_provider_key(provider_id, None)

    # Even if the .env removal succeeded, the key might also live in
    # config.yaml (e.g. providers.<id>.api_key or model.api_key).
    # Clean those up so _provider_has_key() returns False after removal.
    if result.get("ok"):
        _clean_provider_key_from_config(provider_id)

    return result


def _clean_provider_key_from_config(provider_id: str) -> None:
    """Remove provider API key entries from config.yaml.

    Handles three storage locations:
    1. ``providers.<id>.api_key`` — per-provider key
    2. ``model.api_key`` — top-level key (only if provider is active)
    3. ``custom_providers[].api_key`` — custom provider entries

    Writes back to config.yaml only if something was actually removed.
    Uses ``_cfg_lock`` to prevent TOCTOU races.
    """
    from api.config import _cfg_lock

    try:
        # Resolve through api.config at call time instead of the function imported
        # at module load. Several tests (and some profile flows) monkeypatch the
        # config module's path resolver after api.providers has already been
        # imported; using the stale imported reference can clean the wrong
        # config.yaml.
        import api.config as _config
        config_path = _config._get_config_path()
    except Exception:
        return

    if not config_path.exists():
        return

    try:
        import yaml as _yaml

        changed = False

        with _cfg_lock:
            raw = config_path.read_text(encoding="utf-8")
            cfg = _yaml.safe_load(raw)
            if not isinstance(cfg, dict):
                return

            # 1. Clean providers.<id>.api_key
            providers_cfg = cfg.get("providers") or {}
            if isinstance(providers_cfg, dict):
                provider_cfg = providers_cfg.get(provider_id, {})
                if isinstance(provider_cfg, dict) and provider_cfg.get("api_key"):
                    del provider_cfg["api_key"]
                    changed = True

            # 2. Clean model.api_key — only if this provider is the active one
            model_cfg = cfg.get("model", {})
            if isinstance(model_cfg, dict) and model_cfg.get("api_key"):
                active_provider = model_cfg.get("provider")
                if active_provider and str(active_provider).strip().lower() == provider_id.lower():
                    del model_cfg["api_key"]
                    changed = True

            # 3. Clean custom_providers[].api_key
            custom_providers = cfg.get("custom_providers", [])
            if isinstance(custom_providers, list):
                for cp in custom_providers:
                    if isinstance(cp, dict):
                        if _custom_provider_name_matches(provider_id, cp.get("name")):
                            if cp.get("api_key"):
                                del cp["api_key"]
                                changed = True

            if changed:
                _save_yaml_config_file(config_path, cfg)
        # Sync in-memory cache and bust model TTL cache
        # MUST be called outside _cfg_lock to avoid deadlock:
        # _cfg_lock is a threading.Lock (non-reentrant) and
        # reload_config() also acquires _cfg_lock internally.
        if changed:
            reload_config()
            invalidate_providers_cache()
    except Exception:
        logger.exception("Failed to clean provider key from config.yaml for %s", provider_id)
