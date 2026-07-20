"""Source-discovery policy and private-safe MCP inventory projection."""

from __future__ import annotations

import re
from collections.abc import Mapping

from api.helpers import _redact_text

NOTES_SOURCE_SERVER_HINTS = frozenset(
    {
        "joplin",
        "obsidian",
        "notion",
        "llm-wiki",
        "llmwiki",
        "wiki",
        "notes",
        "note",
        "knowledge",
        "kb",
        "readwise",
        "logseq",
    }
)
NOTES_SOURCE_TOOL_HINTS = frozenset(
    {
        "note",
        "notes",
        "notebook",
        "page",
        "pages",
        "wiki",
        "knowledge",
        "search_notes",
        "get_note",
        "list_notes",
        "read_note",
    }
)
NOTES_SOURCE_CONFIGURED_TOOL_HINTS = {
    "joplin": (
        {"name": "search_notes", "description": "Search Joplin notes by keyword."},
        {"name": "list_notes", "description": "List notes from a Joplin notebook."},
        {"name": "get_note", "description": "Read a specific Joplin note by ID."},
    ),
    "obsidian": (
        {"name": "search_notes", "description": "Search Obsidian notes by keyword."},
        {"name": "read_note", "description": "Read a specific Obsidian note or file."},
    ),
    "notion": (
        {"name": "search_pages", "description": "Search Notion pages or databases."},
        {"name": "get_page", "description": "Read a specific Notion page."},
    ),
    "llm-wiki": (
        {"name": "query_knowledge_base", "description": "Query the LLM Wiki knowledge base."},
        {"name": "read_page", "description": "Read a specific wiki page."},
    ),
    "llmwiki": (
        {"name": "query_knowledge_base", "description": "Query the LLM Wiki knowledge base."},
        {"name": "read_page", "description": "Read a specific wiki page."},
    ),
}


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def external_notes_sources_enabled(
    config_data: Mapping | None,
    *,
    environ: Mapping[str, str],
) -> bool:
    """Apply the default-off notes-drawer policy to one resolved config snapshot."""
    env_value = environ.get("HERMES_WEBUI_EXTERNAL_NOTES_SOURCES", "")
    if env_value:
        return _truthy(env_value)
    if not isinstance(config_data, Mapping):
        return False
    return _truthy(
        config_data.get("webui_external_notes_sources")
        or config_data.get("external_notes_sources")
        or config_data.get("notes_sources_drawer")
    )


def _safe_display_text(value, *, limit: int) -> str:
    if not isinstance(value, str):
        value = "" if value is None else str(value)
    value = _redact_text(value).strip()
    value = re.sub(
        r"Authorization:\s*Bearer\s+\S+",
        "[REDACTED CREDENTIAL]",
        value,
        flags=re.I,
    )
    if len(value) > limit:
        value = value[: max(0, limit - 1)].rstrip() + "…"
    return value


def note_source_label(name: str) -> str:
    labels = {
        "joplin": "Joplin",
        "obsidian": "Obsidian",
        "notion": "Notion",
        "llm-wiki": "LLM Wiki",
        "llmwiki": "LLM Wiki",
        "readwise": "Readwise",
        "logseq": "Logseq",
    }
    lowered = str(name or "").strip().lower()
    return labels.get(lowered, str(name or "").replace("_", " ").replace("-", " ").title())


def looks_like_notes_source(server_name: str, tool_rows: list[dict]) -> bool:
    server_lower = str(server_name or "").lower()
    if any(hint in server_lower for hint in NOTES_SOURCE_SERVER_HINTS):
        return True
    for tool in tool_rows:
        haystack = " ".join(
            [str(tool.get("name") or ""), str(tool.get("description") or "")]
        ).lower()
        if any(hint in haystack for hint in NOTES_SOURCE_TOOL_HINTS):
            return True
    return False


def configured_note_tool_hints(server_name: str) -> list[dict]:
    server_lower = str(server_name or "").strip().lower()
    hints = NOTES_SOURCE_CONFIGURED_TOOL_HINTS.get(server_lower)
    if hints is None:
        if any(hint in server_lower for hint in ("wiki", "knowledge", "kb")):
            hints = (
                {"name": "search", "description": "Search this configured knowledge source."},
                {"name": "read", "description": "Read an item from this configured knowledge source."},
            )
        elif any(hint in server_lower for hint in ("note", "notes")):
            hints = (
                {"name": "search_notes", "description": "Search this configured notes source."},
                {"name": "read_note", "description": "Read a note from this configured notes source."},
            )
        else:
            hints = ()
    return [
        {
            "name": _safe_display_text(row.get("name") or "", limit=96),
            "description": _safe_display_text(row.get("description") or "", limit=180),
            "inferred": True,
        }
        for row in hints
        if isinstance(row, dict)
    ]


def discover_note_sources(server_summaries: Mapping, tools: list[dict]) -> list[dict]:
    """Build a redacted source inventory from one already-known MCP snapshot."""
    by_server: dict[str, list[dict]] = {}
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        server = str(tool.get("server") or "").strip()
        if server:
            by_server.setdefault(server, []).append(tool)

    if isinstance(server_summaries, Mapping):
        for server in server_summaries:
            server_name = str(server or "").strip()
            if server_name and server_name not in by_server and looks_like_notes_source(server_name, []):
                by_server[server_name] = []

    sources = []
    for server, tool_rows in by_server.items():
        if not looks_like_notes_source(server, tool_rows):
            continue
        summary = (
            server_summaries.get(server, {"name": server})
            if isinstance(server_summaries, Mapping)
            else {"name": server}
        )
        safe_tools = []
        tool_source = "runtime"
        for tool in tool_rows[:8]:
            description = _safe_display_text(tool.get("description") or "", limit=180)
            description = re.sub(
                r"(?i)\b(api[_-]?key|token|password|secret)\s*[:=]\s*\S+",
                "[REDACTED]",
                description,
            )
            safe_tools.append(
                {
                    "name": _safe_display_text(tool.get("name") or "", limit=96),
                    "description": description,
                }
            )
        if not safe_tools:
            safe_tools = configured_note_tool_hints(server)
            if safe_tools:
                tool_source = "configured_hint"
        sources.append(
            {
                "name": server,
                "label": note_source_label(server),
                "enabled": bool(summary.get("enabled", True)),
                "active": bool(summary.get("active")),
                "status": summary.get("status") or "unknown",
                "tool_count": len(safe_tools),
                "tool_source": tool_source,
                "tools": safe_tools,
            }
        )
    sources.sort(key=lambda row: (not row.get("active"), row.get("label", "")))
    return sources
