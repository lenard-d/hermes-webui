"""Regression tests for #3546 — POST /api/models/refresh must exist and
invalidate the per-provider model cache so the "Refresh Models" button
on Settings > Providers works instead of returning 404.

The bug
-------
``static/panels.js`` sends ``POST /api/models/refresh`` from
``_refreshProviderModels``, but no route handled that path in
``api/routes.py``. Every click showed "Error: Not found" because the
server returned 404, which ``workspace.js``'s ``api()`` helper surfaced
as an error toast.

The fix
-------
``api/routes.py`` adds a ``POST /api/models/refresh`` branch that calls
``invalidate_provider_models_cache(provider_id)`` and returns
``{"ok": true, "provider": provider_id}``. The frontend success path
now also calls ``_refreshModelDropdownsAfterProviderChange()`` so the
model picker rebuilds immediately.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from tests.frontend_asset_contract import family_source


REPO = Path(__file__).resolve().parent.parent


def _read_static(name: str) -> str:
    return (REPO / "static" / name).read_text(encoding="utf-8")


def _extract_function_body(src: str, signature: str) -> str:
    """Return the source of a top-level function declaration via brace-balance."""
    idx = src.find(signature)
    if idx == -1:
        raise AssertionError(f"signature {signature!r} not found in source")
    open_idx = src.find("{", idx)
    if open_idx == -1:
        raise AssertionError(f"could not find opening brace after {signature!r}")
    depth = 0
    for i in range(open_idx, len(src)):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[idx : i + 1]
    raise AssertionError(f"unbalanced braces in {signature!r}")


class TestRefreshRouteExists:
    """The refresh route owner validates and invalidates provider caches."""

    @staticmethod
    def _context(*, clear_live_models_cache=lambda: None):
        return {
            "_clear_live_models_cache": clear_live_models_cache,
            "_handle_sessions_cleanup": lambda *_args, **_kwargs: None,
            "bad": lambda _handler, error, status=400: {
                "status": status,
                "error": error,
            },
            "j": lambda _handler, payload, **_kwargs: payload,
            "remove_provider_key": lambda *_args, **_kwargs: None,
            "set_hermes_default_model": lambda *_args, **_kwargs: None,
            "set_provider_key": lambda *_args, **_kwargs: None,
            "set_reasoning_display": lambda *_args, **_kwargs: None,
            "set_reasoning_effort": lambda *_args, **_kwargs: None,
        }

    def test_route_branch_present(self):
        from api.http.context import UNHANDLED
        from api.http.routes import provider_mutations

        result = provider_mutations.handle_post(
            object(),
            urlsplit("/api/models/refresh"),
            {"provider": "openai"},
            None,
            self._context(),
        )
        assert result is not UNHANDLED

    def test_route_validates_provider_param(self):
        from api.http.routes import provider_mutations

        result = provider_mutations.handle_post(
            object(),
            urlsplit("/api/models/refresh"),
            {},
            None,
            self._context(),
        )
        assert result == {"status": 400, "error": "provider is required"}

    def test_route_calls_invalidate_provider_models_cache(self, monkeypatch):
        import api.config as config
        from api.http.routes import provider_mutations

        invalidated = []
        live_cache_clears = []
        monkeypatch.setattr(
            config, "invalidate_provider_models_cache", invalidated.append
        )

        provider_mutations.handle_post(
            object(),
            urlsplit("/api/models/refresh"),
            {"provider": "  OpenAI  "},
            None,
            self._context(
                clear_live_models_cache=lambda: live_cache_clears.append(True)
            ),
        )
        assert invalidated == ["openai"]
        assert live_cache_clears == [True]

    def test_route_returns_ok_with_provider(self):
        from api.http.routes import provider_mutations

        result = provider_mutations.handle_post(
            object(),
            urlsplit("/api/models/refresh"),
            {"provider": "Anthropic"},
            None,
            self._context(),
        )
        assert result == {"ok": True, "provider": "anthropic"}


class TestFrontendRefreshPath:
    """The frontend success path must update the model picker immediately
    after a cache bust, not wait for the next /api/models poll."""

    def test_refresh_calls_dropdown_updater(self):
        src = family_source("panels")
        body = _extract_function_body(src, "async function _refreshProviderModels(")
        assert "_refreshModelDropdownsAfterProviderChange()" in body, (
            "_refreshProviderModels must call _refreshModelDropdownsAfterProviderChange() "
            "on success so the model picker rebuilds immediately after a cache "
            "bust instead of waiting for the next /api/models call (#3546)."
        )

    def test_refresh_shows_friendly_404(self):
        src = family_source("panels")
        body = _extract_function_body(src, "async function _refreshProviderModels(")
        assert "e.status===404" in body or "e.status === 404" in body, (
            "_refreshProviderModels catch block must check e.status===404 to "
            "show a friendly message when the route is missing on older backends."
        )
