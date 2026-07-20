"""Lazy Hermes-agent loading and bounded runtime defaults."""

from __future__ import annotations

from api.config import get_config
from .agent_runtime import ensure_agent_runtime_current, get_ai_agent_class


AIAgent = get_ai_agent_class()


def _prewarm_skill_tool_modules():
    """Import tools.skills_tool and tools.skill_manager_tool outside any lock.

    First-time module imports can trigger heavy initialisation (disk I/O,
    transitive imports, plugin discovery).  Performing those imports while
    holding ``_ENV_LOCK`` serialises every concurrent session behind the
    slowest import. Prewarming ensures the modules are already imported
    before the lock is acquired, so the lock body only does lightweight
    attribute patching.

    We cannot place these at module top-level because ``tools.*`` lives
    in the hermes-agent package which may not be on ``sys.path`` at
    import time (Docker volume-mount ordering).  A dedicated helper
    keeps the lazy-import try/except in one place and makes the intent
    explicit.
    """
    for _mod_name in ('tools.skills_tool', 'tools.skill_manager_tool'):
        try:
            __import__(_mod_name)
        except ImportError:
            pass

def _get_ai_agent():
    """Return AIAgent class, retrying the import if the initial attempt failed.

    auto_install_agent_deps() in server.py may install missing packages after
    this module is first imported (common in Docker with a volume-mounted agent).
    Re-attempting the import here picks up the newly installed packages without
    requiring a server restart. The shared runtime guard also refuses to reuse
    cached Agent modules after the source checkout changes.
    """
    global AIAgent
    ensure_agent_runtime_current()
    if AIAgent is None:
        AIAgent = get_ai_agent_class()
    return AIAgent

def _clarify_timeout_seconds(default: int = 120) -> int:
    """Resolve clarify timeout from config, with bounded fallback."""
    try:
        cfg = get_config()
        raw = cfg.get("clarify", {}).get("timeout", default)
        timeout_seconds = int(raw)
        if timeout_seconds <= 0:
            return default
        return timeout_seconds
    except Exception:
        return default
