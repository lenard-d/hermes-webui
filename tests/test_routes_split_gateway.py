"""Compatibility checks for the gateway route-domain extraction."""

import subprocess

import api.routes as routes


def test_gateway_handler_keeps_routes_monkeypatch_seams(monkeypatch):
    calls = []

    monkeypatch.setattr(
        routes,
        "_run_gateway_lifecycle_command",
        lambda action: subprocess.CompletedProcess(["hermes", "gateway", action], 0, "", ""),
    )
    monkeypatch.setattr(routes, "_gateway_status_payload", lambda: {"running": True})
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200: calls.append((payload, status)) or payload,
    )

    result = routes._handle_gateway_lifecycle(object(), "start", {})

    assert result["ok"] is True
    assert result["status"] == {"running": True}
    assert calls == [(result, 200)]


def test_gateway_exports_remain_owned_by_routes_facade():
    assert routes._gateway_status_payload.__module__ == "api.routes"
    assert routes._handle_gateway_lifecycle.__module__ == "api.routes"
    assert routes._handle_gateway_lifecycle.__globals__ is vars(routes)
