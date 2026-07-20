"""Profile-scoped config, environment, and credential persistence."""

from __future__ import annotations

import json
from pathlib import Path

from api.providers import _write_env_file as write_env_values

from .catalog import SUPPORTED_PROVIDER_SETUPS

__all__ = (
    "get_active_hermes_home",
    "load_env_file",
    "load_yaml_config",
    "oauth_payload_has_token",
    "provider_api_key_present",
    "provider_oauth_authenticated",
    "save_yaml_config",
    "write_env_values",
)


KNOWN_OAUTH_PROVIDERS = frozenset(
    {"openai-codex", "copilot", "copilot-acp", "qwen-oauth", "nous", "anthropic"}
)


def get_active_hermes_home() -> Path:
    try:
        from api.profiles import get_active_hermes_home as resolve_home

        return resolve_home()
    except ImportError:
        return Path.home() / ".hermes"


def load_env_file(env_path: Path) -> dict[str, str]:
    """Read non-comment dotenv values without mutating process state."""
    if not env_path.exists():
        return {}
    values: dict[str, str] = {}
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    except Exception:
        return {}
    return values


def load_yaml_config(config_path: Path) -> dict:
    try:
        import yaml
    except ImportError:
        return {}
    if not config_path.exists():
        return {}
    try:
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_yaml_config(config_path: Path, config: dict) -> None:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to write Hermes config.yaml") from exc
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def oauth_payload_has_token(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    candidates = (
        payload,
        payload.get("tokens") if isinstance(payload.get("tokens"), dict) else {},
    )
    return any(
        str(candidate.get(key) or "").strip()
        for candidate in candidates
        if isinstance(candidate, dict)
        for key in ("access_token", "refresh_token", "api_key")
    )


def provider_oauth_authenticated(provider: str, hermes_home: Path) -> bool:
    """Inspect profile-scoped auth state without returning token material."""
    normalized = (provider or "").strip().lower()
    normalized = {"claude": "anthropic", "claude-code": "anthropic"}.get(
        normalized, normalized
    )
    if normalized not in KNOWN_OAUTH_PROVIDERS:
        return False
    try:
        auth_path = hermes_home / "auth.json"
        if not auth_path.exists():
            return False
        store = json.loads(auth_path.read_text(encoding="utf-8"))
        providers = store.get("providers")
        if isinstance(providers, dict) and oauth_payload_has_token(
            providers.get(normalized)
        ):
            return True
        pools = store.get("credential_pool")
        entries = pools.get(normalized) if isinstance(pools, dict) else None
        if not isinstance(entries, list):
            return False
        for entry in entries:
            if oauth_payload_has_token(entry):
                return True
            if (
                normalized == "anthropic"
                and isinstance(entry, dict)
                and entry.get("auth_type") == "oauth"
                and entry.get("source") == "claude_code_linked"
            ):
                return True
    except Exception:
        return False
    return False


def provider_api_key_present(
    provider: str, config: dict, env_values: dict[str, str]
) -> bool:
    """Return only presence, never credential contents."""
    normalized = (provider or "").strip().lower()
    if not normalized:
        return False
    metadata = SUPPORTED_PROVIDER_SETUPS.get(normalized, {})
    env_var = metadata.get("env_var")
    if env_var and env_values.get(env_var):
        return True
    if any(env_values.get(alias) for alias in metadata.get("env_var_aliases", ())):
        return True

    model = config.get("model", {})
    if isinstance(model, dict) and str(model.get("api_key") or "").strip():
        return True
    providers = config.get("providers") or {}
    if isinstance(providers, dict):
        provider_config = providers.get(normalized, {})
        if isinstance(provider_config, dict) and str(
            provider_config.get("api_key") or ""
        ).strip():
            return True
        custom = providers.get("custom", {})
        if normalized == "custom" and isinstance(custom, dict) and str(
            custom.get("api_key") or ""
        ).strip():
            return True

    if normalized not in SUPPORTED_PROVIDER_SETUPS and normalized not in KNOWN_OAUTH_PROVIDERS:
        try:
            from hermes_cli.auth import get_auth_status

            status = get_auth_status(normalized)
            return bool(isinstance(status, dict) and status.get("logged_in"))
        except Exception:
            return False
    return False
