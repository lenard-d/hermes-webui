"""Compatibility checks for the live-model discovery route extraction."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from api import routes
from api.routes_parts import live_models


REPO = Path(__file__).resolve().parents[1]


_LIVE_MODELS_FUNCTION_EXPORTS = (
    "_live_models_address_is_global",
    "_live_models_address_is_loopback",
    "_resolve_live_models_addresses",
    "_prepare_live_models_target",
    "_open_live_models_request",
    "_fetch_live_models_payload",
    "_active_profile_for_live_models_cache",
    "_live_models_cache_key",
    "_get_cached_live_models",
    "_set_cached_live_models",
    "_clear_live_models_cache",
    "_handle_live_models",
)


def test_live_models_part_declares_the_complete_discovery_owner():
    assert live_models.__routes_exports__ == (
        "_OPENAI_COMPAT_ENDPOINTS",
        "_LIVE_MODELS_CACHE_TTL",
        "_LIVE_MODELS_CACHE",
        "_LIVE_MODELS_CACHE_LOCK",
        "_LIVE_MODELS_LOOPBACK_HOSTS",
        "_live_models_address_is_global",
        "_live_models_address_is_loopback",
        "_resolve_live_models_addresses",
        "_prepare_live_models_target",
        "_NoRedirectLiveModelsHandler",
        "_PinnedLiveModelsHTTPConnection",
        "_PinnedLiveModelsHTTPSConnection",
        "_PinnedLiveModelsHTTPHandler",
        "_PinnedLiveModelsHTTPSHandler",
        "_open_live_models_request",
        "_fetch_live_models_payload",
        "_active_profile_for_live_models_cache",
        "_live_models_cache_key",
        "_get_cached_live_models",
        "_set_cached_live_models",
        "_clear_live_models_cache",
        "_handle_live_models",
    )


def test_live_models_exports_keep_the_routes_facade_as_compatibility_seam():
    for name in _LIVE_MODELS_FUNCTION_EXPORTS:
        route_export = getattr(routes, name)
        assert route_export.__module__ == "api.routes"
        assert route_export.__globals__ is vars(routes)

    assert routes._LIVE_MODELS_CACHE is live_models._LIVE_MODELS_CACHE
    assert routes._LIVE_MODELS_CACHE_LOCK is live_models._LIVE_MODELS_CACHE_LOCK
    for name in (
        "_NoRedirectLiveModelsHandler",
        "_PinnedLiveModelsHTTPConnection",
        "_PinnedLiveModelsHTTPSConnection",
        "_PinnedLiveModelsHTTPHandler",
        "_PinnedLiveModelsHTTPSHandler",
    ):
        assert getattr(routes, name).__module__ == "api.routes"


def test_live_models_handler_resolves_facade_transport_patch_at_call_time(monkeypatch):
    import api.config as config

    routes._clear_live_models_cache()
    monkeypatch.setattr(
        config,
        "get_config",
        lambda: {
            "model": {"provider": "custom:alpha"},
            "custom_providers": [
                {
                    "name": "alpha",
                    "base_url": "https://models.example/v1",
                    "api_key": "secret",
                }
            ],
        },
    )
    monkeypatch.setattr(config, "_resolve_provider_alias", lambda provider: provider)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)
    monkeypatch.setattr(
        routes,
        "_fetch_live_models_payload",
        lambda *_args, **_kwargs: {"data": [{"id": "alpha-model"}]},
    )

    payload = routes._handle_live_models(
        object(),
        SimpleNamespace(query="provider=custom:alpha"),
    )

    assert payload == {
        "provider": "custom:alpha",
        "models": [{"id": "alpha-model", "label": "Alpha Model"}],
        "count": 1,
    }


def test_live_models_pinned_connection_keeps_facade_socket_seam(monkeypatch):
    calls = []

    class _Socket:
        def setsockopt(self, *_args):
            return None

    monkeypatch.setattr(
        routes,
        "_socket",
        SimpleNamespace(
            create_connection=lambda address, timeout, source_address: calls.append(
                (address, timeout, source_address)
            )
            or _Socket(),
            IPPROTO_TCP=6,
            TCP_NODELAY=1,
        ),
    )
    connection = routes._PinnedLiveModelsHTTPConnection(
        "models.example",
        pinned_addresses=["203.0.113.7"],
        timeout=4,
    )

    connection.connect()

    assert calls == [(('203.0.113.7', 80), 4, None)]


def test_live_models_get_route_dispatches_through_the_facade(monkeypatch):
    sentinel = object()
    seen = []
    parsed = SimpleNamespace(path="/api/models/live", query="provider=openai")

    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_args: False)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        routes,
        "_handle_live_models",
        lambda handler, request: seen.append((handler, request)) or sentinel,
    )
    handler = object()

    assert routes.handle_get(handler, parsed) is sentinel
    assert seen == [(handler, parsed)]


def test_live_models_owner_imports_without_loading_routes_facade():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import api.routes_parts.live_models as owner; "
                "assert owner.__routes_exports__; "
                "assert 'api.routes' not in sys.modules"
            ),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_live_models_implementation_is_file_backed_and_dispatch_stays_in_facade():
    owner_source = Path(live_models.__file__).read_text(encoding="utf-8")
    facade_source = Path(routes.__file__).read_text(encoding="utf-8")

    assert "def _handle_live_models(" in owner_source
    assert "class _PinnedLiveModelsHTTPSConnection(" in owner_source
    assert "def _handle_live_models(" not in facade_source
    assert "exec(" not in owner_source
