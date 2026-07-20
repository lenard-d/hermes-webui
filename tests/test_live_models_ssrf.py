"""Behavioral SSRF coverage for the live-model catalog endpoint."""

from __future__ import annotations

import json
import socket
import ssl
import sys
import types
import urllib.request
from urllib.parse import urlparse

import pytest


class _Response:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._payload


class _FakeSocket:
    def __init__(self):
        self.writes = []

    def sendall(self, data):
        self.writes.append(data)

    def setsockopt(self, *_args):
        return None

    def close(self):
        return None


def _install_empty_agent_catalog(monkeypatch):
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    models = types.ModuleType("hermes_cli.models")
    models.provider_model_ids = lambda _provider: []
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", models)


def _call_custom(monkeypatch, *, base_url, provider="custom:alpha", named=True, api_key="secret"):
    import api.config as config
    import api.routes as routes

    entry = {"name": "alpha", "base_url": base_url, "api_key": api_key}
    cfg = {
        "model": {
            "provider": provider,
            "base_url": base_url,
            "api_key": api_key,
        },
        "providers": {},
        "custom_providers": [entry] if named else [],
    }
    routes._clear_live_models_cache()
    monkeypatch.setattr(config, "get_config", lambda: cfg)
    monkeypatch.setattr(config, "_resolve_provider_alias", lambda value: value)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)
    _install_empty_agent_catalog(monkeypatch)
    return routes._handle_live_models(object(), urlparse(f"/api/models/live?provider={provider}"))


def test_untrusted_private_literal_is_not_opened(monkeypatch):
    import api.routes as routes

    calls = []
    monkeypatch.setattr(
        routes,
        "_open_live_models_request",
        lambda req, **_kwargs: calls.append(req) or _Response({"data": [{"id": "stolen"}]}),
        raising=False,
    )
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, **_kwargs: calls.append(req) or _Response({"data": [{"id": "stolen"}]}),
    )

    payload = _call_custom(
        monkeypatch,
        base_url="http://10.0.0.7:8000/v1",
        provider="custom",
        named=False,
    )

    assert payload["models"] == []
    assert calls == []


def test_untrusted_hostname_resolving_private_is_not_opened(monkeypatch):
    import api.routes as routes

    calls = []
    monkeypatch.setattr(
        routes,
        "_open_live_models_request",
        lambda req, **_kwargs: calls.append(req) or _Response({"data": [{"id": "stolen"}]}),
        raising=False,
    )
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.9", 443))
        ],
    )
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, **_kwargs: calls.append(req) or _Response({"data": [{"id": "stolen"}]}),
    )

    payload = _call_custom(
        monkeypatch,
        base_url="https://models.attacker.invalid/v1",
        provider="custom",
        named=False,
    )

    assert payload["models"] == []
    assert calls == []


@pytest.mark.parametrize(
    "base_url",
    [
        "file:///tmp/provider",
        "gopher://models.example/v1",
        "https://user:password@models.example/v1",
        "https://models.example/v1?next=http://127.0.0.1",
        "https://models.example/v1#secret",
    ],
)
def test_hostile_custom_url_is_not_opened(monkeypatch, base_url):
    import api.routes as routes

    calls = []
    monkeypatch.setattr(
        routes,
        "_open_live_models_request",
        lambda req, **_kwargs: calls.append(req) or _Response({"data": [{"id": "stolen"}]}),
        raising=False,
    )
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, **_kwargs: calls.append(req) or _Response({"data": [{"id": "stolen"}]}),
    )
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))
        ],
    )

    payload = _call_custom(monkeypatch, base_url=base_url)

    assert payload["models"] == []
    assert calls == []


def test_public_custom_host_uses_vetted_address_and_scoped_key(monkeypatch):
    import api.routes as routes

    captured = {}
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))
        ],
    )

    def fake_open(req, *, pinned_addresses, timeout):
        captured.update(
            url=req.full_url,
            auth=req.headers.get("Authorization"),
            pinned=list(pinned_addresses),
            timeout=timeout,
        )
        return _Response({"data": [{"id": "safe-model"}]})

    monkeypatch.setattr(routes, "_open_live_models_request", fake_open, raising=False)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("generic urlopen used")),
    )

    payload = _call_custom(monkeypatch, base_url="https://models.example/v1")

    assert [model["id"] for model in payload["models"]] == ["safe-model"]
    assert captured == {
        "url": "https://models.example/v1/models",
        "auth": "Bearer secret",
        "pinned": ["1.1.1.1"],
        "timeout": routes.CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS,
    }


