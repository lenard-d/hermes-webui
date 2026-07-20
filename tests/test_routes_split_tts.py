"""Compatibility checks for the server-side TTS route-domain extraction."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from api import routes
from api.routes_parts import tts


_TTS_FUNCTION_EXPORTS = (
    "_normalize_tts_prosody",
    "_tts_addr_is_blocked",
    "_tts_host_is_blocked_target",
    "_tts_resolve_pinned_addresses",
    "_tts_resolve_pinned_address",
    "_normalized_openai_tts_base_url",
    "_buffer_tts_audio_response",
    "_tts_open",
    "_handle_tts",
)


def test_tts_part_declares_the_complete_cohesive_owner():
    assert tts.__routes_exports__ == (
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


def test_tts_function_exports_remain_owned_by_routes_facade():
    for name in _TTS_FUNCTION_EXPORTS:
        route_export = getattr(routes, name)
        assert route_export.__module__ == "api.routes"
        assert route_export.__globals__ is vars(routes)

    assert routes._TTS_PROXY_MAX_BYTES == 16 * 1024 * 1024
    assert routes._TTS_LOCALHOST_HOSTS == {"127.0.0.1", "::1", "localhost"}
    for name in (
        "_NoRedirectTtsHandler",
        "_PinnedHTTPSConnection",
        "_PinnedHTTPSHandler",
    ):
        assert getattr(routes, name).__module__ == "api.routes"


def test_tts_helper_resolves_facade_monkeypatch_at_call_time(monkeypatch):
    calls = []
    monkeypatch.setattr(
        routes,
        "_tts_resolve_pinned_addresses",
        lambda hostname, port: calls.append((hostname, port)) or ["1.1.1.1"],
    )

    assert routes._tts_resolve_pinned_address("speech.example") == "1.1.1.1"
    assert calls == [("speech.example", None)]


def test_tts_pinned_connection_keeps_facade_resolver_seam(monkeypatch):
    calls = []
    monkeypatch.setattr(
        routes,
        "_tts_resolve_pinned_addresses",
        lambda hostname, port: calls.append((hostname, port)) or [],
    )
    connection = routes._PinnedHTTPSConnection("speech.example", 443)

    with pytest.raises(OSError, match="could not connect to any pinned"):
        connection.connect()

    assert calls == [("speech.example", 443)]


def test_tts_limiter_attribute_remains_owned_by_facade_function(monkeypatch):
    checks = []

    class _Limiter:
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
    limiter = _Limiter()
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


def test_tts_implementation_is_file_backed_and_media_route_stays_in_facade():
    part_source = Path(tts.__file__).read_text(encoding="utf-8")
    facade_source = Path(routes.__file__).read_text(encoding="utf-8")

    assert "def _handle_tts(" in part_source
    assert "class _PinnedHTTPSConnection(" in part_source
    assert "def _handle_tts(" not in facade_source
    assert "def _html_preview_with_blank_base(" in facade_source
    assert "def _handle_media(" in facade_source
    assert "exec(" not in part_source
