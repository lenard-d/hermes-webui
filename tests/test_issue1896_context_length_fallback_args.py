"""Regression checks for #1896 — context-length fallback ignores config overrides.

The former session-persistence and terminal-SSE fallback callsites are now one
``ContextWindowProjection`` owner. It resolves the window once, then projects
that same answer into durable session metadata and the terminal usage payload.
Its metadata lookup must still carry `config_context_length`, `provider`, and
`custom_providers` and must still support older Hermes-agent signatures.

When the agent's `context_compressor` reports 0 (fresh / cached / transitioning
agent), context-length resolution falls all the way through to
`DEFAULT_FALLBACK_CONTEXT = 256_000` even when the user has set
`model.context_length: 1048576` in `config.yaml` or has a 1M model with a
`custom_providers` per-model override.

For users with a context-management plugin (LCM) configured around the real
window, this cascades into a session-killing failure mode: auto-compression
triggers far too early → flood of compress requests → 429s → credential pool
exhaustion → fallback also 429s → "API call failed after 3 retries".

These tests pin the call shape so future refactors can't silently drop the
config-override args again.
"""

import sys
import types
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SESSION_MODELS_PY = (
    (REPO / "api" / "sessions" / "session_model_context.py").read_text(
        encoding="utf-8"
    )
    + (REPO / "api" / "model_context.py").read_text(encoding="utf-8")
)


def _install_context_length_resolver(monkeypatch, resolver):
    module = types.ModuleType("agent.model_metadata")
    module.get_model_context_length = resolver
    if "agent" not in sys.modules:
        package = types.ModuleType("agent")
        package.__path__ = []
        monkeypatch.setitem(sys.modules, "agent", package)
    monkeypatch.setitem(sys.modules, "agent.model_metadata", module)


def _projection(
    *,
    config,
    model="configured-model",
    provider="openrouter",
    base_url="",
    api_key="runtime-key",
):
    from api.runs.local_context_window import ContextWindowProjection

    agent = types.SimpleNamespace(
        model=model,
        base_url=base_url,
        api_key=api_key,
        context_compressor=types.SimpleNamespace(
            context_length=0,
            threshold_tokens=0,
            last_prompt_tokens=321,
        ),
    )
    return ContextWindowProjection.from_agent(
        agent,
        resolved_model=model,
        resolved_provider=provider,
        resolved_base_url="",
        resolved_api_key="",
        config=config,
    )


def test_context_projection_resolves_once_and_projects_to_session_and_usage(monkeypatch):
    """The consolidated owner gives persistence and terminal SSE one answer."""
    calls = []

    def resolver(model, base_url, **kwargs):
        calls.append((model, base_url, kwargs))
        return kwargs["config_context_length"]

    _install_context_length_resolver(monkeypatch, resolver)
    projection = _projection(
        config={
            "model": {"default": "configured-model", "context_length": 1_048_576},
            "custom_providers": [],
        }
    )
    session = types.SimpleNamespace(
        context_length=0,
        threshold_tokens=0,
        last_prompt_tokens=0,
    )
    usage = {}
    projection.persist_on(session)
    projection.enrich_usage(usage, session=session)

    assert len(calls) == 1
    assert projection.context_length == 1_048_576
    assert session.context_length == 1_048_576
    assert usage["context_length"] == 1_048_576
    assert usage["last_prompt_tokens"] == 321


def test_context_projection_passes_provider_and_custom_provider_config(monkeypatch):
    """The metadata resolver receives the active provider and custom catalog."""
    calls = []

    def resolver(model, base_url, **kwargs):
        calls.append((model, base_url, kwargs))
        return kwargs["config_context_length"]

    custom_providers = [
        {
            "name": "llm-proxy",
            "base_url": "https://proxy.example/v1",
            "models": {"custom-model": {"context_length": 777_000}},
        }
    ]
    _install_context_length_resolver(monkeypatch, resolver)
    projection = _projection(
        model="custom-model",
        provider="custom:llm-proxy",
        config={"custom_providers": custom_providers},
    )

    _model, base_url, kwargs = calls[-1]
    assert projection.context_length == 777_000
    assert base_url == "https://proxy.example/v1"
    assert kwargs["provider"] == "custom:llm-proxy"
    assert kwargs["custom_providers"] is custom_providers
    assert kwargs["config_context_length"] == 777_000


