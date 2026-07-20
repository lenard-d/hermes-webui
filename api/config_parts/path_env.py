"""Environment parsing, installation discovery, and workspace path policy.

``api.config`` remains the compatibility facade for mutable path constants and
test overrides.  Every helper resolves that facade at call time, so established
patches of ``HOME``, ``REPO_ROOT``, ``STATE_DIR``, and helper functions continue
to affect the decision that ultimately uses them.
"""

import os
import re
import shutil
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast

from api.config_parts.facade import config_api


class PathEnvironmentAPI(Protocol):
    HOME: Path
    REPO_ROOT: Path
    STATE_DIR: Path
    SESSION_DIR: Path
    SESSION_INDEX_FILE: Path
    DEFAULT_WORKSPACE: Path
    PYTHON_EXE: str
    HOST: str
    PORT: int
    _DEFAULT_HERMES_HOME: Path
    _AGENT_DIR: Path | None
    _HERMES_FOUND: bool
    logger: object

    def _looks_like_agent_source_root(self, path: Path) -> bool: ...
    def _looks_like_pip_style_agent_source_root(self, path: Path) -> bool: ...
    def _workspace_candidates(self, raw: str | Path | None = None) -> list[Path]: ...
    def _ensure_workspace_dir(self, path: Path) -> bool: ...
    def _get_config_path(self) -> Path: ...
    def _warn_state_dir_divergence(self, warn_prefix: str) -> None: ...


