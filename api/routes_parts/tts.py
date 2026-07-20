"""Server-side text-to-speech routing and guarded upstream transport."""

from __future__ import annotations

import errno
import http.client
import json
import logging
import os
import re
import socket as _socket
import sys
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from api.helpers import read_body

logger = logging.getLogger(__name__)


def _routes_facade_override(name: str, fallback):
    """Keep class-method dependency lookups compatible with ``api.routes`` patches."""
    facade = sys.modules.get("api.routes")
    return getattr(facade, name, fallback) if facade is not None else fallback


def _normalize_tts_prosody(value, *, unit: str) -> str | None:
    if not value:
        return ""
    value = str(value).strip()
    if not re.fullmatch(r"[+-]?\d{1,3}" + re.escape(unit), value):
        return None
    amount = int(value[: -len(unit)])
    if -100 <= amount <= 100:
        return value
    return None


_TTS_PROXY_MAX_BYTES = 16 * 1024 * 1024
_TTS_LOCALHOST_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _tts_addr_is_blocked(ip_str: str) -> bool:
    """Return True when IP is in a private or otherwise non-routable class.

    The explicit flags below document the concrete SSRF-risk classes, but the
    load-bearing rule is the ``not is_global`` backstop: it blocks every address
    that is not globally routable — including ranges the named flags miss, most
    notably ``100.64.0.0/10`` (RFC 6598 CGNAT, also Tailscale's default address
    space), the ``198.18.0.0/15`` benchmarking range, and any future
    non-global allocation — so a rebinding host cannot reach a victim's tailnet
    or carrier-NAT peer.
    """
    import ipaddress

    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _tts_host_is_blocked_target(hostname: str) -> bool:
    """True if the hostname resolves to (or literally is) a private / loopback /
    link-local / reserved / multicast address — the SSRF-risk targets that an
    OpenAI-compatible TTS base_url must not be allowed to reach. Public hosts
    (a user's own hosted OpenAI-compatible server) are allowed; the explicit
    localhost-over-http dev case is handled separately by the caller."""
    import ipaddress
    import socket

    host = (hostname or "").strip().lower()
    if not host:
        return True

    # Literal IP host?
    try:
        ipaddress.ip_address(host)
        return _tts_addr_is_blocked(host)
    except ValueError:
        pass

    # DNS host: resolve and block if ANY resolved address is a blocked target
    # (defends against a hostname pointing at an internal/link-local address).
    # A DNS-resolution failure is NOT treated as an SSRF block — an unresolvable
    # host simply can't be reached (the outbound request fails naturally), and
    # failing closed here would wrongly reject legitimate public hosts that don't
    # resolve in a sandboxed/offline environment. Only a host that resolves to a
    # blocked address is rejected.
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    for info in infos:
        sockaddr = info[4]
        if sockaddr and _tts_addr_is_blocked(str(sockaddr[0])):
            return True
    return False


def _tts_resolve_pinned_addresses(hostname: str, port: int | None) -> list[str]:
    """Resolve once, validate the RRset, and preserve candidate dial order."""
    import socket

    host = (hostname or "").strip().lower()
    if not host:
        raise ValueError("invalid OpenAI TTS base_url host")

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except Exception as exc:
        raise ValueError("could not resolve OpenAI TTS base_url host") from exc
    pinned_hosts = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        pinned_host = str(sockaddr[0])
        if _tts_addr_is_blocked(pinned_host):
            raise ValueError("resolved OpenAI TTS target is not allowed")
        pinned_hosts.append(pinned_host)
    if not pinned_hosts:
        raise ValueError("could not resolve OpenAI TTS base_url host")
    return pinned_hosts


def _tts_resolve_pinned_address(hostname: str) -> str:
    """Return the first vetted literal address for direct helper callers."""
    return _tts_resolve_pinned_addresses(hostname, None)[0]


