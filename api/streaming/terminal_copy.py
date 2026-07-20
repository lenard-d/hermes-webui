"""User-facing copy for terminal and provider outcomes."""

from __future__ import annotations

import logging

from api.config import load_settings


logger = logging.getLogger(__name__)


def _preferred_agent_display_name() -> str:
    try:
        name = str((load_settings() or {}).get("bot_name") or "").strip()
    except Exception:
        logger.debug("Failed to load bot_name for cancellation copy", exc_info=True)
        name = ""
    return name or "Hermes"


def _preferred_agent_display_name_for_session(session) -> str:
    profile = str(getattr(session, "profile", "") or "").strip()
    if profile and profile != "default":
        return profile[:1].upper() + profile[1:]
    return _preferred_agent_display_name()


def _cancelled_turn_hint(agent_name: str | None = None) -> str:
    name = str(agent_name or _preferred_agent_display_name()).strip() or "Hermes"
    return f"The run was cancelled by the user before {name} finished. No provider failure occurred."


__all__ = [
    "_cancelled_turn_hint",
    "_preferred_agent_display_name",
    "_preferred_agent_display_name_for_session",
]
