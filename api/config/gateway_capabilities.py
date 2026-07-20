"""Cached Hermes Gateway capability discovery.

The cache is private to this module; callers receive a stable capability
projection and can invalidate one endpoint or the complete cache explicitly.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request


# ── Gateway capability cache ─────────────────────────────────────────────────
# Probes /v1/capabilities once per base_url/api-key pair and caches the result
# for 60 s so guarded-turn routing decisions do not add latency on every chat
# turn.
_GATEWAY_CAPS_CACHE: dict[tuple[str, str], dict] = {}
_GATEWAY_CAPS_LOCK = threading.Lock()
_GATEWAY_CAPS_TTL_S: float = 60.0


def _gateway_caps_probe_timed_out(exc: BaseException) -> bool:
    """Keep slow capability probes on the legacy reachable-but-unsupported path."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return True
    reason_text = str(reason).lower()
    return "timed out" in reason_text or "timeout" in reason_text


def get_gateway_caps(base_url: str, api_key: str = "") -> dict:
    """Return cached gateway capability flags, probing /v1/capabilities if stale."""
    base_url = str(base_url or "").rstrip("/")
    cache_key = (base_url, str(api_key or ""))
    now = time.time()
    probe_started_at = now
    with _GATEWAY_CAPS_LOCK:
        cached = _GATEWAY_CAPS_CACHE.get(cache_key)
        if cached and now - cached.get("fetched_at", 0) < _GATEWAY_CAPS_TTL_S:
            return cached
    caps = {
        "approval_events": False,
        "run_approval_response": False,
        "capabilities_reachable": False,
        "probe_error": None,
        "fetched_at": 0.0,
    }
    try:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(
            f"{base_url}/v1/capabilities", headers=headers, method="GET"
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            caps["capabilities_reachable"] = True
            body = json.loads(resp.read(65536))
        features = body.get("features") if isinstance(body, dict) else {}
        if not isinstance(features, dict):
            features = {}
        caps["approval_events"] = bool(features.get("approval_events"))
        caps["run_approval_response"] = bool(features.get("run_approval_response"))
    except urllib.error.HTTPError as exc:
        caps["capabilities_reachable"] = True
        caps["probe_error"] = f"{type(exc).__name__}: {exc}"
    except urllib.error.URLError as exc:
        if _gateway_caps_probe_timed_out(exc):
            caps["capabilities_reachable"] = True
        caps["probe_error"] = f"{type(exc).__name__}: {exc}"
    except (TimeoutError, socket.timeout) as exc:
        caps["capabilities_reachable"] = True
        caps["probe_error"] = f"{type(exc).__name__}: {exc}"
    except OSError as exc:
        caps["probe_error"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        caps["probe_error"] = f"{type(exc).__name__}: {exc}"
    with _GATEWAY_CAPS_LOCK:
        current = _GATEWAY_CAPS_CACHE.get(cache_key)
        if current and current.get("fetched_at", 0) > probe_started_at:
            return current
        caps["fetched_at"] = time.time()
        _GATEWAY_CAPS_CACHE[cache_key] = caps
    return caps


def gateway_approval_unavailable_reason(base_url: str, api_key: str = "") -> str | None:
    """Return why approval support is unavailable, if it is unavailable."""
    caps = get_gateway_caps(base_url, api_key)
    if bool(caps.get("approval_events") and caps.get("run_approval_response")):
        return None
    if not caps.get("capabilities_reachable"):
        return "unreachable"
    return "unsupported"


def gateway_supports_approval(base_url: str, api_key: str = "") -> bool:
    """True only when the gateway advertises both approval_events and run_approval_response."""
    caps = get_gateway_caps(base_url, api_key)
    return bool(caps.get("approval_events") and caps.get("run_approval_response"))


def invalidate_gateway_caps(base_url: str | None = None) -> None:
    """Evict capability cache for base_url, or all entries when base_url is None."""
    with _GATEWAY_CAPS_LOCK:
        if base_url is None:
            _GATEWAY_CAPS_CACHE.clear()
        else:
            normalized = str(base_url or "").rstrip("/")
            for cache_key in [
                key for key in _GATEWAY_CAPS_CACHE if key[0] == normalized
            ]:
                _GATEWAY_CAPS_CACHE.pop(cache_key, None)
