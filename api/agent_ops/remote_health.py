"""Authenticated remote Gateway health probe with bounded single-flight caching."""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from .gateway_status import _checked_at

# Remote-gateway probe (#3281)
# ------------------------------------------------------------------
# In multi-container Docker deployments the WebUI container does not ship the
# ``gateway`` Python package. The lazy ``importlib.import_module("gateway.status")``
# therefore raises ``ModuleNotFoundError`` and the payload falls through to
# ``gateway_not_configured`` even though ``HERMES_API_URL`` points at a perfectly
# reachable remote gateway. The Tasks/Cron banner then shows a spurious amber
# "Gateway not configured" warning.
#
# When a gateway base URL is set in any supported env var, we treat that as an
# explicit declaration that the gateway lives elsewhere, and probe it over HTTP
# before touching any local filesystem / module signal. The probe result is
# cached briefly so a dashboard rerender that fans out to multiple panels does
# not hammer the gateway.

_REMOTE_PROBE_TIMEOUT_S: float = 2.0
_REMOTE_PROBE_CACHE_TTL_S: float = 5.0
_REMOTE_PROBE_PATHS: tuple[str, ...] = ("/health/detailed", "/health", "/v1/health")
# A gateway health payload is small JSON; cap the 2xx body read so a large or
# slow-trickled remote response can't hang /api/health/agent or balloon memory.
_REMOTE_PROBE_BODY_LIMIT_BYTES: int = 64 * 1024

_remote_probe_lock = threading.Lock()
# Condition wraps the same lock so cache reads/writes and single-flight waits
# share one mutex (mirrors the Condition(_lock) idiom in api/session_lifecycle).
_remote_probe_cond = threading.Condition(_remote_probe_lock)
_remote_probe_cache: dict[str, Any] = {"url": None, "expires_at": 0.0, "result": None}
# base_urls currently being probed by a "leader" thread. Latecomers wait on the
# Condition for the leader's result instead of stampeding the (possibly dead)
# gateway themselves (#5455 dashboard fan-out, #2476).
_remote_probe_inflight: set[str] = set()


def _remote_probe_wait_budget_s() -> float:
    """How long a latecomer waits for the leader before giving up and self-probing.

    The leader can walk every path (each up to ``_REMOTE_PROBE_TIMEOUT_S``), so
    budget for the full walk plus a small margin; timing out is a safety valve
    against a hung leader, never the normal path. Computed on each call (not a
    module-level constant) so a test that monkeypatches the timeout or the path
    list gets a budget consistent with those values instead of a stale one.
    """
    return _REMOTE_PROBE_TIMEOUT_S * len(_REMOTE_PROBE_PATHS) + 1.0


def _remote_gateway_base_url() -> str | None:
    """Return an explicit remote gateway base URL, or None for local-only setups.

    Priority: GATEWAY_HEALTH_URL > HERMES_GATEWAY_HEALTH_URL > HERMES_API_URL
    > HERMES_WEBUI_GATEWAY_BASE_URL.
    Returns ``None`` when no env var is set so the caller falls through to
    local PID/state checks.

    Any of these env vars may legitimately point AT a health endpoint
    (e.g. ``GATEWAY_HEALTH_URL=http://host:8642/health``). Since the probe
    appends ``/health/detailed`` etc. to the returned base, strip a trailing
    health-path suffix first so we don't build ``/health/health/detailed``
    (mirrors the normalization in api/updates.py).
    """
    for var in (
        "GATEWAY_HEALTH_URL",
        "HERMES_GATEWAY_HEALTH_URL",
        "HERMES_API_URL",
        "HERMES_WEBUI_GATEWAY_BASE_URL",
    ):
        val = os.environ.get(var, "").strip()
        if val:
            base = val.rstrip("/")
            for suffix in ("/health/detailed", "/health", "/v1/health", "/status"):
                if base.endswith(suffix):
                    base = base[: -len(suffix)].rstrip("/")
                    break
            return base
    return None


def _remote_gateway_api_key() -> str:
    """Return the Bearer token for authenticated gateway health probes.

    Mirrors ``api.gateway_chat._gateway_api_key``: WebUI containers in
    multi-service deployments must present the same key the agent's API server
    expects on ``/health/detailed`` (#5418).
    """
    return str(
        os.environ.get("HERMES_WEBUI_GATEWAY_API_KEY")
        or os.environ.get("API_SERVER_KEY")
        or ""
    ).strip()


def _http_probe(
    url: str,
    timeout_s: float,
    *,
    api_key: str | None = None,
) -> tuple[bool, int | None, str | None, bytes | None]:
    """GET ``url`` and return (ok, status_code, error_name, body).

    ``ok`` is True only for a 2xx response. 5xx and network errors are not OK.
    4xx is also treated as "responded" (the gateway is up, just answering 404
    on this particular path) so the caller can move on to the next path.
    ``body`` is the raw response bytes for 2xx responses, None otherwise.
    """
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib_request.Request(url, method="GET", headers=headers)
    try:
        with urllib_request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - trusted env var URL
            status = getattr(resp, "status", None) or resp.getcode()
            ok = 200 <= int(status) < 300
            # Cap the body read: we only need a small JSON health payload, and an
            # unbounded resp.read() on a large/trickled 2xx body could hang the
            # /api/health/agent handler or balloon memory. Read one byte over the
            # cap so the caller can detect (and skip) an oversized body.
            body = resp.read(_REMOTE_PROBE_BODY_LIMIT_BYTES + 1) if ok else None
            return (ok, int(status), None, body)
    except urllib_error.HTTPError as exc:
        return (False, int(exc.code), "HTTPError", None)
    except Exception as exc:  # urllib_error.URLError, socket.timeout, ssl, etc.
        return (False, None, type(exc).__name__, None)


