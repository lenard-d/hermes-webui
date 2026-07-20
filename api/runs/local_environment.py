"""Per-turn process and thread environment lifecycle for local execution."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from api.config import environment_mutation_lock as _ENV_LOCK, set_thread_env
from .agent_runtime import ensure_agent_runtime_current
from .diagnostics import _install_streaming_cronjob_profile_wrapper
from .agent_loader import _prewarm_skill_tool_modules
from .turn_identity import _build_agent_thread_env


logger = logging.getLogger(__name__)


class LocalRunEnvironment:
    """Install one run's profile/workspace context and restore it exactly once."""

    _PROCESS_KEYS = (
        "TERMINAL_CWD",
        "HERMES_EXEC_ASK",
        "HERMES_SESSION_KEY",
        "HERMES_SESSION_ID",
        "HERMES_SESSION_PLATFORM",
        "HERMES_SESSION_CHAT_ID",
        "HERMES_HOME",
    )

    def __init__(self) -> None:
        self._previous: dict[str, str | None] = {}
        self._entered = False

    def enter(
        self,
        *,
        session_id: str,
        workspace: str,
        profile_home: str,
        profile_runtime_env: dict,
        safe_profile_runtime_env: dict,
        patch_skill_home_modules,
    ) -> None:
        thread_env = _build_agent_thread_env(
            profile_runtime_env,
            workspace,
            session_id,
            profile_home,
        )
        set_thread_env(thread_env)
        try:
            from api.background_process import register_process_session

            register_process_session(session_id, session_id)
        except Exception:
            logger.debug("register_process_session failed", exc_info=True)

        # Potentially slow imports happen before the process-global env lock.
        ensure_agent_runtime_current()
        _prewarm_skill_tool_modules()
        _install_streaming_cronjob_profile_wrapper()
        with _ENV_LOCK:
            keys = {*self._PROCESS_KEYS, *safe_profile_runtime_env}
            self._previous = {key: os.environ.get(key) for key in keys}
            os.environ.update(safe_profile_runtime_env)
            os.environ.update(
                {
                    "TERMINAL_CWD": workspace,
                    "HERMES_EXEC_ASK": "1",
                    "HERMES_SESSION_KEY": session_id,
                    "HERMES_SESSION_ID": session_id,
                    "HERMES_SESSION_PLATFORM": "webui",
                    "HERMES_SESSION_CHAT_ID": session_id,
                }
            )
            if profile_home:
                os.environ["HERMES_HOME"] = profile_home
                if patch_skill_home_modules is not None:
                    patch_skill_home_modules(Path(profile_home))
            self._entered = True

        # Discovery intentionally happens after HERMES_HOME is installed.
        try:
            from tools.mcp_tool import discover_mcp_tools

            discover_mcp_tools()
        except Exception:
            pass

    def close(self) -> None:
        if not self._entered:
            return
        with _ENV_LOCK:
            for key, value in self._previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self._entered = False