def _config_api() -> PathEnvironmentAPI:
    return cast(PathEnvironmentAPI, cast(ModuleType, config_api()))


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """Read a bounded integer environment setting, falling back on bad input."""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def _env_mb_bytes(name: str, default_mb: int) -> int:
    """Parse an optional megabyte environment variable into bytes."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default_mb * 1024 * 1024
    match = re.match(r"^(\d+)\s*(?:m|mb|mib)?$", raw, re.IGNORECASE)
    if not match:
        _config_api().logger.warning(
            "Invalid %s=%r; expected a positive integer in MB. Falling back to %sMB.",
            name,
            raw,
            default_mb,
        )
        return default_mb * 1024 * 1024
    value_mb = int(match.group(1))
    if value_mb <= 0:
        _config_api().logger.warning(
            "Invalid %s=%r; expected a value greater than zero. Falling back to %sMB.",
            name,
            raw,
            default_mb,
        )
        return default_mb * 1024 * 1024
    return value_mb * 1024 * 1024


def _discover_agent_dir() -> Path | None:
    """Locate the Hermes Agent source root using the documented priority."""
    api = _config_api()
    explicit_override = os.getenv("HERMES_WEBUI_AGENT_DIR")
    if explicit_override:
        explicit_path = Path(explicit_override).expanduser().resolve()
        if explicit_path.exists() and api._looks_like_agent_source_root(explicit_path):
            return explicit_path

    candidates: list[Path] = []
    hermes_home = os.getenv("HERMES_HOME", str(api._DEFAULT_HERMES_HOME))
    candidates.append(Path(hermes_home).expanduser() / "hermes-agent")
    candidates.append(api.REPO_ROOT.parent / "hermes-agent")
    if api._looks_like_agent_source_root(api.REPO_ROOT.parent):
        candidates.append(api.REPO_ROOT.parent)
    candidates.append(api._DEFAULT_HERMES_HOME / "hermes-agent")
    candidates.append(api.HOME / "hermes-agent")
    xdg_data = Path(os.getenv("XDG_DATA_HOME", str(api.HOME / ".local" / "share")))
    candidates.append(xdg_data.expanduser() / "hermes-agent")
    for sys_prefix in ("/opt", "/usr/local", "/usr/local/share"):
        candidates.append(Path(sys_prefix) / "hermes-agent")

    for path in candidates:
        if path.exists() and (path / "run_agent.py").exists():
            return path.resolve()
    for path in candidates:
        if path.exists() and api._looks_like_pip_style_agent_source_root(path):
            return path.resolve()
    return None


def _looks_like_agent_source_root(path: Path) -> bool:
    """Return whether a directory resembles a Hermes Agent source root."""
    if (path / "run_agent.py").exists():
        return True
    return _config_api()._looks_like_pip_style_agent_source_root(path)


def _looks_like_pip_style_agent_source_root(path: Path) -> bool:
    """Return whether a directory has the required pip-style Agent markers."""
    if not (path / "cron" / "jobs.py").exists():
        return False
    if (path / "hermes").exists():
        return True
    hermes_cli_dir = path / "hermes_cli"
    return (
        (hermes_cli_dir / "__init__.py").exists()
        or (hermes_cli_dir / "main.py").exists()
    )


def _discover_python(agent_dir: Path | None) -> str:
    """Locate the interpreter used for Hermes Agent execution."""
    configured = os.getenv("HERMES_WEBUI_PYTHON")
    if configured:
        return configured

    if agent_dir:
        for subdir, binary in (("bin", "python"), ("Scripts", "python.exe")):
            for venv_name in ("venv", ".venv"):
                candidate = agent_dir / venv_name / subdir / binary
                if candidate.exists():
                    return str(candidate)

    for subdir, binary in (("bin", "python"), ("Scripts", "python.exe")):
        local_venv = _config_api().REPO_ROOT / ".venv" / subdir / binary
        if local_venv.exists():
            return str(local_venv)

    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            return found
    return "python3"


def _workspace_candidates(raw: str | Path | None = None) -> list[Path]:
    """Return ordered workspace candidates without duplicates."""
    api = _config_api()
    candidates: list[Path] = []

    def add(candidate: str | Path | None) -> None:
        if candidate in (None, ""):
            return
        try:
            path = Path(candidate).expanduser().resolve()
        except Exception:
            return
        if path not in candidates:
            candidates.append(path)

    add(raw)
    configured = os.getenv("HERMES_WEBUI_DEFAULT_WORKSPACE")
    if configured:
        add(configured)

    home_workspace = api.HOME / "workspace"
    home_work = api.HOME / "work"
    if home_workspace.exists():
        add(home_workspace)
    if home_work.exists():
        add(home_work)
    add(home_workspace)
    add(api.STATE_DIR / "workspace")
    return candidates


def _ensure_workspace_dir(path: Path) -> bool:
    """Best-effort check that a workspace directory exists and is usable."""
    try:
        resolved = path.expanduser().resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved.is_dir() and os.access(
            resolved, os.R_OK | os.W_OK | os.X_OK
        )
    except Exception:
        return False


def resolve_default_workspace(raw: str | Path | None = None) -> Path:
    """Return the first usable workspace path, creating it when possible."""
    api = _config_api()
    for candidate in api._workspace_candidates(raw):
        if api._ensure_workspace_dir(candidate):
            return candidate
    raise RuntimeError(
        "Could not create or access any usable workspace directory. "
        "Set HERMES_WEBUI_DEFAULT_WORKSPACE to a writable path."
    )


def _discover_default_workspace() -> Path:
    """Resolve the default workspace using the shared candidate policy."""
    return resolve_default_workspace()


def _warn_state_dir_divergence(warn_prefix: str) -> None:
    """Warn when another sibling state directory appears to own sessions."""
    api = _config_api()
    try:
        if api.SESSION_DIR.exists():
            session_dir_empty = not any(
                path.name != "_index.json" for path in api.SESSION_DIR.glob("*.json")
            )
        else:
            session_dir_empty = True

        index_file_empty = True
        if api.SESSION_INDEX_FILE.exists():
            try:
                content = api.SESSION_INDEX_FILE.read_text(encoding="utf-8").strip()
                index_file_empty = not content or content in ("{}", "[]", "null")
            except Exception:
                pass

        if not (session_dir_empty and index_file_empty):
            return
        state_parent = api.STATE_DIR.parent
        if not state_parent.exists():
            return
        for sibling in state_parent.iterdir():
            if not sibling.is_dir() or sibling == api.STATE_DIR:
                continue
            sibling_sessions = sibling / "sessions"
            if not sibling_sessions.exists():
                continue
            if not any(
                path.name != "_index.json" for path in sibling_sessions.glob("*.json")
            ):
                continue
            print(
                f"{warn_prefix}  STATE_DIR is empty but a sibling state directory has session data.\n"
                f"        Current : {api.STATE_DIR}\n"
                f"        Sibling : {sibling}\n"
                "        If you switched launch methods (bootstrap.py / ctl.sh / systemd),\n"
                "        the active HERMES_WEBUI_STATE_DIR env var may differ from the\n"
                "        previous run. Set it explicitly to restore access:\n"
                f"          export HERMES_WEBUI_STATE_DIR={sibling}",
                flush=True,
            )
            return
    except Exception:
        pass


def print_startup_config() -> None:
    """Print detected configuration so operators can verify discovery."""
    api = _config_api()
    ok = "\033[32m[ok]\033[0m"
    warn = "\033[33m[!!]\033[0m"
    err = "\033[31m[XX]\033[0m"
    config_path = api._get_config_path()
    lines = [
        "",
        "  Hermes Web UI -- startup config",
        "  --------------------------------",
        f"  repo root   : {api.REPO_ROOT}",
        f"  agent dir   : {api._AGENT_DIR if api._AGENT_DIR else 'NOT FOUND'}  {ok if api._AGENT_DIR else err}",
        f"  python      : {api.PYTHON_EXE}",
        f"  state dir   : {api.STATE_DIR}",
        f"  workspace   : {api.DEFAULT_WORKSPACE}",
        f"  host:port   : {api.HOST}:{api.PORT}",
        f"  config file : {config_path}  {'(found)' if config_path.exists() else '(not found, using defaults)'}",
        "",
    ]
    print("\n".join(lines), flush=True)
    try:
        api._warn_state_dir_divergence(warn)
    except Exception:
        pass
    if not api._HERMES_FOUND:
        print(
            f"{err}  Could not find the Hermes agent directory.\n"
            "      The server will start but agent features will not work.\n\n"
            "      To fix, set one of:\n"
            "        export HERMES_WEBUI_AGENT_DIR=/path/to/hermes-agent\n"
            "        export HERMES_HOME=/path/to/.hermes\n\n"
            "      Or clone hermes-agent as a sibling of this repo:\n"
            "        git clone <hermes-agent-repo> ../hermes-agent\n",
            flush=True,
        )


def verify_hermes_imports() -> tuple[bool, list[str], dict[str, str]]:
    """Return whether required Hermes Agent modules import successfully."""
    missing: list[str] = []
    errors: dict[str, str] = {}
    for module_name in ("run_agent",):
        try:
            __import__(module_name)
        except Exception as exc:
            missing.append(module_name)
            errors[module_name] = f"{type(exc).__name__}: {exc}"
    return not missing, missing, errors
