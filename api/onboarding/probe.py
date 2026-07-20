"""Bounded OpenAI-compatible provider endpoint probing."""

from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse

from .catalog import normalize_base_url

logger = logging.getLogger(__name__)

PROBE_ERROR_CODES = (
    "invalid_url",
    "dns",
    "connect_refused",
    "timeout",
    "http_4xx",
    "http_5xx",
    "parse",
    "unreachable",
)
PROBE_TIMEOUT_SECONDS = 5.0
PROBE_MAX_BYTES = 256 * 1024


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_PROBE_OPENER = urllib.request.build_opener(_NoRedirectHandler())
_DNS_ONLY_TEST_TLDS = frozenset({"invalid", "test", "example"})


def _hostname_uses_reserved_dns_tld(hostname: str | None) -> bool:
    host = str(hostname or "").strip().rstrip(".").lower()
    return bool(host and "." in host and host.rsplit(".", 1)[-1] in _DNS_ONLY_TEST_TLDS)


def _exception_chain_text(exc) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(str(current))
        current = getattr(current, "__cause__", None) or getattr(
            current, "__context__", None
        )
    return " ".join(parts).lower()


def _probe_failure_is_dns(exc, hostname: str | None) -> bool:
    if isinstance(exc, socket.gaierror):
        return True
    text = _exception_chain_text(exc)
    markers = (
        "getaddrinfo",
        "gaierror",
        "name or service not known",
        "temporary failure in name resolution",
        "nodename nor servname provided",
        "no address associated with hostname",
    )
    return any(marker in text for marker in markers) or _hostname_uses_reserved_dns_tld(
        hostname
    )


def _http_error(exc: urllib.error.HTTPError) -> dict:
    if 300 <= exc.code < 400:
        return {
            "ok": False,
            "error": "unreachable",
            "detail": (
                f"HTTP {exc.code} — endpoint returned a redirect "
                "(probe does not follow redirects). Point base_url at the final URL directly."
            ),
            "status": exc.code,
        }
    code = "http_4xx" if 400 <= exc.code < 500 else "http_5xx"
    try:
        body = exc.read(2048).decode("utf-8", errors="replace").strip()
    except Exception:
        body = ""
    detail = f"HTTP {exc.code}"
    if body:
        detail = f"{detail}: {body.splitlines()[0][:200]}"
    return {"ok": False, "error": code, "detail": detail, "status": exc.code}


def _url_error(
    exc: urllib.error.URLError, *, hostname: str, scheme: str, port: int | None, timeout: float
) -> dict:
    reason = exc.reason
    if isinstance(reason, socket.timeout) or "timed out" in str(reason).lower():
        return {
            "ok": False,
            "error": "timeout",
            "detail": f"connection timed out after {timeout:g}s",
        }
    if _probe_failure_is_dns(reason, hostname):
        return {
            "ok": False,
            "error": "dns",
            "detail": f"could not resolve host '{hostname}'",
        }
    if isinstance(reason, ConnectionRefusedError) or "refused" in str(reason).lower():
        port_hint = port or (443 if scheme == "https" else 80)
        return {
            "ok": False,
            "error": "connect_refused",
            "detail": f"connection refused at {hostname}:{port_hint}",
        }
    return {"ok": False, "error": "unreachable", "detail": str(reason)[:200]}


def _parse_models(body: bytes, status: int) -> dict:
    if len(body) > PROBE_MAX_BYTES:
        return {
            "ok": False,
            "error": "parse",
            "detail": f"response exceeded {PROBE_MAX_BYTES // 1024} KB cap",
        }
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError) as exc:
        return {
            "ok": False,
            "error": "parse",
            "detail": f"response is not JSON ({exc.__class__.__name__})",
        }
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        entries = payload["data"]
    elif isinstance(payload, list):
        entries = payload
    else:
        return {
            "ok": False,
            "error": "parse",
            "detail": "response is not in OpenAI /models shape (expected {'data': [...]} or [...])",
        }
    models = []
    for entry in entries:
        model_id = ""
        if isinstance(entry, dict) and entry.get("id"):
            model_id = str(entry["id"]).strip()
        elif isinstance(entry, str):
            model_id = entry.strip()
        if model_id:
            models.append({"id": model_id, "label": model_id})
    return {"ok": True, "models": models, "status": status}


def probe_provider_endpoint(
    provider: str,
    base_url: str,
    api_key: str | None = None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> dict:
    """Probe exactly ``<base_url>/models`` without redirects or retries.

    Private addresses remain valid because local model servers are the intended
    target. The response is bounded and never persisted, and credential material
    is sent only to the exact user-selected URL.
    """
    del provider  # provider identity does not alter the OpenAI-compatible probe
    normalized_url = normalize_base_url(base_url)
    if not normalized_url:
        return {"ok": False, "error": "invalid_url", "detail": "base_url is required"}
    parsed = urlparse(normalized_url)
    if parsed.scheme not in {"http", "https"}:
        return {
            "ok": False,
            "error": "invalid_url",
            "detail": "base_url must start with http:// or https://",
        }
    if not parsed.hostname:
        return {"ok": False, "error": "invalid_url", "detail": "base_url has no host"}

    headers = {
        "Accept": "application/json",
        "User-Agent": "hermes-webui-onboarding-probe",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        f"{normalized_url}/models", headers=headers, method="GET"
    )
    try:
        with _PROBE_OPENER.open(request, timeout=timeout) as response:
            return _parse_models(response.read(PROBE_MAX_BYTES + 1), response.status)
    except urllib.error.HTTPError as exc:
        return _http_error(exc)
    except urllib.error.URLError as exc:
        return _url_error(
            exc,
            hostname=parsed.hostname,
            scheme=parsed.scheme,
            port=parsed.port,
            timeout=timeout,
        )
    except (TimeoutError, socket.timeout):
        return {
            "ok": False,
            "error": "timeout",
            "detail": f"connection timed out after {timeout:g}s",
        }
    except Exception as exc:
        if _probe_failure_is_dns(exc, parsed.hostname):
            return {
                "ok": False,
                "error": "dns",
                "detail": f"could not resolve host '{parsed.hostname}'",
            }
        logger.debug("provider endpoint probe failed", exc_info=True)
        return {"ok": False, "error": "unreachable", "detail": str(exc)[:200]}