def test_config_context_length_parsed_safely():
    """Invalid config_context_length values must NOT crash the resolver call —
    they should fall through to provider/registry probing instead."""
    # Both blocks should wrap the int parse in try/except (TypeError, ValueError).
    assert "except (TypeError, ValueError):" in SESSION_MODELS_PY, (
        "Config context_length parse must be guarded against (TypeError, ValueError) "
        "so a string like '256K' or 'one million' falls through to the resolver "
        "instead of crashing the SSE/save path."
    )


def test_legacy_signature_fallback_present():
    """Older hermes-agent builds may not yet have config_context_length on
    get_model_context_length(). The fix must catch TypeError and retry with
    the legacy 2-arg form so the indicator still resolves *something*."""
    calls = []

    def legacy_resolver(model, base_url, **kwargs):
        calls.append((model, base_url, kwargs))
        if kwargs:
            raise TypeError("old agent signature")
        return 512_000

    from pytest import MonkeyPatch

    monkeypatch = MonkeyPatch()
    try:
        _install_context_length_resolver(monkeypatch, legacy_resolver)
        projection = _projection(config={"custom_providers": []})
    finally:
        monkeypatch.undo()

    assert projection.context_length == 512_000
    assert len(calls) == 2
    assert calls[0][2]
    assert calls[1][2] == {}


def test_cfg_custom_providers_resolved_from_cfg_dict():
    """The kwargs source must be the per-profile config (`_cfg`), not a
    module-level snapshot — otherwise profile switches with different
    custom_providers wouldn't take effect."""
    # The parsing lives in the shared model-context owner so session load,
    # terminal projection, and live SSE cannot drift.
    assert 'cfg.get("custom_providers")' in SESSION_MODELS_PY, (
        "_cfg_custom_providers must be sourced from `cfg.get('custom_providers')` "
        "(per-profile config) so profile-scoped custom_providers entries work."
    )
    assert 'cfg.get("model", {})' in SESSION_MODELS_PY, (
        "_cfg_ctx_len must be sourced from `cfg.get('model', {}).get('context_length')` "
        "(per-profile config) so profile-scoped model.context_length overrides work."
    )


# ── Sibling fallback in the session-query HTTP owner ────────────────────────

SESSION_QUERIES_PY = (
    REPO / "api" / "http" / "routes" / "session_queries.py"
).read_text(encoding="utf-8")