def _normalized_openai_tts_base_url(base_url: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    raw = str(base_url or "").strip()
    parsed = urlsplit(raw)
    hostname = (parsed.hostname or "").strip().lower()
    if parsed.username or parsed.password:
        raise ValueError("invalid OpenAI base_url in config")
    if not parsed.scheme or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("invalid OpenAI base_url in config")
    if parsed.scheme == "https":
        # Public https hosts are allowed (a user's own OpenAI-compatible server),
        # but reject private/loopback/link-local/reserved targets to close the
        # SSRF surface (e.g. https://169.254.169.254, https://10.x internal).
        if _tts_host_is_blocked_target(hostname):
            raise ValueError("invalid OpenAI base_url in config")
    elif parsed.scheme == "http" and hostname in _TTS_LOCALHOST_HOSTS:
        # Explicit localhost-over-http dev/self-hosted case only.
        pass
    else:
        raise ValueError("invalid OpenAI base_url in config")
    path = parsed.path.rstrip("/") or ""
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _buffer_tts_audio_response(resp, *, max_bytes: int | None = None) -> bytes:
    if max_bytes is None:
        max_bytes = _TTS_PROXY_MAX_BYTES
    headers = getattr(resp, "headers", None)
    content_type = ""
    if headers is not None:
        try:
            content_type = str(headers.get("Content-Type") or "")
        except Exception:
            content_type = ""
    if not content_type:
        try:
            info = resp.info()
            content_type = str(info.get("Content-Type") or "")
        except Exception:
            content_type = ""
    # A present Content-Type that isn't audio/* is rejected. A MISSING
    # Content-Type is tolerated: some OpenAI-compatible servers stream audio
    # bytes without setting Content-Type, and the success path defaults the
    # browser-facing type to audio/mpeg (encoded in the tests). The SSRF
    # base-url guard is the primary defense against reaching a non-audio
    # internal endpoint.
    if content_type and not content_type.lower().startswith("audio/"):
        raise ValueError("upstream returned non-audio content")
    audio_data = bytearray()
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        audio_data.extend(chunk)
        if len(audio_data) > max_bytes:
            raise ValueError("upstream audio exceeded byte limit")
    return bytes(audio_data)

class _NoRedirectTtsHandler(HTTPRedirectHandler):
    """Refuse to follow redirects on the TTS call.

    A redirect is never a legitimate response to POST /audio/speech and can
    carry the Authorization bearer to a target that bypasses the base_url check.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("OpenAI TTS upstream attempted a redirect")


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a pinned IP while keeping Host and TLS SNI on the hostname."""

    def connect(self):
        sys.audit("http.client.connect", self, self.host, self.port)
        last_error = None
        resolve_pinned_addresses = _routes_facade_override(
            "_tts_resolve_pinned_addresses",
            _tts_resolve_pinned_addresses,
        )
        socket_module = _routes_facade_override("_socket", _socket)
        for pinned_host in resolve_pinned_addresses(self.host, self.port):
            try:
                self.sock = socket_module.create_connection(
                    (pinned_host, self.port), self.timeout, self.source_address
                )
                break
            except OSError as exc:
                last_error = exc
        else:
            if last_error is not None:
                raise last_error
            raise OSError("could not connect to any pinned OpenAI TTS target")
        try:
            self.sock.setsockopt(socket_module.IPPROTO_TCP, socket_module.TCP_NODELAY, 1)
        except OSError as exc:
            if exc.errno != errno.ENOPROTOOPT:
                raise

        if self._tunnel_host:
            self._tunnel()

        server_hostname = self._tunnel_host or self.host
        self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname)


class _PinnedHTTPSHandler(HTTPSHandler):
    def https_open(self, req):
        connection_class = _routes_facade_override(
            "_PinnedHTTPSConnection",
            _PinnedHTTPSConnection,
        )
        return self.do_open(connection_class, req, context=self._context)


def _tts_open(req, *, timeout=30, opener_factory=None):
    """Thin network seam for the TTS upstream fetch so tests can intercept it.

    Defaults to a no-redirect opener (built by opener_factory) so an upstream
    redirect can't carry the Authorization bearer to — or SSRF-bounce the
    request into — a different/private target after base-url validation passed.
    Tests monkeypatch this function (or urllib.request.urlopen) to inject a
    stub response."""
    if opener_factory is not None:
        opener = opener_factory()
        return opener.open(req, timeout=timeout)
    from urllib.request import urlopen as _urlopen
    return _urlopen(req, timeout=timeout)


