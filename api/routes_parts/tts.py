"""Thin HTTP adapter for the speech package."""

from __future__ import annotations

import logging
import os
import threading
import time
from urllib.request import ProxyHandler

from api.helpers import read_body
from api import speech as _speech_api
from api.speech.transport import (
    NoRedirectTtsHandler as _NoRedirectTtsHandler,
    PinnedHTTPSConnection as _PinnedHTTPSConnection,  # noqa: F401 - route compatibility export
    PinnedHTTPSHandler as _PinnedHTTPSHandler,
    TTS_LOCALHOST_HOSTS as _TTS_LOCALHOST_HOSTS,  # noqa: F401 - route compatibility export
    TTS_PROXY_MAX_BYTES as _TTS_PROXY_MAX_BYTES,
    buffer_tts_audio_response as _speech_buffer_tts_audio_response,
    normalize_openai_tts_base_url as _speech_normalized_openai_tts_base_url,
    tts_addr_is_blocked as _speech_tts_addr_is_blocked,
    tts_host_is_blocked_target as _speech_tts_host_is_blocked_target,
    tts_open as _speech_tts_open,
    tts_resolve_pinned_address as _speech_tts_resolve_pinned_address,
    tts_resolve_pinned_addresses as _speech_tts_resolve_pinned_addresses,
)
from api.speech.validation import normalize_tts_prosody as _speech_normalize_tts_prosody


logger = logging.getLogger(__name__)


def _normalize_tts_prosody(value, *, unit: str) -> str | None:
    return _speech_normalize_tts_prosody(value, unit=unit)


def _tts_addr_is_blocked(ip_str: str) -> bool:
    return _speech_tts_addr_is_blocked(ip_str)


def _tts_host_is_blocked_target(hostname: str) -> bool:
    return _speech_tts_host_is_blocked_target(hostname)


def _tts_resolve_pinned_addresses(hostname: str, port: int | None) -> list[str]:
    return _speech_tts_resolve_pinned_addresses(hostname, port)


def _tts_resolve_pinned_address(hostname: str) -> str:
    return _speech_tts_resolve_pinned_address(hostname)


def _normalized_openai_tts_base_url(base_url: str) -> str:
    return _speech_normalized_openai_tts_base_url(base_url)


def _buffer_tts_audio_response(resp, *, max_bytes: int | None = None) -> bytes:
    return _speech_buffer_tts_audio_response(
        resp,
        max_bytes=_TTS_PROXY_MAX_BYTES if max_bytes is None else max_bytes,
    )


def _tts_open(req, *, timeout=30, opener_factory=None):
    return _speech_tts_open(
        req,
        timeout=timeout,
        opener_factory=opener_factory,
    )


class _TtsRateLimiter:
    """Bounded per-client request cache owned by the HTTP adapter."""

    def __init__(self, window_seconds=2.0, prune_interval=50):
        self.window = window_seconds
        self.prune_interval = prune_interval
        self._hits = {}
        self._lock = threading.Lock()
        self._checks = 0

    def _get_client_key(self, handler):
        trust_proxy = os.getenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "").strip().lower()
        if trust_proxy in ("1", "true", "yes", "on"):
            for header in ("X-Forwarded-For", "X-Real-IP", "Forwarded"):
                value = handler.headers.get(header)
                if value:
                    client = value.split(",")[0].strip().split(";")[0].strip()
                    if client:
                        return client
        return getattr(handler, "client_address", ("unknown",))[0]

    def check(self, handler, session_cookie=None):
        key = self._get_client_key(handler)
        if session_cookie and "." in str(session_cookie):
            key = str(session_cookie).split(".", 1)[0]
        now = time.time()
        with self._lock:
            self._checks += 1
            if self._checks % self.prune_interval == 0:
                cutoff = now - (self.window * 10)
                self._hits = {
                    cached_key: timestamp
                    for cached_key, timestamp in self._hits.items()
                    if timestamp > cutoff
                }
            last = self._hits.get(key, 0)
            if now - last < self.window:
                return False
            self._hits[key] = now
            return True


def _write_audio_response(handler, audio) -> bool:
    handler.send_response(200)
    handler.send_header("Content-Type", audio.content_type)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(audio.content)))
    handler.end_headers()
    try:
        handler.wfile.write(audio.content)
    except (BrokenPipeError, ConnectionResetError):
        pass
    return True


