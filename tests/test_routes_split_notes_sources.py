"""Compatibility checks for the external-notes route-domain extraction."""

from pathlib import Path

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


def test_notes_part_declares_the_complete_coherent_export_surface():
    assert set(notes_sources.__routes_exports__) == EXPECTED_EXPORTS


def test_notes_exports_remain_owned_by_routes_facade():
    for name in EXPECTED_EXPORTS:
        value = getattr(routes, name)
        if callable(value):
            assert value.__module__ == "api.routes"
            assert value.__globals__ is vars(routes)
        else:
            assert value is getattr(notes_sources, name)


def test_joplin_search_keeps_routes_monkeypatch_seams(monkeypatch):
    calls = []

    monkeypatch.setattr(
        routes,
        "_joplin_api_get",
        lambda path, params: calls.append((path, params))
        or {
            "items": [
                {
                    "id": "abc123def4567890",
                    "title": "Patched result",
                    "body": "A matching Hermes note",
                    "parent_id": "parent",
                    "updated_time": 42,
                }
            ]
        },
    )

    [result] = routes._joplin_search_notes("Hermes", limit=3)

    assert calls == [
        (
            "/search",
            {
                "query": "Hermes",
                "type": "note",
                "fields": "id,title,body,parent_id,updated_time",
                "limit": 3,
            },
        )
    ]
    assert result["title"] == "Patched result"


def test_script_path_parser_resolves_shlex_through_routes_facade():
    assert routes._script_path_from_config_value("python /tmp/recall.py") == Path(
        "/tmp/recall.py"
    )


def test_notes_implementation_is_file_backed_and_mcp_crud_stays_in_facade():
    part_source = Path(notes_sources.__file__).read_text(encoding="utf-8")
    facade_source = Path(routes.__file__).read_text(encoding="utf-8")

    assert "def _handle_notes_item(" in part_source
    assert "def _webui_truthy(" not in facade_source
    assert "def _handle_notes_item(" not in facade_source
    assert "def _handle_mcp_servers_list(" in facade_source
    assert "def _handle_mcp_servers_list(" not in part_source
    assert "exec(" not in part_source
