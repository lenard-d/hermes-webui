"""Regression coverage for issue #4087, WebUI skill bundle slash parity."""

from tests.frontend_asset_contract import family_source

from contextlib import contextmanager, nullcontext
from types import ModuleType, SimpleNamespace

import pytest

import api.commands as commands


COMMANDS_JS = family_source("commands")
MESSAGES_JS = family_source("messages")


def _install_fake_skill_bundles(monkeypatch, *, bundles=None, resolver=None, builder=None):
    import sys

    agent_pkg = sys.modules.get("agent") or ModuleType("agent")
    # `sys.modules.get(...)` returns the REAL agent package; emptying its
    # __path__ in place strands it for the rest of the suite (later
    # `from agent.<sub> import ...` — e.g. hermes_state's `agent.memory_manager`
    # — then fails). monkeypatch.setattr snapshots and restores it on teardown.
    monkeypatch.setattr(agent_pkg, "__path__", [], raising=False)
    skill_bundles = ModuleType("agent.skill_bundles")
    skill_bundles.list_bundles = lambda: list(bundles or [])
    skill_bundles.resolve_bundle_command_key = resolver or (lambda name: None)
    skill_bundles.build_bundle_invocation_message = builder or (lambda key, args: None)
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.skill_bundles", skill_bundles)


def test_bundle_routes_are_wired_through_dedicated_endpoints(monkeypatch):
    import api.routes as route_facade
    from api.http.routes import automation_mutations, workspace_queries

    bundle_metadata = [{"name": "incident-review", "source": "bundle"}]
    resolved = {
        "name": "incident-review",
        "source": "bundle",
        "message": "$incident-review check alerts",
    }
    resolved_commands = []
    monkeypatch.setattr(commands, "list_command_bundles", lambda: bundle_metadata)
    monkeypatch.setattr(
        commands,
        "resolve_bundle_command",
        lambda command: resolved_commands.append(command) or resolved,
    )
    ctx = dict(vars(route_facade))
    ctx["j"] = lambda _handler, payload, status=200, **_kwargs: {
        "payload": payload,
        "status": status,
    }

    listed = workspace_queries.handle_get(
        object(),
        SimpleNamespace(path="/api/commands/bundles", query=""),
        ctx,
    )
    resolved_response = automation_mutations.handle_post(
        object(),
        SimpleNamespace(path="/api/commands/bundles/resolve"),
        {"command": "/incident-review check alerts"},
        None,
        ctx,
    )

    assert listed == {"payload": {"bundles": bundle_metadata}, "status": 200}
    assert resolved_response == {"payload": resolved, "status": 200}
    assert resolved_commands == ["/incident-review check alerts"]


def test_frontend_bundle_dispatch_uses_dedicated_metadata_and_resolve_calls():
    assert "api('/api/commands/bundles')" in COMMANDS_JS
    assert "api('/api/commands/bundles/resolve'" in COMMANDS_JS
    assert "await loadAgentCommandMetadata();" in COMMANDS_JS
    assert "const _bundleCmd=!_agentCmd&&typeof getBundleCommandMetadata==='function'" in MESSAGES_JS
    assert "await resolveBundleCommand(text,_bundleCmd)" in MESSAGES_JS


def test_frontend_checks_agent_ownership_before_bundle_resolution():
    agent_idx = MESSAGES_JS.find("await getAgentCommandMetadata(_parsedCmd.name)")
    bundle_idx = MESSAGES_JS.find("await getBundleCommandMetadata(_parsedCmd.name)")
    assert agent_idx != -1
    assert bundle_idx != -1
    assert agent_idx < bundle_idx


def test_list_command_bundles_returns_bundle_metadata(monkeypatch):
    seen = {}

    @contextmanager
    def _profile_scope(purpose):
        seen["purpose"] = purpose
        yield

    _install_fake_skill_bundles(
        monkeypatch,
        bundles=[
            {
                "slug": "incident-review",
                "description": "Investigate incidents with the bundled workflow",
                "skills": ["triage", "report"],
            },
            {
                "slug": "",
                "description": "ignored",
                "skills": ["missing-slug"],
            },
        ],
    )
    monkeypatch.setattr(commands, "_bundle_profile_context", _profile_scope)

    assert commands.list_command_bundles() == [
        {
            "name": "incident-review",
            "description": "Investigate incidents with the bundled workflow",
            "skill_count": 2,
            "source": "bundle",
        }
    ]
    assert seen == {"purpose": "/api/commands/bundles"}


def test_resolve_bundle_command_uses_bundle_runtime(monkeypatch):
    seen = {}

    @contextmanager
    def _profile_scope(purpose):
        seen["purpose"] = purpose
        yield

    def _resolve(name):
        seen["resolve_name"] = name
        return "/incident-review" if name == "incident-review" else None

    def _build(key, args):
        seen["build"] = (key, args)
        return ("$incident review the primary alerts", ["triage", "report"], [])

    _install_fake_skill_bundles(monkeypatch, resolver=_resolve, builder=_build)
    monkeypatch.setattr(commands, "_bundle_profile_context", _profile_scope)

    assert commands.resolve_bundle_command("/incident-review the primary alerts") == {
        "name": "incident-review",
        "source": "bundle",
        "message": "$incident review the primary alerts",
        "loaded_skills": ["triage", "report"],
        "missing_skills": [],
    }
    assert seen == {
        "purpose": "/api/commands/bundles/resolve",
        "resolve_name": "incident-review",
        "build": ("/incident-review", "the primary alerts"),
    }


def test_resolve_bundle_command_raises_for_unknown_bundle(monkeypatch):
    _install_fake_skill_bundles(monkeypatch)
    monkeypatch.setattr(commands, "_bundle_profile_context", lambda purpose: nullcontext())

    with pytest.raises(KeyError):
        commands.resolve_bundle_command("/does-not-exist investigate this")


def test_resolve_bundle_command_wraps_unexpected_runtime_errors(monkeypatch):
    def _explode(_name):
        raise AttributeError("bundle runtime broke")

    _install_fake_skill_bundles(monkeypatch, resolver=_explode)
    monkeypatch.setattr(commands, "_bundle_profile_context", lambda purpose: nullcontext())

    with pytest.raises(RuntimeError, match="Skill bundle command unavailable"):
        commands.resolve_bundle_command("/incident-review investigate this")