def test_explicit_named_lan_provider_remains_supported(monkeypatch):
    import api.routes as routes

    captured = {}
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.20", 8000))
        ],
    )

    def fake_open(req, *, pinned_addresses, timeout):
        captured["url"] = req.full_url
        captured["pinned"] = list(pinned_addresses)
        return _Response({"data": [{"id": "lan-model"}]})

    monkeypatch.setattr(routes, "_open_live_models_request", fake_open, raising=False)

    payload = _call_custom(monkeypatch, base_url="http://lan-host:8000/v1")

    assert [model["id"] for model in payload["models"]] == ["lan-model"]
    assert captured == {
        "url": "http://lan-host:8000/v1/models",
        "pinned": ["192.168.1.20"],
    }


def test_legacy_localhost_provider_remains_supported(monkeypatch):
    import api.routes as routes

    captured = {}
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 11434))
        ],
    )

    def fake_open(req, *, pinned_addresses, timeout):
        captured["url"] = req.full_url
        captured["pinned"] = list(pinned_addresses)
        return _Response({"data": [{"id": "local-model"}]})

    monkeypatch.setattr(routes, "_open_live_models_request", fake_open)

    payload = _call_custom(
        monkeypatch,
        base_url="http://127.0.0.1:11434/v1",
        provider="custom",
        named=False,
    )

    assert [model["id"] for model in payload["models"]] == ["local-model"]
    assert captured == {
        "url": "http://127.0.0.1:11434/v1/models",
        "pinned": ["127.0.0.1"],
    }


def test_live_models_redirect_handler_refuses_redirect():
    import api.routes as routes

    with pytest.raises(ValueError, match="redirect"):
        routes._NoRedirectLiveModelsHandler().redirect_request(
            object(), None, 302, "Found", {}, "http://169.254.169.254/latest/meta-data"
        )


def test_pinned_https_connection_dials_only_prevalidated_address(monkeypatch):
    import api.routes as routes

    created = []
    fake_socket = _FakeSocket()
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("DNS resolved twice")),
    )
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda address, *_args, **_kwargs: created.append(address) or fake_socket,
    )
    monkeypatch.setattr(
        ssl.SSLContext,
        "wrap_socket",
        lambda _self, sock, *, server_hostname: sock,
    )

    connection = routes._PinnedLiveModelsHTTPSConnection(
        "models.example",
        pinned_addresses=["1.1.1.1"],
        context=ssl.create_default_context(),
    )
    connection.connect()

    assert created == [("1.1.1.1", 443)]


def test_fixed_compat_endpoint_uses_provider_scoped_key(monkeypatch):
    import api.config as config
    import api.routes as routes

    routes._clear_live_models_cache()
    _install_empty_agent_catalog(monkeypatch)
    monkeypatch.setattr(config, "_resolve_provider_alias", lambda value: value)
    monkeypatch.setattr(
        config,
        "get_config",
        lambda: {
            "model": {"provider": "openai", "api_key": "wrong-key"},
            "providers": {"deepseek": {"api_key": "deepseek-key"}},
        },
    )
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))
        ],
    )
    captured = {}

    def fake_open(req, *, pinned_addresses, timeout):
        captured["auth"] = req.headers.get("Authorization")
        captured["url"] = req.full_url
        return _Response({"data": [{"id": "deepseek-safe"}]})

    monkeypatch.setattr(routes, "_open_live_models_request", fake_open, raising=False)

    payload = routes._handle_live_models(
        object(), urlparse("/api/models/live?provider=deepseek")
    )

    assert [model["id"] for model in payload["models"]] == ["deepseek-safe"]
    assert captured == {
        "auth": "Bearer deepseek-key",
        "url": "https://api.deepseek.com/models",
    }
