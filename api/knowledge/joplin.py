"""Joplin search, preview, and configured-recall behavior."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from api.helpers import _redact_text

from .adapters import (
    JoplinHTTPAdapter,
    KnowledgeFilesystemAdapter,
    active_config_snapshot,
)
from .parsing import note_snippet, parse_joplin_recall_refs, script_path_from_config_value
from .sources import _safe_display_text


def _resolved_adapter(
    adapter: JoplinHTTPAdapter | None,
    config: Mapping | None,
) -> JoplinHTTPAdapter:
    return adapter or JoplinHTTPAdapter.from_active_profile(config)


def search_joplin_notes(
    query: str,
    *,
    limit: int = 20,
    adapter: JoplinHTTPAdapter | None = None,
    config: Mapping | None = None,
) -> list[dict]:
    query = str(query or "").strip()
    if not query:
        return []
    limit = max(1, min(int(limit or 20), 50))
    data = _resolved_adapter(adapter, config).get(
        "/search",
        {
            "query": query,
            "type": "note",
            "fields": "id,title,body,parent_id,updated_time",
            "limit": limit,
        },
    )
    rows = data.get("items") if isinstance(data, dict) else []
    results = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        note_id = _safe_display_text(row.get("id") or "", limit=64)
        if not note_id:
            continue
        results.append(
            {
                "id": note_id,
                "title": _safe_display_text(row.get("title") or "Untitled", limit=180),
                "snippet": _safe_display_text(
                    note_snippet(str(row.get("body") or ""), query), limit=260
                ),
                "parent_id": _safe_display_text(row.get("parent_id") or "", limit=64),
                "updated_time": row.get("updated_time"),
                "source": "joplin",
            }
        )
    return results


def get_joplin_note(
    note_id: str,
    *,
    adapter: JoplinHTTPAdapter | None = None,
    config: Mapping | None = None,
) -> dict:
    note_id = str(note_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{16,64}", note_id):
        raise ValueError("Invalid Joplin note id")
    data = _resolved_adapter(adapter, config).get(
        f"/notes/{note_id}",
        {"fields": "id,title,body,parent_id,updated_time,created_time"},
    )
    if not data.get("id"):
        raise ValueError("Joplin note not found")
    body = str(data.get("body") or "")
    if len(body) > 50_000:
        body = body[:50_000].rstrip() + "\n\n[Preview truncated at 50,000 characters]"
    return {
        "id": _safe_display_text(data.get("id") or "", limit=64),
        "title": _safe_display_text(data.get("title") or "Untitled", limit=180),
        "body": _redact_text(body),
        "parent_id": _safe_display_text(data.get("parent_id") or "", limit=64),
        "updated_time": data.get("updated_time"),
        "created_time": data.get("created_time"),
        "source": "joplin",
    }


def prefill_script_path(
    config: Mapping | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    resolved_config = config if isinstance(config, Mapping) else active_config_snapshot()
    resolved_environ = environ if isinstance(environ, Mapping) else os.environ
    return script_path_from_config_value(
        resolved_environ.get("HERMES_WEBUI_PREFILL_MESSAGES_SCRIPT", "")
        or resolved_config.get("webui_prefill_messages_script")
        or resolved_config.get("prefill_messages_script")
    )


def recall_note_refs(
    script_path: Path | None = None,
    *,
    config: Mapping | None = None,
    filesystem: KnowledgeFilesystemAdapter | None = None,
) -> list[dict]:
    resolved_path = script_path or prefill_script_path(config)
    if not resolved_path:
        return []
    source = (filesystem or KnowledgeFilesystemAdapter()).read_command_source(resolved_path)
    return parse_joplin_recall_refs(source) if source is not None else []


def recent_ai_notes(
    *,
    limit: int = 6,
    adapter: JoplinHTTPAdapter | None = None,
    config: Mapping | None = None,
    filesystem: KnowledgeFilesystemAdapter | None = None,
) -> list[dict]:
    try:
        limit = max(1, min(int(limit or 6), 20))
    except Exception:
        limit = 6
    resolved_config = config if isinstance(config, Mapping) else active_config_snapshot()
    resolved_adapter = _resolved_adapter(adapter, resolved_config)
    notes = []
    for ref in recall_note_refs(config=resolved_config, filesystem=filesystem)[:limit]:
        try:
            data = resolved_adapter.get(
                f"/notes/{ref['id']}",
                {"fields": "id,title,parent_id,updated_time,user_updated_time,created_time"},
            )
        except Exception:
            continue
        note_id = _safe_display_text(data.get("id") or ref.get("id") or "", limit=64)
        if not note_id:
            continue
        notes.append(
            {
                "id": note_id,
                "title": _safe_display_text(
                    data.get("title") or ref.get("label") or "Untitled", limit=180
                ),
                "label": _safe_display_text(ref.get("label") or "", limit=120),
                "parent_id": _safe_display_text(data.get("parent_id") or "", limit=64),
                "updated_time": data.get("user_updated_time") or data.get("updated_time"),
                "created_time": data.get("created_time"),
                "source": "joplin",
                "used_by": ref.get("used_by") or "ai_prefill",
                "used_reason": ref.get("used_reason") or "automatic_recall",
            }
        )
    return notes
