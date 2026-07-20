"""Bounded in-memory session-cache policy."""

from api import config as _config_module
from api.config.paths import _env_int


DEFAULT_SESSIONS_CACHE_MAX = 300
SESSIONS_MAX = _env_int("HERMES_WEBUI_SESSIONS_MAX", DEFAULT_SESSIONS_CACHE_MAX)


def get_sessions_cache_max(config_data: dict | None = None) -> int:
    """Return the effective cap for compact sessions retained in memory."""
    active_cfg = (
        config_data if isinstance(config_data, dict) else _config_module.get_config()
    )
    webui_cfg = active_cfg.get("webui", {}) if isinstance(active_cfg, dict) else {}
    if isinstance(webui_cfg, dict):
        raw = webui_cfg.get("sessions_cache_max")
        if raw is not None:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                value = None
            if value is not None and value >= 1:
                return value

    # Read the facade values at call time so established monkeypatch and
    # operator override behavior remains authoritative during the migration.
    legacy_cap = _config_module.SESSIONS_MAX
    if isinstance(legacy_cap, int) and legacy_cap >= 1:
        return legacy_cap
    return _config_module.DEFAULT_SESSIONS_CACHE_MAX


__all__ = [
    "DEFAULT_SESSIONS_CACHE_MAX",
    "SESSIONS_MAX",
    "get_sessions_cache_max",
]