def _handle_tts(handler, parsed):
    """Translate one HTTP request into the speech package interface."""
    from api.helpers import bad as _bad

    if handler.command != "POST":
        return _bad(handler, "POST required for /api/tts", 405)

    try:
        request = _speech_api.parse_tts_request(read_body(handler))
    except _speech_api.InvalidSpeechRequest as exc:
        return _bad(handler, exc.public_message, exc.status)
    except Exception:
        return _bad(handler, "invalid request body", 400)

    from api.auth import is_auth_enabled, parse_cookie, verify_session

    session_cookie = None
    if is_auth_enabled():
        session_cookie = parse_cookie(handler)
        if not (session_cookie and verify_session(session_cookie)):
            return _bad(handler, "unauthorized", 401)

    if not hasattr(_handle_tts, "_tts_limiter"):
        _handle_tts._tts_limiter = _TtsRateLimiter(window_seconds=2.0)
    limiter = _handle_tts._tts_limiter
    if not limiter.check(handler, session_cookie):
        logger.warning(
            "TTS rate limit hit for client=%s",
            limiter._get_client_key(handler),
        )
        return _bad(handler, "rate limit exceeded — please wait", 429)

    try:
        provider = _speech_api.resolve_tts_provider(request.engine)
        audio = _speech_api.synthesize_tts(
            request,
            provider,
            open_fn=_tts_open,
            max_bytes=_TTS_PROXY_MAX_BYTES,
            proxy_handler_factory=ProxyHandler,
            no_redirect_handler_class=_NoRedirectTtsHandler,
            pinned_https_handler_class=_PinnedHTTPSHandler,
        )
    except BrokenPipeError:
        return True
    except _speech_api.SpeechError as exc:
        return _bad(handler, exc.public_message, exc.status)
    return _write_audio_response(handler, audio)


def _stt_provider_capability_from_module(stt):
    return _speech_api.transcription_provider_capability_from_module(stt)


def _stt_provider_capability():
    return _speech_api.discover_transcription_provider()


def handle_transcribe(handler):
    """Parse the multipart HTTP request and delegate temp-file ownership."""
    from api.config import MAX_UPLOAD_BYTES
    from api.helpers import j
    from api.upload import _sanitize_upload_name, parse_multipart

    try:
        content_type = handler.headers.get("Content-Type", "")
        content_length = int(handler.headers.get("Content-Length", 0) or 0)
        if content_length > MAX_UPLOAD_BYTES:
            return j(
                handler,
                {"error": f"File too large (max {MAX_UPLOAD_BYTES // 1024 // 1024}MB)"},
                status=413,
            )
        _fields, files = parse_multipart(
            handler.rfile,
            content_type,
            content_length,
        )
        if "file" not in files:
            return j(handler, {"error": "No file field in request"}, status=400)
        filename, content = files["file"]
        if not filename:
            return j(handler, {"error": "No filename in upload"}, status=400)
        safe_name = _sanitize_upload_name(filename)
        transcript = _speech_api.transcribe_audio_upload(safe_name, content)
        return j(handler, {"ok": True, "transcript": transcript.text})
    except ValueError as exc:
        return j(handler, {"error": str(exc)}, status=400)
    except _speech_api.SpeechError as exc:
        return j(handler, {"error": exc.public_message}, status=exc.status)


def handle_transcribe_capability(handler):
    from api.helpers import j

    available, provider = _stt_provider_capability()
    return j(
        handler,
        {"ok": True, "available": bool(available), "provider": provider},
    )


__routes_exports__ = (
    "_normalize_tts_prosody",
    "_TTS_PROXY_MAX_BYTES",
    "_TTS_LOCALHOST_HOSTS",
    "_tts_addr_is_blocked",
    "_tts_host_is_blocked_target",
    "_tts_resolve_pinned_addresses",
    "_tts_resolve_pinned_address",
    "_normalized_openai_tts_base_url",
    "_buffer_tts_audio_response",
    "_NoRedirectTtsHandler",
    "_PinnedHTTPSConnection",
    "_PinnedHTTPSHandler",
    "_tts_open",
    "_handle_tts",
    "_stt_provider_capability_from_module",
    "_stt_provider_capability",
    "handle_transcribe",
    "handle_transcribe_capability",
)
