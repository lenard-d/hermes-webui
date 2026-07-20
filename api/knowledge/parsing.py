"""Pure parsing and normalization for configured knowledge sources."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

JOPLIN_AI_RECALL_NOTE_PRIORITY = (
    ("CURRENT_CONTEXT_ID", "Current Context"),
    ("OPEN_ISSUES_ID", "Open Issues"),
    ("AGENT_MEMORY_ID", "Agent Memory"),
    ("CONVENTIONS_ID", "Conventions / Preferences"),
    ("INFRA_ID", "Infrastructure"),
    ("SERVICES_ID", "Services"),
)


def note_snippet(body: str, query: str = "", *, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", str(body or "")).strip()
    if not text:
        return ""
    normalized_query = str(query or "").strip().lower()
    if normalized_query:
        index = text.lower().find(normalized_query)
        if index > 40:
            text = "…" + text[max(0, index - 60) :]
    if len(text) > limit:
        return text[:limit].rstrip() + "…"
    return text


def script_path_from_config_value(path_value) -> Path | None:
    """Return the recall script path from a plain path or argv-style hook."""
    if not path_value:
        return None
    try:
        if isinstance(path_value, (list, tuple)):
            candidates = [str(part).strip() for part in path_value if str(part).strip()]
        else:
            raw = str(path_value).strip()
            raw_path = Path(raw).expanduser()
            if raw and raw_path.exists():
                return raw_path
            candidates = shlex.split(raw)
        for candidate in candidates:
            if candidate.endswith((".py", ".sh", ".bash")):
                return Path(candidate).expanduser()
        return Path(candidates[-1]).expanduser() if candidates else None
    except Exception:
        return None


def parse_joplin_recall_refs(text: str) -> list[dict]:
    """Parse stable, allowlisted Joplin note IDs from recall-script source."""
    constants = {
        match.group(1): match.group(2)
        for match in re.finditer(
            r'(?m)^\s*([A-Z0-9_]+_ID)\s*=\s*["\']([A-Fa-f0-9]{16,64})["\']',
            str(text or ""),
        )
    }
    refs: list[dict] = []
    seen: set[str] = set()
    for constant_name, label in JOPLIN_AI_RECALL_NOTE_PRIORITY:
        note_id = constants.get(constant_name)
        if not note_id or note_id in seen:
            continue
        seen.add(note_id)
        refs.append(
            {
                "id": note_id,
                "label": label,
                "constant": constant_name,
                "used_by": "ai_prefill",
                "used_reason": "automatic_recall",
            }
        )
    return refs