def _handle_tts(handler, parsed):
    """Generate TTS audio via supported server TTS engines. POST JSON body only.

    Design note addressing deep review blocker #4 (synchronous I/O):
    The server uses ThreadingHTTPServer (see server.py:173), so each request
    already runs in its own dedicated thread. A TTS request therefore occupies
    only its own thread during Microsoft network I/O + streaming; other clients
    are unaffected. Combined with early auth, a strict per-client 2 s rate
    limit, 5000-char cap, and voice allowlist, the blocking cost is bounded and
    intentional. All audio chunks are buffered before sending so that a
    Content-Length header can be included. The 5000-char cap bounds audio to
    roughly 1-5 MB, making full buffering safe. Without Content-Length the
    HTTP/1.0 server leaves the response open until a ~31 s timeout fires, and
    the browser cannot play the blob mid-stream.
    If the HTTP layer ever moves to asyncio we can adopt edge_tts's native
    async API at that time.
    """
    text = ""
    voice = "zh-CN-XiaoxiaoNeural"
    rate_str = ""
    pitch_str = ""
    engine = "edge"  # "edge" | "elevenlabs" | "openai" | "browser" (browser is client-side only)

    if handler.command != "POST":
        from api.helpers import bad as _bad
        return _bad(handler, "POST required for /api/tts", 405)

    try:
        data = read_body(handler)
        text = (data.get("text") or "").strip()
        voice = data.get("voice") or voice
        rate_str = _normalize_tts_prosody(data.get("rate"), unit="%")
        pitch_str = _normalize_tts_prosody(data.get("pitch"), unit="Hz")
        engine = (data.get("engine") or "edge").strip().lower()
    except Exception:
        from api.helpers import bad as _bad
        return _bad(handler, "invalid request body", 400)

    if rate_str is None:
        from api.helpers import bad as _bad
        return _bad(handler, "invalid rate", 400)
    if pitch_str is None:
        from api.helpers import bad as _bad
        return _bad(handler, "invalid pitch", 400)

    if not text:
        from api.helpers import bad as _bad
        return _bad(handler, "text is required", 400)
    if len(text) > 5000:
        from api.helpers import bad as _bad
        return _bad(handler, "text too long (max 5000 characters)", 400)

    from api.auth import is_auth_enabled, parse_cookie, verify_session
    cv = None
    if is_auth_enabled():
        cv = parse_cookie(handler)
        if not (cv and verify_session(cv)):
            from api.helpers import bad as _bad
            return _bad(handler, "unauthorized", 401)

    # High-quality per-client rate limiting for TTS.
    if not hasattr(_handle_tts, "_tts_limiter"):
        import time as _time, threading as _threading
        class _TtsRateLimiter:
            def __init__(self, window_seconds=2.0, prune_interval=50):
                self.window = window_seconds
                self.prune_interval = prune_interval
                self._hits = {}
                self._lock = _threading.Lock()
                self._checks = 0

            def _get_client_key(self, h):
                trust_proxy = os.getenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "").strip().lower()
                if trust_proxy in ("1", "true", "yes", "on"):
                    for hdr in ("X-Forwarded-For", "X-Real-IP", "Forwarded"):
                        val = h.headers.get(hdr)
                        if val:
                            ip = val.split(",")[0].strip().split(";")[0].strip()
                            if ip:
                                return ip
                return getattr(h, "client_address", ("unknown",))[0]

            def check(self, handler, session_cookie=None):
                key = self._get_client_key(handler)
                if session_cookie and "." in str(session_cookie):
                    key = str(session_cookie).split(".", 1)[0]
                now = _time.time()
                with self._lock:
                    self._checks += 1
                    if self._checks % self.prune_interval == 0:
                        cutoff = now - (self.window * 10)
                        self._hits = {k: v for k, v in self._hits.items() if v > cutoff}
                    last = self._hits.get(key, 0)
                    if now - last < self.window:
                        return False
                    self._hits[key] = now
                    return True

        _handle_tts._tts_limiter = _TtsRateLimiter(window_seconds=2.0)

    limiter = _handle_tts._tts_limiter
    if not limiter.check(handler, cv):
        logger.warning("TTS rate limit hit for client=%s", limiter._get_client_key(handler))
        from api.helpers import bad as _bad
        return _bad(handler, "rate limit exceeded — please wait", 429)

    # ── ElevenLabs TTS ──────────────────────────────────────────────────
    if engine == "elevenlabs":
        api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
        if not api_key:
            # Fall back to reading from Hermes .env file
            try:
                from api.onboarding import _load_env_file
                from api.profiles import get_active_hermes_home
                api_key = _load_env_file(get_active_hermes_home() / ".env").get("ELEVENLABS_API_KEY", "")
            except Exception:
                pass
        if not api_key:
            from api.helpers import bad as _bad
            return _bad(handler, "ELEVENLABS_API_KEY not configured", 503)

        # Resolve voice_id from Hermes config.yaml → env fallback
        voice_id = "pNInz6obpgDQGcFmaJgB"  # Adam (same default as hermes-agent config.yaml)
        model_id = "eleven_multilingual_v2"
        try:
            from api.config import get_config
            tts_cfg = (get_config() or {}).get("tts", {})
            if isinstance(tts_cfg, dict):
                el_cfg = tts_cfg.get("elevenlabs", {})
                if isinstance(el_cfg, dict):
                    voice_id = el_cfg.get("voice_id", voice_id)
                    model_id = el_cfg.get("model", model_id) or el_cfg.get("model_id", model_id)
                    # ^ treat empty string as "not set" — fall through to default
        except Exception:
            pass  # fall back to defaults

        # Validate voice_id is a safe path segment (no traversal)
        # fullmatch (not match) so a trailing newline can't slip past the `$`
        # anchor — defense-in-depth on the config-derived voice_id before it
        # goes into the request URL (#3510 review).
        if not re.fullmatch(r'[A-Za-z0-9_-]+', voice_id):
            from api.helpers import bad as _bad
            return _bad(handler, "invalid voice_id in config", 400)

        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream?output_format=mp3_44100_128"
        req_body = json.dumps({
            "text": text,
            "model_id": model_id,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        }).encode("utf-8")

        req = Request(url, data=req_body, headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        })

        # Buffer the full response before sending first byte.
        # The streaming endpoint is designed for chunked delivery, but urllib's
        # chunked-read path adds per-chunk overhead that dominates short TTS
        # payloads. A hard cap keeps the buffered path bounded even if the
        # upstream misbehaves.
        try:
            with _tts_open(req, timeout=30, opener_factory=lambda: build_opener(ProxyHandler({}), _NoRedirectTtsHandler())) as resp:
                audio_data = _buffer_tts_audio_response(resp)
        except ValueError:
            logger.warning("ElevenLabs TTS rejected an invalid upstream response", exc_info=True)
            from api.helpers import bad as _bad
            return _bad(handler, "ElevenLabs TTS generation failed", 502)
        except Exception:
            logger.exception("ElevenLabs TTS generation failed")
            from api.helpers import bad as _bad
            return _bad(handler, "ElevenLabs TTS generation failed", 500)

        handler.send_response(200)
        handler.send_header("Content-Type", "audio/mpeg")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", str(len(audio_data)))
        handler.end_headers()
        try:
            handler.wfile.write(audio_data)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return True

    # ── OpenAI-compatible TTS ──────────────────────────────────────────
    if engine == "openai":
        api_key = os.getenv("VOICE_TOOLS_OPENAI_KEY", "").strip()
        if not api_key:
            api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            try:
                from api.onboarding import _load_env_file
                from api.profiles import get_active_hermes_home
                env_cfg = _load_env_file(get_active_hermes_home() / ".env")
                api_key = env_cfg.get("VOICE_TOOLS_OPENAI_KEY", "") or env_cfg.get("OPENAI_API_KEY", "")
            except Exception:
                pass
        if not api_key:
            from api.helpers import bad as _bad
            return _bad(handler, "OpenAI API key not configured", 503)

        from urllib.parse import urlunsplit as _urlunsplit

        base_url = _urlunsplit(("https", "api.openai.com", "/v1", "", ""))
        model = "gpt-4o-mini-tts"
        oai_voice = "alloy"
        try:
            from api.config import get_config
            tts_cfg = (get_config() or {}).get("tts", {})
            if isinstance(tts_cfg, dict):
                oai_cfg = tts_cfg.get("openai", {})
                if isinstance(oai_cfg, dict):
                    base_url = _normalized_openai_tts_base_url(oai_cfg.get("base_url") or base_url)
                    model = oai_cfg.get("model") or model
                    oai_voice = oai_cfg.get("voice") or oai_voice
                else:
                    base_url = _normalized_openai_tts_base_url(base_url)
            else:
                base_url = _normalized_openai_tts_base_url(base_url)
        except ValueError:
            from api.helpers import bad as _bad
            return _bad(handler, "invalid OpenAI base_url in config", 400)
        except Exception:
            pass

        url = f"{base_url}/audio/speech"
        req_body = json.dumps({
            "model": model,
            "input": text,
            "voice": oai_voice,
        }).encode("utf-8")

        req = Request(url, data=req_body, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        })

        # Use a pinned HTTPS opener so the resolved address is the one that gets
        # dialed. Keep the no-redirect handler in the same chain to block
        # bearer leaks and SSRF bounce redirects after hostname validation.
        try:
            with _tts_open(req, timeout=30, opener_factory=lambda: build_opener(ProxyHandler({}), _NoRedirectTtsHandler(), _PinnedHTTPSHandler())) as resp:
                audio_data = _buffer_tts_audio_response(resp)
        except ValueError:
            logger.warning("OpenAI TTS rejected an invalid upstream response", exc_info=True)
            from api.helpers import bad as _bad
            return _bad(handler, "OpenAI TTS generation failed", 502)
        except Exception:
            logger.exception("OpenAI TTS generation failed")
            from api.helpers import bad as _bad
            return _bad(handler, "OpenAI TTS generation failed", 500)

        handler.send_response(200)
        handler.send_header("Content-Type", "audio/mpeg")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", str(len(audio_data)))
        handler.end_headers()
        try:
            handler.wfile.write(audio_data)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return True

    # ── Edge TTS ────────────────────────────────────────────────────────
    allowed = {
        "zh-CN-XiaoxiaoNeural", "zh-CN-XiaoyiNeural", "zh-CN-YunxiNeural",
        "zh-CN-YunjianNeural", "zh-CN-YunyangNeural",
        "en-US-AriaNeural", "en-US-GuyNeural",
        "fr-CA-AntoineNeural", "fr-CA-JeanNeural",
        "fr-CA-SylvieNeural", "fr-CA-ThierryNeural",
        "fr-FR-DeniseNeural", "fr-FR-EloiseNeural", "fr-FR-HenriNeural",
        "id-ID-GadisNeural",
    }
    if voice not in allowed:
        from api.helpers import bad as _bad
        return _bad(handler, "invalid voice", 400)

    try:
        try:
            import edge_tts
        except ImportError:
            from api.helpers import bad as _bad
            return _bad(handler, "Edge TTS engine not installed on the server. Install it with: pip install edge-tts", 503)

        kwargs = {}
        if rate_str:
            kwargs["rate"] = rate_str
        if pitch_str:
            kwargs["pitch"] = pitch_str

        comm = edge_tts.Communicate(text, voice, **kwargs)

        # Buffer all audio chunks before responding so Content-Length is known.
        # Without it the HTTP/1.0 server holds the connection open until a ~31 s
        # timeout fires and the browser cannot play the resulting blob.
        audio_buf = bytearray()
        for chunk in comm.stream_sync():
            if chunk.get("type") == "audio" and chunk.get("data"):
                audio_buf.extend(chunk["data"])

        if not audio_buf:
            from api.helpers import bad as _bad
            return _bad(handler, "TTS produced no audio", 500)

        handler.send_response(200)
        handler.send_header("Content-Type", "audio/mpeg")
        handler.send_header("Content-Length", str(len(audio_buf)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        try:
            handler.wfile.write(audio_buf)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return True

    except BrokenPipeError:
        return True
    except Exception:
        logger.exception("Edge TTS generation failed")
        from api.helpers import bad as _bad
        return _bad(handler, "TTS generation failed", 500)


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
)