def test_routes_session_load_fallback_passes_config_overrides():
    """The session-query owner's fallback (around 'older sessions
    (pre-#1318) that have context_length=0 persisted') has the SAME bug shape
    as the streaming.py fallbacks: it called `_get_cl(model, "")` with no
    config overrides, so `/api/session/get` returned 256K for old sessions
    even when the user had `model.context_length: 1048576` set.

    The fix mirrors streaming.py's: pass config_context_length, provider,
    and custom_providers, with a TypeError fallback to the legacy 2-arg
    form. Without this, the very first paint of a reloaded old session shows
    the wrong window until a turn is sent.
    """
    # Anchor: find the comment that pins this fallback's purpose.
    anchor = "older sessions (pre-#1318) that have context_length=0 persisted"
    idx = SESSION_QUERIES_PY.find(anchor)
    assert idx != -1, "session-load fallback comment moved/removed"
    # The route block may delegate the resolver details to a helper, but the
    # session-load path must still call the helper and that helper must preserve
    # the same kwargs as the streaming.py fix.
    block_end = SESSION_QUERIES_PY.find("_session_tool_calls =", idx)
    assert block_end != -1, "session-load fallback block end not found after fallback comment"
    block = SESSION_QUERIES_PY[idx:block_end]
    helper_start = SESSION_MODELS_PY.find("def _resolve_context_length_for_session_model")
    assert helper_start != -1, "context-length resolver helper not found"
    helper_end = SESSION_MODELS_PY.find("\ndef ", helper_start + 1)
    helper = SESSION_MODELS_PY[
        helper_start:helper_end if helper_end != -1 else len(SESSION_MODELS_PY)
    ]
    assert "_resolve_context_length_for_session_model" in block
    assert "_should_accept_session_context_length_refresh" in block, (
        "session-load fallback must gate lower-confidence recomputes before "
        "replacing persisted context metadata. See #4248."
    )
    # Same kwargs as the streaming.py fix.
    assert "config_context_length=" in helper, (
        "session-load fallback must pass config_context_length= "
        "so user-set model.context_length wins over the 256K default. See #1896."
    )
    assert "provider=lookup.provider or provider or" in helper, (
        "session-load fallback must pass provider= "
        "so the registry lookup is provider-aware. See #1896."
    )
    assert "custom_providers=" in helper, (
        "session-load fallback must pass custom_providers= "
        "so the per-model override path applies. See #1896."
    )
    # Legacy fallback for older hermes-agent builds that pre-date the kwargs.
    assert "except TypeError:" in helper, (
        "session-load fallback must catch TypeError to support older "
        "hermes-agent builds without the new kwargs."
    )


def test_context_lookup_returns_custom_provider_api_key_from_entry():
    """#4059: static session hydration must carry custom-provider API keys.

    A named ``custom_providers`` entry can require auth for its ``/v1/models``
    endpoint. The route-side lookup helper already identifies the matching
    provider/base/model; it must also return that entry's API key so
    ``get_model_context_length`` does not probe anonymously and fall back to
    256K.
    """
    from api.routes import _context_length_lookup_inputs_for_model

    lookup = _context_length_lookup_inputs_for_model(
        "custom-model-id",
        "custom:llm-proxy",
        cfg={
            "custom_providers": [
                {
                    "name": "llm-proxy",
                    "base_url": "https://llm.example.test/v1",
                    "api_key": "sk-test-entry",
                    "model": "custom-model-id",
                }
            ]
        },
    )

    assert lookup.provider == "custom:llm-proxy"
    assert lookup.base_url == "https://llm.example.test/v1"
    assert lookup.api_key == "sk-test-entry"


def test_context_lookup_resolves_custom_provider_api_key_env_template(monkeypatch):
    """#4059: ``${ENV_VAR}`` custom-provider keys resolve before metadata probes."""
    from api.routes import _context_length_lookup_inputs_for_model

    monkeypatch.setenv("ISSUE_4059_CONTEXT_KEY", "env-template-key")

    lookup = _context_length_lookup_inputs_for_model(
        "custom-model-id",
        "custom:llm-proxy",
        cfg={
            "custom_providers": [
                {
                    "name": "llm-proxy",
                    "base_url": "https://llm.example.test/v1",
                    "api_key": "${ISSUE_4059_CONTEXT_KEY}",
                    "model": "custom-model-id",
                }
            ]
        },
    )

    assert lookup.api_key == "env-template-key"


def test_context_lookup_logs_unresolved_custom_provider_api_key_env_template(monkeypatch, caplog):
    """#4059: unresolved ``${ENV_VAR}`` keys get a DEBUG diagnostic.

    Behavior stays permissive: after logging the unresolved template, the helper
    still falls through to ``key_env`` and sanitized provider env lookup.
    """
    import logging

    from api.routes import _context_length_lookup_inputs_for_model

    monkeypatch.delenv("ISSUE_4059_MISSING_CONTEXT_KEY", raising=False)
    monkeypatch.setenv("ISSUE_4059_KEY_ENV_AFTER_TEMPLATE", "key-env-fallback")
    caplog.set_level(logging.DEBUG, logger="api.routes")

    lookup = _context_length_lookup_inputs_for_model(
        "custom-model-id",
        "custom:llm-proxy",
        cfg={
            "custom_providers": [
                {
                    "name": "llm-proxy",
                    "base_url": "https://llm.example.test/v1",
                    "api_key": "${ISSUE_4059_MISSING_CONTEXT_KEY}",
                    "key_env": "ISSUE_4059_KEY_ENV_AFTER_TEMPLATE",
                    "model": "custom-model-id",
                }
            ]
        },
    )

    assert lookup.api_key == "key-env-fallback"
    assert "${ISSUE_4059_MISSING_CONTEXT_KEY}" in caplog.text
    assert "unset or empty" in caplog.text


