"""Read-only memory files and effective project-context projection."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import parse_qs

from api import knowledge
from api.helpers import j, redact_text
from api.sessions import get_session
from api.workspace import get_last_workspace, resolve_trusted_workspace


logger = logging.getLogger(__name__)

_HERMES_NAMES = (".hermes.md", "HERMES.md")
_CWD_NAMES = (
    "AGENTS.md",
    "agents.md",
    "CLAUDE.md",
    "claude.md",
    ".cursorrules",
)
_CURSOR_RULES_GLOB = ".cursor/rules/*.mdc"
_MAX_BYTES = 20_000


def strip_frontmatter(content: str) -> str:
    """Strip the leading YAML block omitted by the agent's context loader."""
    if not content.startswith("---"):
        return content
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return content
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            return "".join(lines[index + 1 :]).lstrip("\n")
    return content


def git_root(start: Path) -> Path | None:
    current = start.resolve()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def candidates(workspace: Path) -> list[Path]:
    """Return project-context candidates in the agent's effective priority."""
    current = workspace.resolve()
    context_candidates: list[Path] = []
    repository_root = git_root(current)
    stop_at = repository_root if repository_root is not None else current
    for directory in [current, *current.parents]:
        for name in _HERMES_NAMES:
            context_candidates.append(directory / name)
        if directory == stop_at:
            break
    for name in _CWD_NAMES:
        context_candidates.append(current / name)
    try:
        context_candidates.extend(sorted(current.glob(_CURSOR_RULES_GLOB)))
    except OSError:
        pass
    return context_candidates


def workspace_for_request(parsed) -> Path | None:
    query = parse_qs(parsed.query or "") if parsed is not None else {}
    session_id = query.get("session_id", [""])[0]
    if session_id:
        try:
            workspace = (get_session(session_id).workspace or "").strip()
            if not workspace:
                return None
            return Path(workspace).expanduser().resolve()
        except Exception:
            return None

    raw_workspace = (
        query.get("workspace", [""])[0]
        or os.environ.get("TERMINAL_CWD", "")
        or get_last_workspace()
    )
    if not raw_workspace:
        return None
    try:
        return Path(resolve_trusted_workspace(raw_workspace)).expanduser().resolve()
    except Exception:
        logger.debug(
            "Skipping project context for untrusted workspace %s",
            raw_workspace,
            exc_info=True,
        )
        return None


def read_active(workspace: Path | None) -> dict:
    payload = {
        "content": "",
        "path": "",
        "mtime": None,
        "workspace": str(workspace) if workspace else "",
        "shadowed": [],
    }
    if not workspace:
        return payload
    try:
        if not workspace.exists() or not workspace.is_dir():
            return payload
    except OSError:
        return payload

    seen: set[str] = set()
    readable: list[dict] = []
    for candidate in candidates(workspace):
        try:
            if not candidate.is_file():
                continue
            resolved = candidate.resolve()
            key = os.path.normcase(str(resolved)).casefold()
            if key in seen:
                continue
            seen.add(key)
            content = resolved.read_text(encoding="utf-8", errors="replace")
            content = strip_frontmatter(content)
            if len(content) > _MAX_BYTES:
                content = content[:_MAX_BYTES]
            if not content.strip():
                continue
            readable.append(
                {
                    "name": resolved.name,
                    "path": str(resolved),
                    "content": content,
                    "mtime": resolved.stat().st_mtime,
                }
            )
        except Exception:
            logger.debug(
                "Could not read project context candidate %s",
                candidate,
                exc_info=True,
            )
    if not readable:
        return payload

    active = readable[0]
    payload.update(
        {
            "content": active["content"],
            "path": active["path"],
            "mtime": active["mtime"],
            "name": active["name"],
            "shadowed": [
                {
                    "name": item["name"],
                    "path": item["path"],
                    "mtime": item["mtime"],
                    "shadowed_by": active["name"],
                    "shadowed_by_path": active["path"],
                }
                for item in readable[1:]
            ],
        }
    )
    return payload


def handle_memory_read(handler, parsed=None):
    try:
        from api.profiles import get_active_hermes_home

        home = get_active_hermes_home()
        memory_dir = home / "memories"
    except ImportError:
        home = Path.home() / ".hermes"
        memory_dir = home / "memories"
    memory_file = memory_dir / "MEMORY.md"
    user_file = memory_dir / "USER.md"
    soul_file = home / "SOUL.md"

    def read(path: Path) -> str:
        return (
            path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        )

    project_context = read_active(workspace_for_request(parsed))
    external_notes_enabled = knowledge.external_notes_sources_enabled(
        knowledge.active_config_snapshot(), environ=os.environ
    )
    return j(
        handler,
        {
            "memory": redact_text(read(memory_file)),
            "user": redact_text(read(user_file)),
            "soul": redact_text(read(soul_file)),
            "project_context": redact_text(project_context["content"]),
            "memory_path": str(memory_file),
            "user_path": str(user_file),
            "soul_path": str(soul_file),
            "project_context_path": project_context["path"],
            "project_context_name": project_context.get("name", ""),
            "project_context_workspace": project_context["workspace"],
            "memory_mtime": (
                memory_file.stat().st_mtime if memory_file.exists() else None
            ),
            "user_mtime": user_file.stat().st_mtime if user_file.exists() else None,
            "soul_mtime": soul_file.stat().st_mtime if soul_file.exists() else None,
            "project_context_mtime": project_context["mtime"],
            "project_context_shadowed": project_context["shadowed"],
            "external_notes_enabled": external_notes_enabled,
        },
    )


__all__ = (
    "candidates",
    "git_root",
    "handle_memory_read",
    "read_active",
    "strip_frontmatter",
    "workspace_for_request",
)
