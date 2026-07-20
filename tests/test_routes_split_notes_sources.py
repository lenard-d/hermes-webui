"""Architecture checks for the knowledge-backed notes HTTP adapter."""

from __future__ import annotations

from pathlib import Path

import api.knowledge as knowledge
import api.routes as routes
from api.routes_parts import notes_sources

EXPECTED_EXPORTS = {
    "_webui_truthy",
    "_external_notes_sources_enabled",
    "_NOTES_SOURCE_SERVER_HINTS",
    "_NOTES_SOURCE_TOOL_HINTS",
    "_NOTES_SOURCE_CONFIGURED_TOOL_HINTS",
    "_note_source_label",
    "_looks_like_notes_source",
    "_configured_note_tool_hints",
    "_notes_sources_from_mcp_inventory",
    "_handle_notes_sources_list",
    "_notes_configured_server",
    "_joplin_connection_from_config",
    "_joplin_api_get",
    "_note_snippet",
    "_joplin_search_notes",
    "_joplin_get_note",
    "_JOPLIN_AI_RECALL_NOTE_PRIORITY",
    "_script_path_from_config_value",
    "_joplin_prefill_script_path",
    "_joplin_recall_note_refs",
    "_joplin_recent_ai_notes",
    "_handle_notes_search",
    "_handle_notes_item",
}


def test_notes_adapter_preserves_the_legacy_route_exports_without_rebinding():
    assert set(notes_sources.__routes_exports__) == EXPECTED_EXPORTS
    for name in EXPECTED_EXPORTS:
        assert getattr(routes, name) is getattr(notes_sources, name)
    assert notes_sources._handle_notes_search.__module__ == "api.routes_parts.notes_sources"
    assert notes_sources._handle_notes_search.__globals__ is vars(notes_sources)


def test_notes_domain_interface_owns_discovery_search_and_parsing():
    assert routes._notes_sources_from_mcp_inventory is knowledge.discover_note_sources
    assert routes._note_snippet is knowledge.note_snippet
    assert routes._script_path_from_config_value is knowledge.script_path_from_config_value
    assert routes._joplin_search_notes is knowledge.search_joplin_notes
    assert routes._joplin_get_note is knowledge.get_joplin_note


def test_notes_knowledge_modules_do_not_import_route_facades():
    package_dir = Path(knowledge.__file__).parent
    for path in package_dir.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "api.routes" not in source
        assert "routes_parts" not in source
        assert "sys.modules" not in source


def test_notes_adapter_is_transport_only_and_file_backed():
    source = Path(notes_sources.__file__).read_text(encoding="utf-8")
    assert "def _handle_notes_sources_list(" in source
    assert "def _handle_notes_search(" in source
    assert "def _handle_notes_item(" in source
    assert "urlopen(" not in source
    assert "read_text(" not in source
    assert "exec(" not in source


def test_script_path_parser_stays_available_through_route_compatibility():
    assert routes._script_path_from_config_value("python /tmp/recall.py") == Path(
        "/tmp/recall.py"
    )


def test_active_knowledge_config_is_resolved_from_the_request_profile(monkeypatch, tmp_path):
    from api import config, profiles

    profile_home = tmp_path / "profiles" / "research"
    expected = {"mcp_servers": {"joplin": {"enabled": True}}}
    captured = []
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: profile_home)
    monkeypatch.setattr(
        config,
        "get_config_for_profile_home",
        lambda home: captured.append(Path(home)) or expected,
    )

    assert knowledge.active_config_snapshot() is expected
    assert captured == [profile_home]


def test_notes_list_adapter_projects_runtime_inventory_without_route_globals(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        notes_sources.knowledge,
        "active_config_snapshot",
        lambda: {
            "webui_external_notes_sources": True,
            "mcp_servers": {"joplin": {"enabled": True, "command": "joplin"}},
        },
    )
    monkeypatch.setattr(
        notes_sources,
        "_mcp_runtime_status_by_name",
        lambda: {"joplin": {"connected": True, "tools": 1}},
    )
    monkeypatch.setattr(
        notes_sources,
        "_mcp_tools_from_runtime_status",
        lambda runtime, summaries: [
            {
                "server": "joplin",
                "name": "search_notes",
                "description": "Search notes token=secret-placeholder",
            }
        ],
    )
    monkeypatch.setattr(notes_sources.knowledge, "recent_ai_notes", lambda **kwargs: [])
    monkeypatch.setattr(
        notes_sources,
        "j",
        lambda handler, payload, **kwargs: captured.update(payload) or payload,
    )

    notes_sources._handle_notes_sources_list(object())

    assert captured["source"] == "mcp_runtime_status"
    assert captured["sources"][0]["name"] == "joplin"
    assert "secret-placeholder" not in repr(captured["sources"])