def test_context_lookup_resolves_custom_provider_key_env(monkeypatch):
    """#4059: ``key_env`` custom-provider keys resolve before metadata probes."""
    from api.routes import _context_length_lookup_inputs_for_model

    monkeypatch.setenv("ISSUE_4059_KEY_ENV", "key-env-value")

    lookup = _context_length_lookup_inputs_for_model(
        "custom-model-id",
        "custom:llm-proxy",
        cfg={
            "custom_providers": [
                {
                    "name": "llm-proxy",
                    "base_url": "https://llm.example.test/v1",
                    "key_env": "ISSUE_4059_KEY_ENV",
                    "model": "custom-model-id",
                }
            ]
        },
    )

    assert lookup.api_key == "key-env-value"


def test_routes_session_model_resolver_passes_custom_provider_api_key(monkeypatch):
    """#4059: ``_resolve_context_length_for_session_model`` passes api_key.

    This is the session-load/static-update path that was clobbering persisted
    500K context metadata back to the unauthenticated 256K fallback.
    """
    from api import config as cfg_mod
    from api import routes

    # Some unrelated route tests install ``sys.modules["agent"]`` as a plain
    # module stub at collection time. Make this regression test own a package-
    # shaped temporary ``agent.model_metadata`` import target so the production
    # helper's local import is order-independent.
    fake_agent = types.ModuleType("agent")
    fake_agent.__path__ = []
    metadata = types.ModuleType("agent.model_metadata")
    fake_agent.model_metadata = metadata  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "agent.model_metadata", metadata)

    seen = {}

    monkeypatch.setattr(
        cfg_mod,
        "get_config",
        lambda: {
            "custom_providers": [
                {
                    "name": "llm-proxy",
                    "base_url": "https://llm.example.test/v1",
                    "api_key": "sk-test-route",
                    "model": "custom-model-id",
                }
            ]
        },
    )

    def fake_get_model_context_length(model, base_url, **kwargs):
        seen.update(model=model, base_url=base_url, kwargs=kwargs)
        return 500_000 if kwargs.get("api_key") == "sk-test-route" else 256_000

    monkeypatch.setattr(metadata, "get_model_context_length", fake_get_model_context_length, raising=False)

    assert routes._resolve_context_length_for_session_model(
        "custom-model-id",
        "custom:llm-proxy",
    ) == 500_000
    assert seen["kwargs"]["api_key"] == "sk-test-route"


def test_terminal_context_projection_passes_custom_provider_api_key(monkeypatch):
    """#4059: the terminal projection probes custom-provider metadata with auth."""
    calls = []

    def resolver(model, base_url, **kwargs):
        calls.append((model, base_url, kwargs))
        return 500_000 if kwargs.get("api_key") == "sk-test-terminal" else 256_000

    _install_context_length_resolver(monkeypatch, resolver)
    projection = _projection(
        model="custom-model-id",
        provider="custom:llm-proxy",
        api_key="",
        config={
            "custom_providers": [
                {
                    "name": "llm-proxy",
                    "base_url": "https://llm.example.test/v1",
                    "api_key": "sk-test-terminal",
                    "model": "custom-model-id",
                }
            ]
        },
    )

    assert projection.context_length == 500_000
    assert calls[-1][2]["api_key"] == "sk-test-terminal"
