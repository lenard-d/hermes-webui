"""Validated profile creation, cloning, configuration, and deletion.

This module keeps the whole mutation transaction local: logical-name and path
validation, upstream/fallback persistence, model-catalog validation, secret
placement in ``.env``, config writes, and cache invalidation.  Public callers
continue to use :mod:`api.profiles`; the facade is resolved late so its existing
test and integration patches remain authoritative.
"""

from pathlib import Path
from typing import Optional

import yaml

from api import profiles as _profiles_module


def _validate_profile_name(name: str):
    """Validate the logical profile identifier."""
    api = _profiles_module
    if name == "default":
        raise ValueError(
            "Cannot create a profile named 'default' -- it is the built-in profile."
        )
    if not api._PROFILE_ID_RE.fullmatch(name):
        raise ValueError(
            f"Invalid profile name {name!r}. "
            "Must match [a-z0-9][a-z0-9_-]{0,63}"
        )


def _profiles_root() -> Path:
    """Return the canonical root containing named profiles."""
    api = _profiles_module
    return (api._DEFAULT_HERMES_HOME / "profiles").resolve()


def _resolve_named_profile_home(name: str) -> Path:
    """Resolve a validated name beneath the canonical profiles root."""
    api = _profiles_module
    api._validate_profile_name(name)
    profiles_root = api._profiles_root()
    candidate = (profiles_root / name).resolve()
    candidate.relative_to(profiles_root)
    return candidate


def _create_profile_fallback(
    name: str,
    clone_from: str = None,
    clone_config: bool = False,
) -> Path:
    """Create a profile directory when hermes_cli is unavailable."""
    api = _profiles_module
    profile_dir = api._DEFAULT_HERMES_HOME / "profiles" / name
    if profile_dir.exists():
        raise FileExistsError(f"Profile '{name}' already exists.")

    profile_dir.mkdir(parents=True, exist_ok=False)
    for subdir in api._PROFILE_DIRS:
        (profile_dir / subdir).mkdir(parents=True, exist_ok=True)

    if clone_config and clone_from:
        source_dir = (
            api._DEFAULT_HERMES_HOME
            if api._is_root_profile(clone_from)
            else api._DEFAULT_HERMES_HOME / "profiles" / clone_from
        )
        if source_dir.is_dir():
            for filename in api._CLONE_CONFIG_FILES:
                source = source_dir / filename
                if source.exists():
                    api.shutil.copy2(source, profile_dir / filename)
    return profile_dir


def _resolve_env_var_for_provider(provider: Optional[str]) -> Optional[str]:
    """Return the provider-specific secret environment variable name."""
    if not provider:
        return None
    return _profiles_module._PROVIDER_ENV_MAP.get(str(provider).strip().lower())


def _upsert_dotenv_line(env_path: Path, key: str, value: str) -> None:
    """Replace or append one key in a dotenv file."""
    api = _profiles_module
    env_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lines = (
            env_path.read_text(encoding="utf-8").splitlines()
            if env_path.exists()
            else []
        )
    except Exception:
        lines = []

    new_line = f"{key}={value}"
    found = False
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            existing_key, _ = stripped.split("=", 1)
            if existing_key.strip() == key:
                new_lines.append(new_line)
                found = True
                continue
        new_lines.append(line)
    if not found:
        new_lines.append(new_line)

    try:
        env_path.write_text(
            "\n".join(new_lines).rstrip("\n") + "\n", encoding="utf-8"
        )
    except Exception as exc:
        api.logger.error("Failed to write %s to %s: %s", key, env_path, exc)
        raise


def _write_api_key_to_dotenv(
    profile_dir: Path,
    api_key: str,
    model_provider: Optional[str] = None,
) -> None:
    """Persist a provider credential only in the profile's protected .env."""
    api = _profiles_module
    env_var = api._resolve_env_var_for_provider(model_provider)
    if not env_var:
        env_var = "HERMES_API_KEY"
        api.logger.info(
            "No provider→env mapping for %r; writing API key as %s",
            model_provider,
            env_var,
        )
    env_path = profile_dir / ".env"
    api._upsert_dotenv_line(env_path, env_var, api_key)
    try:
        env_path.chmod(0o600)
    except Exception:
        api.logger.debug("Failed to chmod 0o600 on %s", env_path)


