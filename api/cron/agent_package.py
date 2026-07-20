"""Resolve the Hermes Agent cron package without accepting a shadow package."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path


_IMPORT_PATH_LOCK = threading.Lock()
_IMPORT_PATH_READY: str | None = None


def ensure_agent_cron_import_path() -> None:
    """Prefer the configured agent's cron package over unrelated packages."""
    try:
        from api import config as api_config
    except Exception:
        return

    agent_dir = getattr(api_config, "_AGENT_DIR", None)
    if not agent_dir:
        return
    agent_path = str(Path(agent_dir).expanduser().resolve())
    agent_cron_path = str(Path(agent_path) / "cron")

    global _IMPORT_PATH_READY
    with _IMPORT_PATH_LOCK:
        cron_mod = sys.modules.get("cron")
        cron_file = str(getattr(cron_mod, "__file__", "") or "") if cron_mod else ""
        cron_is_agent = bool(
            cron_mod is not None and cron_file.startswith(agent_cron_path + os.sep)
        )
        if _IMPORT_PATH_READY == agent_path and (cron_mod is None or cron_is_agent):
            return

        while agent_path in sys.path:
            sys.path.remove(agent_path)
        shadow_indexes = [
            idx
            for idx, path_entry in enumerate(sys.path)
            if path_entry
            and Path(path_entry).resolve() != Path(agent_path)
            and (Path(path_entry) / "cron" / "__init__.py").exists()
        ]
        if shadow_indexes:
            sys.path.insert(min(shadow_indexes), agent_path)
        else:
            sys.path.append(agent_path)
        _IMPORT_PATH_READY = agent_path

        # Keep in-memory test doubles or namespace stubs intact; only evict a
        # real on-disk shadow package so the agent's package can import.
        if cron_mod is not None and cron_file and not cron_is_agent:
            for name in list(sys.modules):
                if name == "cron" or name.startswith("cron."):
                    sys.modules.pop(name, None)


def reset_import_path_cache_for_tests() -> None:
    """Reset cached import-path identity for isolated package-resolution tests."""
    global _IMPORT_PATH_READY
    with _IMPORT_PATH_LOCK:
        _IMPORT_PATH_READY = None
