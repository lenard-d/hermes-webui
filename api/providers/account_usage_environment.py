"""Fail-closed environment construction for profile-isolated usage probes."""

from __future__ import annotations

import os
import signal
from pathlib import Path

from api.config import (
    PROVIDER_CREDENTIAL_ENV_VARS as _PROVIDER_CREDENTIAL_ENV_VARS,
    get_agent_source_dir,
    is_process_env_fallback_blocked,
)
from api.providers.credentials import (
    _load_env_file,
    _provider_env_var_for,
)


def _account_usage_preexec_fn() -> None:
    """Ask Linux to terminate a probe when its WebUI parent disappears."""
    try:
        import ctypes

        libc = ctypes.CDLL(None)
        libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG=1, SIGTERM=15
    except Exception:
        pass


def _account_usage_subprocess_env(
    home: Path,
    provider: str,
    api_key: str | None,
) -> dict[str, str]:
    """Build a profile-scoped child env without process-default credentials."""
    env = dict(os.environ)
    if is_process_env_fallback_blocked():
        secret_names = set(_PROVIDER_CREDENTIAL_ENV_VARS)
        try:
            from api.profiles import get_active_hermes_home, profile_secret_env_names

            secret_names.update(profile_secret_env_names(get_active_hermes_home()))
        except Exception:
            # Keep the fallback fail-closed even if the shared registry is
            # temporarily unavailable during import/startup.
            secret_names.update({"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"})
        for env_name in secret_names:
            env.pop(env_name, None)

    home = Path(home)
    env["HERMES_HOME"] = str(home)
    for key, value in _load_env_file(home / ".env").items():
        if value:
            env[key] = value

    env_var = _provider_env_var_for((provider or "").strip().lower())
    if env_var and api_key:
        env[env_var] = api_key

    # The child launches a WebUI-owned module and imports Hermes Agent. Include
    # both source roots explicitly so startup does not depend on the server cwd.
    pythonpath_parts = [str(Path(__file__).resolve().parents[2])]
    agent_dir = get_agent_source_dir()
    if agent_dir:
        pythonpath_parts.append(str(agent_dir))
    existing_pythonpath = env.get("PYTHONPATH", "")
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    return env


__all__ = ("_account_usage_preexec_fn", "_account_usage_subprocess_env")
