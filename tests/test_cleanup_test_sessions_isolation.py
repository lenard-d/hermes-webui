"""Regression coverage for the global session-cleanup fixture."""

import json
import urllib.request


def _request_json(base_url: str, path: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        base_url + path,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def test_cleanup_fixture_tracks_actual_visibility_drift(base_url):
    settings = _request_json(
        base_url,
        "/api/settings",
        {"show_cli_sessions": True},
    )
    assert settings["show_cli_sessions"] is True
    # Deliberately do not restore the setting. The autouse fixture owns that
    # cleanup, including when a test exits through an assertion or exception.


def test_cleanup_fixture_restored_visibility_for_next_test(base_url):
    settings = _request_json(base_url, "/api/settings")
    assert settings["show_cli_sessions"] is False
