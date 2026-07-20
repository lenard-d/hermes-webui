"""Thin HTTP adapters and legacy route exports for :mod:`api.knowledge`."""

from __future__ import annotations

import os
from urllib.parse import parse_qs

from api import knowledge
from api.helpers import j
from api.knowledge import joplin as _joplin
from api.knowledge import parsing as _parsing
from api.knowledge import sources as _sources
from api.routes_parts.mcp_inventory import (
    _mcp_runtime_status_by_name,
    _mcp_tools_from_registry,
    _mcp_tools_from_runtime_status,
    _server_summary,
)

_webui_truthy = _sources._truthy
_NOTES_SOURCE_SERVER_HINTS = _sources.NOTES_SOURCE_SERVER_HINTS
_NOTES_SOURCE_TOOL_HINTS = _sources.NOTES_SOURCE_TOOL_HINTS
_NOTES_SOURCE_CONFIGURED_TOOL_HINTS = _sources.NOTES_SOURCE_CONFIGURED_TOOL_HINTS
_note_source_label = knowledge.note_source_label
_looks_like_notes_source = knowledge.looks_like_notes_source
_configured_note_tool_hints = knowledge.configured_note_tool_hints
_notes_sources_from_mcp_inventory = knowledge.discover_note_sources
_note_snippet = knowledge.note_snippet
_JOPLIN_AI_RECALL_NOTE_PRIORITY = _parsing.JOPLIN_AI_RECALL_NOTE_PRIORITY
_script_path_from_config_value = knowledge.script_path_from_config_value
_joplin_search_notes = knowledge.search_joplin_notes
_joplin_get_note = knowledge.get_joplin_note
_joplin_prefill_script_path = _joplin.prefill_script_path
_joplin_recall_note_refs = _joplin.recall_note_refs
_joplin_recent_ai_notes = knowledge.recent_ai_notes


def _external_notes_sources_enabled(config_data: dict | None = None) -> bool:
    config = config_data if isinstance(config_data, dict) else knowledge.active_config_snapshot()
    return knowledge.external_notes_sources_enabled(config, environ=os.environ)


def _notes_configured_server(source: str) -> dict:
    from api.knowledge.adapters import configured_server

    return configured_server(knowledge.active_config_snapshot(), source)


def _joplin_connection_from_config() -> tuple[str, str]:
    return knowledge.joplin_connection(knowledge.active_config_snapshot(), environ=os.environ)


def _joplin_api_get(path: str, params: dict | None = None) -> dict:
    base_url, token = _joplin_connection_from_config()
    return knowledge.JoplinHTTPAdapter(base_url, token).get(path, params)


def _handle_notes_sources_list(handler):
    """Translate source inventory state to the legacy HTTP payload."""
    config = knowledge.active_config_snapshot()
    if not knowledge.external_notes_sources_enabled(config, environ=os.environ):
        return j(
            handler,
            {
                "enabled": False,
                "sources": [],
                "source": "disabled",
                "inventory_scope": "disabled_by_default",
                "attach_supported": False,
                "automatic_recall_unchanged": True,
                "recent_ai_notes": [],
            },
        )
    servers = config.get("mcp_servers", {})
    if not isinstance(servers, dict):
        servers = {}
    runtime = _mcp_runtime_status_by_name()
    summaries = {
        str(name): _server_summary(str(name), server_config, runtime.get(str(name)))
        for name, server_config in servers.items()
    }
    tools = _mcp_tools_from_runtime_status(runtime, summaries)
    source = "mcp_runtime_status"
    if not tools:
        tools = _mcp_tools_from_registry(summaries)
        source = "tool_registry" if tools else "none"
    return j(
        handler,
        {
            "enabled": True,
            "sources": knowledge.discover_note_sources(summaries, tools),
            "source": source,
            "inventory_scope": "already_known_runtime_only",
            "attach_supported": False,
            "automatic_recall_unchanged": True,
            "recent_ai_notes": knowledge.recent_ai_notes(limit=6, config=config),
        },
    )


def _handle_notes_search(handler, parsed):
    config = knowledge.active_config_snapshot()
    if not knowledge.external_notes_sources_enabled(config, environ=os.environ):
        return j(
            handler,
            {
                "source": "disabled",
                "results": [],
                "error": "External notes sources are disabled.",
            },
            status=404,
        )
    query = parse_qs(parsed.query or "")
    source = str(query.get("source", ["joplin"])[0] or "joplin").strip().lower()
    search_query = str(query.get("q", [""])[0] or "").strip()
    try:
        limit = int(query.get("limit", ["20"])[0] or 20)
    except Exception:
        limit = 20
    if source != "joplin":
        return j(
            handler,
            {
                "source": source,
                "results": [],
                "error": "Search is currently implemented for Joplin sources only.",
            },
            status=400,
        )
    try:
        results = knowledge.search_joplin_notes(search_query, limit=limit, config=config)
        return j(handler, {"source": "joplin", "query": search_query, "results": results})
    except ValueError as exc:
        return j(
            handler,
            {"source": "joplin", "query": search_query, "results": [], "error": str(exc)},
            status=502,
        )


def _handle_notes_item(handler, parsed):
    config = knowledge.active_config_snapshot()
    if not knowledge.external_notes_sources_enabled(config, environ=os.environ):
        return j(
            handler,
            {"source": "disabled", "error": "External notes sources are disabled."},
            status=404,
        )
    query = parse_qs(parsed.query or "")
    source = str(query.get("source", ["joplin"])[0] or "joplin").strip().lower()
    note_id = str(query.get("id", [""])[0] or "").strip()
    if source != "joplin":
        return j(
            handler,
            {"source": source, "error": "Preview is currently implemented for Joplin sources only."},
            status=400,
        )
    try:
        return j(
            handler,
            {"source": "joplin", "note": knowledge.get_joplin_note(note_id, config=config)},
        )
    except ValueError as exc:
        return j(handler, {"source": "joplin", "error": str(exc)}, status=502)


__routes_exports__ = (
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
)
