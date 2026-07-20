"""Architecture and compatibility coverage for the speech package migration."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from api import routes, speech
from api.routes_parts import media_files, tts
from api.speech import transport


_TTS_ROUTE_EXPORTS = (
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


def test_tts_route_exports_remain_available_through_compatibility_facade():
    assert tts.__routes_exports__ == _TTS_ROUTE_EXPORTS
    for name in _TTS_ROUTE_EXPORTS:
        assert getattr(routes, name) is not None

    assert routes._TTS_PROXY_MAX_BYTES == 16 * 1024 * 1024
    assert routes._TTS_LOCALHOST_HOSTS == {"127.0.0.1", "::1", "localhost"}
    assert routes._NoRedirectTtsHandler is transport.NoRedirectTtsHandler
    assert routes._PinnedHTTPSConnection is transport.PinnedHTTPSConnection
    assert routes._PinnedHTTPSHandler is transport.PinnedHTTPSHandler


def test_speech_package_exposes_deep_operations_instead_of_http_handlers():
    assert callable(speech.parse_tts_request)
    assert callable(speech.resolve_tts_provider)
    assert callable(speech.synthesize_tts)
    assert callable(speech.transcribe_audio_upload)
    assert callable(speech.discover_transcription_provider)
    assert not hasattr(speech, "handle_post")


def test_pinned_address_resolution_is_owned_by_transport(monkeypatch):
    calls = []

    def fake_getaddrinfo(hostname, port, **kwargs):
        calls.append((hostname, port, kwargs))
        return [(0, 0, 0, "", ("1.1.1.1", port or 0))]

    monkeypatch.setattr(transport.socket, "getaddrinfo", fake_getaddrinfo)

    assert routes._tts_resolve_pinned_address("speech.example") == "1.1.1.1"
    assert calls == [
        ("speech.example", None, {"type": transport.socket.SOCK_STREAM})
    ]


def test_pinned_connection_uses_transport_resolver_seam(monkeypatch):
    calls = []
    monkeypatch.setattr(
        transport,
        "tts_resolve_pinned_addresses",
        lambda hostname, port: calls.append((hostname, port)) or [],
    )
    connection = routes._PinnedHTTPSConnection("speech.example", 443)

    with pytest.raises(OSError, match="could not connect to any pinned"):
        connection.connect()

    assert calls == [("speech.example", 443)]


def test_tts_limiter_attribute_remains_on_http_adapter(monkeypatch):
    checks = []

    class Limiter:
        def check(self, handler, session_cookie=None):
            checks.append((handler, session_cookie))
            return False

        def _get_client_key(self, _handler):
            return "test-client"

    handler = SimpleNamespace(
        command="POST",
        headers={},
        client_address=("127.0.0.1", 12345),
    )
    limiter = Limiter()
    monkeypatch.setattr(routes._handle_tts, "_tts_limiter", limiter, raising=False)
    monkeypatch.setattr(
        routes,
        "read_body",
        lambda _handler: {"text": "hello", "engine": "edge"},
    )
    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: False)
    monkeypatch.setattr(
        "api.helpers.bad",
        lambda seen_handler, message, status=400: (seen_handler, message, status),
    )

    assert routes._handle_tts(handler, None) == (
        handler,
        "rate limit exceeded — please wait",
        429,
    )
    assert checks == [(handler, None)]
    assert routes._handle_tts._tts_limiter is limiter


def test_route_adapter_calls_speech_interface_and_serializes_audio(monkeypatch):
    seen = {}

    class Limiter:
        def check(self, _handler, _session_cookie=None):
            return True

    class Handler:
        command = "POST"
        headers = {}
        client_address = ("127.0.0.1", 12345)

        def __init__(self):
            self.status = None
            self.sent_headers = {}
            self.wfile = SimpleNamespace(write=lambda content: seen.setdefault("body", content))

        def send_response(self, status):
            self.status = status

        def send_header(self, name, value):
            self.sent_headers[name] = value

        def end_headers(self):
            return None

    request = speech.TtsRequest("hello", "edge", "en-US-AriaNeural", "", "")
    provider = object()
    monkeypatch.setattr(routes._handle_tts, "_tts_limiter", Limiter(), raising=False)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"text": "hello"})
    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: False)
    monkeypatch.setattr(speech, "parse_tts_request", lambda _body: request)
    monkeypatch.setattr(speech, "resolve_tts_provider", lambda engine: provider)

    def fake_synthesize(seen_request, seen_provider, **kwargs):
        seen["request"] = seen_request
        seen["provider"] = seen_provider
        seen["dependencies"] = kwargs
        return speech.SpeechAudio(b"audio")

    monkeypatch.setattr(speech, "synthesize_tts", fake_synthesize)
    handler = Handler()

    assert routes._handle_tts(handler, None) is True
    assert handler.status == 200
    assert handler.sent_headers["Content-Length"] == "5"
    assert seen["body"] == b"audio"
    assert seen["request"] is request
    assert seen["provider"] is provider


def test_speech_implementation_is_package_owned_without_route_back_imports():
    package_dir = Path(speech.__file__).parent
    route_source = Path(tts.__file__).read_text(encoding="utf-8")
    transport_source = Path(transport.__file__).read_text(encoding="utf-8")
    media_source = Path(media_files.__file__).read_text(encoding="utf-8")
    facade_source = Path(routes.__file__).read_text(encoding="utf-8")
    speech_source = "\n".join(
        path.read_text(encoding="utf-8") for path in package_dir.glob("*.py")
    )

    assert "def _handle_tts(" in route_source
    assert "class PinnedHTTPSConnection(" not in route_source
    assert "class PinnedHTTPSConnection(" in transport_source
    assert "def _handle_tts(" not in facade_source
    assert "api.routes" not in speech_source
    assert "sys.modules" not in speech_source
    assert "_routes_facade_override" not in speech_source
    assert "def _handle_media(" in media_source
    assert "exec(" not in speech_source