def _write_endpoint_to_config(
    profile_dir: Path,
    base_url: str = None,
    api_key: str = None,
) -> None:
    """Persist a base URL while deliberately excluding API keys."""
    if not base_url:
        return
    api = _profiles_module
    config_path = profile_dir / "config.yaml"
    cfg = {}
    if config_path.exists():
        try:
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg = loaded
        except Exception:
            api.logger.debug("Failed to load config from %s", config_path)
    model_section = cfg.get("model", {})
    if not isinstance(model_section, dict):
        model_section = {}
    model_section["base_url"] = base_url
    cfg["model"] = model_section
    config_path.write_text(
        yaml.dump(cfg, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )


def _clean_profile_config_value(
    value: Optional[str], field: str
) -> Optional[str]:
    """Normalize a bounded single-line profile config value."""
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    if any(char in cleaned for char in ("\x00", "\r", "\n")):
        raise ValueError(f"{field} must be a single-line value")
    if len(cleaned) > 512:
        raise ValueError(f"{field} is too long")
    return cleaned


def _split_webui_provider_model_value(
    default_model: Optional[str],
    model_provider: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Normalize an internal ``@provider:model`` picker value."""
    api = _profiles_module
    model = api._clean_profile_config_value(default_model, "default_model")
    provider = api._clean_profile_config_value(model_provider, "model_provider")
    if model and model.startswith("@") and ":" in model:
        provider_part, model_part = model[1:].rsplit(":", 1)
        provider = provider or api._clean_profile_config_value(
            provider_part, "model_provider"
        )
        model = api._clean_profile_config_value(model_part, "default_model")
    return model, provider


def _strip_webui_provider_prefix(model_id: object) -> str:
    value = str(model_id or "").strip()
    if value.startswith("@") and ":" in value:
        return value.rsplit(":", 1)[1]
    return value


def _profile_model_selection_exists(
    available_models: object,
    default_model: Optional[str],
    model_provider: Optional[str],
) -> bool:
    """Return whether a default model/provider exists in the catalog."""
    api = _profiles_module
    if not default_model and not model_provider:
        return True
    if not isinstance(available_models, dict):
        return False

    provider_seen = False
    model_seen = False
    for group in available_models.get("groups", []) or []:
        if not isinstance(group, dict):
            continue
        provider_id = str(group.get("provider_id") or "").strip()
        if model_provider and provider_id != model_provider:
            continue
        if model_provider and provider_id == model_provider:
            provider_seen = True
        group_models = (group.get("models") or []) + (
            group.get("extra_models") or []
        )
        for model in group_models:
            if not isinstance(model, dict):
                continue
            model_id = str(model.get("id") or "").strip()
            if not model_id:
                continue
            if default_model and (
                model_id == default_model
                or api._strip_webui_provider_prefix(model_id) == default_model
            ):
                model_seen = True
                if model_provider:
                    return True
        if not default_model and provider_seen:
            return True
    if model_provider and not provider_seen:
        return False
    return bool(model_seen)


def _get_available_models_for_profile_validation() -> dict:
    from api.config import get_available_models

    return get_available_models()


def _validate_profile_model_selection(
    default_model: Optional[str],
    model_provider: Optional[str],
    available_models: Optional[dict] = None,
) -> None:
    """Reject profile defaults absent from the server model catalog."""
    api = _profiles_module
    if not default_model and not model_provider:
        return
    catalog = (
        available_models
        if available_models is not None
        else api._get_available_models_for_profile_validation()
    )
    if api._profile_model_selection_exists(
        catalog, default_model, model_provider
    ):
        return
    if default_model and model_provider:
        raise ValueError(
            f"Selected model '{default_model}' is not available for provider "
            f"'{model_provider}'"
        )
    if default_model:
        raise ValueError(f"Selected model '{default_model}' is not available")
    raise ValueError(
        f"Selected model provider '{model_provider}' is not available"
    )


def _write_model_defaults_to_config(
    profile_dir: Path,
    *,
    default_model: Optional[str] = None,
    model_provider: Optional[str] = None,
) -> None:
    """Persist validated default model/provider fields."""
    api = _profiles_module
    default_model, model_provider = api._split_webui_provider_model_value(
        default_model, model_provider
    )
    if not default_model and not model_provider:
        return
    config_path = profile_dir / "config.yaml"
    cfg = {}
    if config_path.exists():
        try:
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg = loaded
        except Exception:
            api.logger.debug("Failed to load config from %s", config_path)
    model_section = cfg.get("model", {})
    if not isinstance(model_section, dict):
        model_section = {}
    if default_model:
        model_section["default"] = default_model
    if model_provider:
        model_section["provider"] = model_provider
    cfg["model"] = model_section
    config_path.write_text(
        yaml.dump(cfg, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )


def create_profile_api(
    name: str,
    clone_from: str = None,
    clone_config: bool = False,
    base_url: str = None,
    api_key: str = None,
    default_model: str = None,
    model_provider: str = None,
) -> dict:
    """Create, configure, and return one profile through a single transaction."""
    api = _profiles_module
    if api._is_isolated_profile_mode():
        raise PermissionError(
            "Profile creation is not allowed in isolated profile mode."
        )
    api._validate_profile_name(name)
    if clone_from is not None and not api._is_root_profile(clone_from):
        api._validate_profile_name(clone_from)
    default_model, model_provider = api._split_webui_provider_model_value(
        default_model, model_provider
    )
    api._validate_profile_model_selection(default_model, model_provider)

    try:
        from hermes_cli.profiles import create_profile

        create_profile(
            name,
            clone_from=clone_from,
            clone_config=clone_config,
            clone_all=False,
            no_alias=True,
        )
    except ImportError:
        api._create_profile_fallback(name, clone_from, clone_config)

    profile_path = api._DEFAULT_HERMES_HOME / "profiles" / name
    for profile in api.list_profiles_api():
        if profile["name"] == name:
            try:
                profile_path = Path(profile.get("path") or profile_path)
            except Exception:
                api.logger.debug("Failed to parse profile path")
            break
    profile_path.mkdir(parents=True, exist_ok=True)

    if clone_from is None:
        try:
            from hermes_cli.profiles import seed_profile_skills

            seed_profile_skills(profile_path, quiet=True)
        except ImportError:
            api.logger.debug(
                "seed_profile_skills unavailable — bundled skills not seeded "
                "for profile %s (hermes_cli not in path)",
                name,
            )
        except Exception:
            api.logger.warning(
                "Bundled skills could not be seeded for profile %s; "
                "profile created successfully anyway",
                name,
                exc_info=True,
            )

    api._write_endpoint_to_config(profile_path, base_url=base_url)
    if api_key:
        api._write_api_key_to_dotenv(
            profile_path,
            api_key=api_key,
            model_provider=model_provider,
        )
    api._write_model_defaults_to_config(
        profile_path,
        default_model=default_model,
        model_provider=model_provider,
    )

    api._SKILLS_STATS_CACHE.clear()
    api._invalidate_list_profiles_cache()
    api._invalidate_root_profile_cache()

    for profile in api.list_profiles_api():
        if profile["name"] == name:
            return profile
    return {
        "name": name,
        "path": str(profile_path),
        "is_default": False,
        "is_active": api._active_profile == name,
        "gateway_running": False,
        "model": None,
        "provider": None,
        "has_env": (profile_path / ".env").exists(),
        "skill_count": 0,
        "enabled_skills": 0,
        "total_skills": 0,
    }


def delete_profile_api(name: str) -> dict:
    """Delete a validated non-root profile, switching away first if needed."""
    api = _profiles_module
    if api._is_isolated_profile_mode():
        raise PermissionError(
            "Profile deletion is not allowed in isolated profile mode."
        )
    if api._is_root_profile(name):
        raise ValueError("Cannot delete the default profile.")
    api._validate_profile_name(name)

    if api._active_profile == name:
        try:
            api.switch_profile("default")
        except RuntimeError:
            raise RuntimeError(  # noqa: B904 - preserve facade error contract
                f"Cannot delete active profile '{name}' while an agent is running. "
                "Cancel or wait for it to finish."
            )

    try:
        from hermes_cli.profiles import delete_profile

        delete_profile(name, yes=True)
    except ImportError:
        profile_dir = api._resolve_named_profile_home(name)
        if profile_dir.is_dir():
            api.shutil.rmtree(str(profile_dir))
        else:
            raise ValueError(  # noqa: B904 - preserve facade error contract
                f"Profile '{name}' does not exist."
            )

    api._SKILLS_STATS_CACHE.clear()
    api._invalidate_list_profiles_cache()
    api._invalidate_root_profile_cache()
    return {"ok": True, "name": name}
