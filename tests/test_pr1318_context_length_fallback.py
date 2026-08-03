"""Regression coverage for #1318/#1344 context-window metadata fallback.

The terminal projection owns the fallback now: it resolves metadata only when
the agent compressor has no context window, then projects that answer onto the
durable session and terminal usage payload. These checks deliberately exercise
that owner instead of the former monolithic streaming implementation.
"""

import sys
import types


def _install_metadata_resolver(monkeypatch, resolver):
    module = types.ModuleType("agent.model_metadata")
    module.get_model_context_length = resolver
    package = sys.modules.get("agent")
    if package is None:
        package = types.ModuleType("agent")
        package.__path__ = []
        monkeypatch.setitem(sys.modules, "agent", package)
    monkeypatch.setitem(sys.modules, "agent.model_metadata", module)


def _project(*, compressor_length=0, resolver_config=None, base_url="https://api.example/v1"):
    from api.runs.local_context_window import ContextWindowProjection

    return ContextWindowProjection.from_agent(
        types.SimpleNamespace(
            model="fallback-model",
            base_url=base_url,
            api_key="",
            context_compressor=types.SimpleNamespace(
                context_length=compressor_length,
                threshold_tokens=6000,
                last_prompt_tokens=1234,
            ),
        ),
        resolved_model="fallback-model",
        resolved_provider="openrouter",
        resolved_base_url="",
        resolved_api_key="",
        config=resolver_config or {},
    )


def test_fallback_uses_model_metadata_when_compressor_has_no_window(monkeypatch):
    calls = []

    def resolver(model, base_url, **kwargs):
        calls.append((model, base_url, kwargs))
        return 1_000_000

    _install_metadata_resolver(monkeypatch, resolver)
    projection = _project()

    assert projection.context_length == 1_000_000
    assert len(calls) == 1
    assert calls[0][:2] == ("fallback-model", "https://api.example/v1")


def test_model_metadata_refresh_replaces_a_stale_compressor_window(monkeypatch):
    calls = []
    _install_metadata_resolver(
        monkeypatch,
        lambda *_args, **_kwargs: calls.append(True) or 1_000_000,
    )

    projection = _project(compressor_length=200_000)

    assert projection.context_length == 1_000_000
    assert projection.threshold_tokens == 30_000
    assert calls == [True]


def test_fallback_passes_agent_model_and_base_url(monkeypatch):
    seen = {}

    def resolver(model, base_url, **kwargs):
        seen.update(model=model, base_url=base_url, kwargs=kwargs)
        return 400_000

    _install_metadata_resolver(monkeypatch, resolver)
    assert _project(base_url="https://custom.example/v1").context_length == 400_000
    assert seen["model"] == "fallback-model"
    assert seen["base_url"] == "https://custom.example/v1"


def test_fallback_exception_is_non_fatal(monkeypatch):
    _install_metadata_resolver(
        monkeypatch,
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("metadata unavailable")),
    )

    assert _project().context_length == 0


def test_fallback_retries_legacy_metadata_signature(monkeypatch):
    calls = []

    def legacy_resolver(model, base_url, **kwargs):
        calls.append((model, base_url, kwargs))
        if kwargs:
            raise TypeError("old agent signature")
        return 256_000

    _install_metadata_resolver(monkeypatch, legacy_resolver)

    assert _project().context_length == 256_000
    assert len(calls) == 2
    assert calls[0][2]
    assert calls[1][2] == {}


def test_fallback_projection_persists_the_resolved_context_window(monkeypatch):
    _install_metadata_resolver(monkeypatch, lambda *_args, **_kwargs: 512_000)
    projection = _project()
    session = types.SimpleNamespace(
        context_length=0,
        threshold_tokens=0,
        last_prompt_tokens=0,
    )

    projection.persist_on(session)

    assert session.context_length == 512_000
    assert session.threshold_tokens == 6000
    assert session.last_prompt_tokens == 1234