def _cached_remote_result_locked(base_url: str, current: float) -> dict[str, Any] | None:
    """Return a fresh cached probe result for *base_url*, or None if stale/absent.

    Must be called while holding ``_remote_probe_cond``.
    """
    if (
        _remote_probe_cache.get("url") == base_url
        and _remote_probe_cache.get("expires_at", 0.0) > current
        and _remote_probe_cache.get("result") is not None
    ):
        return _remote_probe_cache["result"]
    return None


def _run_remote_probe(base_url: str) -> dict[str, Any]:
    """Walk the remote gateway health paths and build a payload (no caching/locking).

    This is the expensive, network-bound step: each path can block up to
    ``_REMOTE_PROBE_TIMEOUT_S``. It runs OUTSIDE the probe lock so a single
    "leader" thread does it while latecomers wait for the cached result.
    """
    last_status: int | None = None
    last_error: str | None = None
    gateway_api_key = _remote_gateway_api_key()
    for path in _REMOTE_PROBE_PATHS:
        probe_key = gateway_api_key if path == "/health/detailed" else None
        ok, status, err, body = _http_probe(
            base_url + path,
            _REMOTE_PROBE_TIMEOUT_S,
            api_key=probe_key,
        )
        if ok:
            details: dict[str, Any] = {
                "state": "alive",
                "reason": "remote_gateway",
                "endpoint": base_url + path,
                "status_code": status,
            }
            if body and len(body) <= _REMOTE_PROBE_BODY_LIMIT_BYTES:
                try:
                    data = json.loads(body)
                    if isinstance(data, dict) and "gateway_state" in data:
                        details["gateway_state"] = data["gateway_state"]
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
            # An over-cap body (len > limit, i.e. the +1 sentinel byte was read)
            # is treated as "alive but no parseable gateway_state" — we still
            # report the gateway as up, just without the detailed state.
            return {
                "alive": True,
                "checked_at": _checked_at(),
                "details": details,
            }
        # Remember the most informative failure signal we saw.
        if status is not None:
            last_status = status
        if err is not None:
            last_error = err

    details = {
        "state": "down",
        "reason": "remote_gateway_unreachable",
        "endpoint": base_url,
    }
    if last_status is not None:
        details["status_code"] = last_status
    if last_error is not None:
        details["error"] = last_error
    return {
        "alive": False,
        "checked_at": _checked_at(),
        "details": details,
    }


def _probe_remote_gateway(base_url: str, *, now: float | None = None) -> dict[str, Any]:
    """Return an agent-health payload dict for a remote gateway base URL.

    Result is cached for ``_REMOTE_PROBE_CACHE_TTL_S`` seconds per base_url.

    Concurrency (single-flight): when several dashboard panels fan out on a cold
    cache, only the first "leader" thread runs the ~2s-per-path network probe;
    latecomers wait on ``_remote_probe_cond`` for the leader's cached result
    rather than each hammering the (possibly dead) gateway (#5455, #2476). The
    leader always clears the in-flight marker and wakes waiters — even on error —
    so waiters can never deadlock.
    """
    current = time.monotonic() if now is None else now
    with _remote_probe_cond:
        cached = _cached_remote_result_locked(base_url, current)
        if cached is not None:
            # Refresh checked_at so the UI shows a current timestamp without
            # actually re-hitting the gateway.
            return {**cached, "checked_at": _checked_at()}

        # A leader is already probing this base_url: wait for its result instead
        # of starting a duplicate probe.
        if base_url in _remote_probe_inflight:
            deadline = current + _remote_probe_wait_budget_s()
            while base_url in _remote_probe_inflight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break  # leader stuck; fall through and probe ourselves
                _remote_probe_cond.wait(remaining)
            cached = _cached_remote_result_locked(base_url, time.monotonic())
            if cached is not None:
                return {**cached, "checked_at": _checked_at()}
            # Woke without a usable cached result (leader failed, produced no
            # cacheable result, or we timed out): become a leader ourselves.

        # Become the leader for this base_url.
        _remote_probe_inflight.add(base_url)

    payload: dict[str, Any] | None = None
    try:
        payload = _run_remote_probe(base_url)
    finally:
        with _remote_probe_cond:
            if payload is not None:
                _remote_probe_cache["url"] = base_url
                # Expire from the moment the probe COMPLETES, not from the
                # leader's entry time: walking every path of a hung gateway
                # takes len(paths) * timeout (~6s) which exceeds the 5s TTL, so
                # `current + TTL` would write an already-expired cache line.
                # Waiters woken right after this would then miss the cache and
                # each re-probe the dead gateway — collapsing single-flight and
                # regressing latency to worse-than-serial (#5455, #2476).
                _remote_probe_cache["expires_at"] = (
                    time.monotonic() + _REMOTE_PROBE_CACHE_TTL_S
                )
                _remote_probe_cache["result"] = payload
            # Always release the in-flight marker and wake waiters, even if the
            # probe raised — otherwise latecomers would wait out the full budget.
            _remote_probe_inflight.discard(base_url)
            _remote_probe_cond.notify_all()
    # Only reached when _run_remote_probe returned normally (a raise would have
    # propagated through the finally), so payload is always a dict here. The
    # assert makes that explicit for the type checker (declared -> dict[str, Any]).
    assert payload is not None
    return payload


def _reset_remote_probe_cache_for_tests() -> None:
    """Test hook: clear the in-process remote-probe cache and in-flight state."""
    with _remote_probe_cond:
        _remote_probe_cache["url"] = None
        _remote_probe_cache["expires_at"] = 0.0
        _remote_probe_cache["result"] = None
        _remote_probe_inflight.clear()
        _remote_probe_cond.notify_all()
