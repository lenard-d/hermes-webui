"""Profile runtime environment and secret-scope ownership.

This module owns the complete setup/cleanup lifecycle for profile-scoped agent
execution: config and dotenv projection, credential scrubbing, thread-local and
agent secret scopes, optional Hermes-home overrides, legacy process-env mirrors,
and cached skill/cron module paths.  Mutable compatibility state remains on
``api.profiles`` and is resolved through its call-bound facade.
"""

import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import yaml

from api.profiles_parts.facade import profiles_api


def snapshot_skill_home_modules() -> dict[str, dict[str, object]]:
    """Snapshot imported skill-module path globals before a temporary patch."""
    api = profiles_api()
    snapshot: dict[str, dict[str, object]] = {}
    for module_name in api._SKILL_HOME_MODULES:
        module = api.sys.modules.get(module_name)
        if module is None:
            snapshot[module_name] = {"module_present": False}
            continue
        snapshot[module_name] = {
            "module_present": True,
            "has_HERMES_HOME": hasattr(module, "HERMES_HOME"),
            "HERMES_HOME": getattr(module, "HERMES_HOME", None),
            "has_SKILLS_DIR": hasattr(module, "SKILLS_DIR"),
            "SKILLS_DIR": getattr(module, "SKILLS_DIR", None),
        }
    return snapshot


def restore_skill_home_modules(snapshot: dict[str, dict[str, object]]) -> None:
    """Restore skill-module globals captured by snapshot_skill_home_modules()."""
    api = profiles_api()
    for module_name, values in snapshot.items():
        module = api.sys.modules.get(module_name)
        if not values.get("module_present"):
            if module is not None:
                api.sys.modules.pop(module_name, None)
                parent_name, _, child_name = module_name.rpartition(".")
                parent = api.sys.modules.get(parent_name)
                if parent is not None:
                    try:
                        delattr(parent, child_name)
                    except AttributeError:
                        pass
            continue
        if module is None:
            continue
        for attr in ("HERMES_HOME", "SKILLS_DIR"):
            has_attr = bool(values.get(f"has_{attr}"))
            try:
                if has_attr:
                    setattr(module, attr, values.get(attr))
                else:
                    try:
                        delattr(module, attr)
                    except AttributeError:
                        pass
            except AttributeError:
                api.logger.debug("Failed to restore %s.%s", module_name, attr)


def _stringify_env_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return str(value)


def get_profile_runtime_env(home: Path) -> dict[str, str]:
    """Return terminal settings and dotenv values for one profile home."""
    api = profiles_api()
    home = Path(home).expanduser()
    env: dict[str, str] = {}
    try:
        cfg_path = home / "config.yaml"
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
        if not isinstance(cfg, dict):
            cfg = {}
    except Exception:
        cfg = {}

    terminal_cfg = cfg.get("terminal", {}) if isinstance(cfg, dict) else {}
    if isinstance(terminal_cfg, dict):
        for key, env_key in api._TERMINAL_ENV_MAPPINGS.items():
            if key in terminal_cfg and terminal_cfg[key] is not None:
                env[env_key] = api._stringify_env_value(terminal_cfg[key])

    env_path = home / ".env"
    if env_path.exists():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and value and key not in api._PROTECTED_ENV_KEYS:
                        env[key] = value
        except Exception:
            api.logger.debug("Failed to read runtime env from %s", env_path)
    return env


def filter_runtime_env_for_gateway_parity(env: dict[str, str]) -> dict[str, str]:
    """Filter profile runtime env to match Hermes gateway semantics."""
    api = profiles_api()
    filtered: dict[str, str] = {}
    for key, value in (env or {}).items():
        normalized = str(key).strip()
        if not normalized:
            continue
        if normalized in api._BLOCKED_RUNTIME_ENV_KEYS or normalized.startswith("XDG_"):
            continue
        filtered[normalized] = value
    return filtered


def _agent_registry_credential_env_names() -> set[str]:
    """Return all credential env names consumed by the agent runtime."""
    api = profiles_api()
    names: set[str] = set(api._NON_REGISTRY_AGENT_CREDENTIAL_ENV_NAMES)
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY

        items = (
            PROVIDER_REGISTRY.items()
            if hasattr(PROVIDER_REGISTRY, "items")
            else enumerate(PROVIDER_REGISTRY)
        )
        for _key, entry in items:
            for env_var in getattr(entry, "api_key_env_vars", None) or ():
                if env_var:
                    names.add(str(env_var))
    except Exception:
        api.logger.debug(
            "Failed to load agent registry credential env names for profile scope",
            exc_info=True,
        )
    return names


def _profile_secret_env_names(profile_home_path: Path) -> set[str]:
    """Return credential names to scrub before applying one profile's env."""
    api = profiles_api()
    names: set[str] = set()
    try:
        from api.providers import _provider_credential_env_vars

        names.update(_provider_credential_env_vars())
    except Exception:
        api.logger.debug(
            "Failed to load provider credential env names for profile scope",
            exc_info=True,
        )
    names.update(api._agent_registry_credential_env_names())

    config_path = Path(profile_home_path) / "config.yaml"
    if not config_path.exists():
        return names
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        api.logger.debug(
            "Failed to inspect custom-provider credential env names from %s",
            config_path,
            exc_info=True,
        )
        return names

    custom_providers = payload.get("custom_providers") if isinstance(payload, dict) else None
    if not isinstance(custom_providers, list):
        return names
    for custom_provider in custom_providers:
        if not isinstance(custom_provider, dict):
            continue
        key_env = str(custom_provider.get("key_env") or "").strip()
        if key_env:
            names.add(key_env)
        api_key = str(custom_provider.get("api_key") or "").strip()
        match = re.fullmatch(r"\$\{([^}]+)\}", api_key)
        if match:
            env_name = str(match.group(1) or "").strip()
            if env_name:
                names.add(env_name)
    return names


def _apply_profile_env_to_process(
    process_env,
    safe_runtime_env: dict[str, str],
    *,
    secret_env_names: set[str],
) -> dict[str, Optional[str]]:
    """Scrub absent secrets and snapshot every process-env key being scoped."""
    scoped_keys = set(safe_runtime_env) | set(secret_env_names)
    previous_env = {key: process_env.get(key) for key in scoped_keys}
    for key in secret_env_names:
        if key not in safe_runtime_env:
            process_env.pop(key, None)
    return previous_env


def _resolve_secret_scope_module():
    api = profiles_api()
    module = api.sys.modules.get("agent.secret_scope")
    if module is not None:
        return module
    if api._secret_scope_available is False:
        return None
    if api._secret_scope_available is None:
        try:
            import importlib.util

            api._secret_scope_available = importlib.util.find_spec("agent") is not None
        except Exception:
            api._secret_scope_available = False
    if api._secret_scope_available:
        try:
            from agent.secret_scope import reset_secret_scope, set_secret_scope  # noqa: F401

            return api.sys.modules.get("agent.secret_scope")
        except ImportError:
            api._secret_scope_available = False
    return None


def _resolve_hermes_home_override():
    """Return optional context-local Hermes-home override support."""
    api = profiles_api()
    if api._hermes_home_override_available is False:
        return None
    module = api.sys.modules.get("hermes_constants")
    if module is None and api._hermes_home_override_available is None:
        try:
            import hermes_constants as module
        except Exception:
            api._hermes_home_override_available = False
            return None
    if module is not None and hasattr(module, "set_hermes_home_override") and hasattr(
        module, "reset_hermes_home_override"
    ):
        api._hermes_home_override_available = True
        return module
    api._hermes_home_override_available = False
    return None


def profile_env_for_background_worker(
    session,
    purpose: str = "background worker",
    logger_override=None,
):
    """Return a context manager routing detached worker reads through a profile."""
    api = profiles_api()

    @contextmanager
    def scope():
        log = logger_override or api.logger
        raw_profile = session if isinstance(session, str) else getattr(session, "profile", "")
        profile = str(raw_profile or "").strip()
        if not profile or profile == "default":
            yield
            return

        try:
            from api.config import _clear_thread_env, _set_thread_env, _thread_ctx
            from api.streaming import _ENV_LOCK

            profile_home_path = Path(api.get_hermes_home_for_profile(profile))
            runtime_env = api.get_profile_runtime_env(profile_home_path)
            safe_runtime_env = api.filter_runtime_env_for_gateway_parity(runtime_env)
            secret_env_names = api._profile_secret_env_names(profile_home_path)
        except Exception:
            log.debug(
                "Failed to resolve profile env for %s profile %s; falling back to current env",
                purpose,
                profile,
                exc_info=True,
            )
            yield
            return

        thread_env = dict(safe_runtime_env)
        thread_env["HERMES_HOME"] = str(profile_home_path)
        skill_home_snapshot = None
        old_runtime_env: dict[str, Optional[str]] = {}
        old_hermes_home = None
        had_hermes_home = False
        previous_thread_env = getattr(_thread_ctx, "env", {}).copy()
        previous_block_process_env = bool(
            getattr(_thread_ctx, "block_process_env_fallback", False)
        )
        secret_scope_mod = None
        scope_token = None
        has_scope = False
        home_override_mod = None
        home_override_token = None
        try:
            _set_thread_env(**thread_env)
            _thread_ctx.block_process_env_fallback = True
            secret_scope_mod = api._resolve_secret_scope_module()
            if secret_scope_mod is not None:
                try:
                    scope_token = secret_scope_mod.set_secret_scope(thread_env)
                    has_scope = True
                except Exception:
                    pass
            home_override_mod = api._resolve_hermes_home_override()
            if home_override_mod is not None:
                try:
                    home_override_token = home_override_mod.set_hermes_home_override(
                        str(profile_home_path)
                    )
                except Exception:
                    home_override_token = None
            with _ENV_LOCK:
                old_runtime_env = api._apply_profile_env_to_process(
                    os.environ,
                    safe_runtime_env,
                    secret_env_names=secret_env_names,
                )
                had_hermes_home = "HERMES_HOME" in os.environ
                old_hermes_home = os.environ.get("HERMES_HOME")
                skill_home_snapshot = api.snapshot_skill_home_modules()
                os.environ.update(safe_runtime_env)
                os.environ["HERMES_HOME"] = str(profile_home_path)
                try:
                    api.patch_skill_home_modules(profile_home_path)
                except Exception:
                    log.debug(
                        "Failed to patch skill modules for %s profile %s",
                        purpose,
                        profile,
                        exc_info=True,
                    )
            yield
        finally:
            if home_override_mod is not None and home_override_token is not None:
                try:
                    home_override_mod.reset_hermes_home_override(home_override_token)
                except Exception:
                    pass
            if has_scope and secret_scope_mod is not None:
                try:
                    secret_scope_mod.reset_secret_scope(scope_token)
                except Exception:
                    pass
            _thread_ctx.block_process_env_fallback = previous_block_process_env
            if previous_thread_env:
                _set_thread_env(**previous_thread_env)
            else:
                _clear_thread_env()
            with _ENV_LOCK:
                for key, old_value in old_runtime_env.items():
                    if old_value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = old_value
                if had_hermes_home:
                    os.environ["HERMES_HOME"] = old_hermes_home or ""
                else:
                    os.environ.pop("HERMES_HOME", None)
                if skill_home_snapshot is not None:
                    api.restore_skill_home_modules(skill_home_snapshot)

    return scope()


def profile_env_for_active_request_readonly(
    purpose: str = "provider/model read",
    logger_override=None,
):
    """Return a thread-local-only profile scope for read-only request paths."""
    api = profiles_api()

    @contextmanager
    def scope():
        profile = (api.get_active_profile_name() or "").strip()
        if not profile or api._is_root_profile(profile):
            yield
            return
        try:
            from api.config import _clear_thread_env, _set_thread_env, _thread_ctx

            profile_home_path = Path(api.get_hermes_home_for_profile(profile))
            runtime_env = api.get_profile_runtime_env(profile_home_path)
            safe_runtime_env = api.filter_runtime_env_for_gateway_parity(runtime_env)
        except Exception:
            log = logger_override or api.logger
            log.debug(
                "Failed to resolve profile env for active request profile %s in %s; "
                "falling back to current env",
                profile,
                purpose,
                exc_info=True,
            )
            yield
            return
        try:
            from hermes_constants import (
                reset_hermes_home_override,
                set_hermes_home_override,
            )
        except Exception:
            reset_hermes_home_override = None
            set_hermes_home_override = None

        thread_env = dict(safe_runtime_env)
        thread_env["HERMES_HOME"] = str(profile_home_path)
        previous_thread_env = getattr(_thread_ctx, "env", {}).copy()
        previous_block_process_env = bool(
            getattr(_thread_ctx, "block_process_env_fallback", False)
        )
        home_override_token = None
        secret_scope_mod = None
        scope_token = None
        has_scope = False
        try:
            _set_thread_env(**thread_env)
            _thread_ctx.block_process_env_fallback = True
            secret_scope_mod = api._resolve_secret_scope_module()
            if secret_scope_mod is not None:
                try:
                    scope_token = secret_scope_mod.set_secret_scope(thread_env)
                    has_scope = True
                except Exception:
                    pass
            if set_hermes_home_override is not None:
                home_override_token = set_hermes_home_override(profile_home_path)
            yield
        finally:
            if has_scope and secret_scope_mod is not None:
                try:
                    secret_scope_mod.reset_secret_scope(scope_token)
                except Exception:
                    pass
            if home_override_token is not None and reset_hermes_home_override is not None:
                try:
                    reset_hermes_home_override(home_override_token)
                except Exception:
                    (logger_override or api.logger).debug(
                        "Failed to reset Hermes-home override for active request "
                        "profile %s in %s",
                        profile,
                        purpose,
                        exc_info=True,
                    )
            _thread_ctx.block_process_env_fallback = previous_block_process_env
            if previous_thread_env:
                _set_thread_env(**previous_thread_env)
            else:
                _clear_thread_env()

    return scope()


def profile_env_for_active_request(
    purpose: str = "active request",
    logger_override=None,
):
    """Return the legacy process-mirrored active-request scope."""
    api = profiles_api()

    @contextmanager
    def scope():
        profile = (api.get_active_profile_name() or "").strip()
        if not profile or api._is_root_profile(profile):
            yield
            return
        with api.profile_env_for_background_worker(
            profile, purpose, logger_override=logger_override
        ):
            yield

    return scope()


def profile_scope_for_detached_worker(
    profile_name,
    purpose: str = "detached worker",
    logger_override=None,
):
    """Return a scope binding both request TLS and runtime env on a new thread."""
    api = profiles_api()

    @contextmanager
    def scope():
        name = (profile_name or "").strip()
        if not name or api._is_root_profile(name):
            yield
            return
        api.set_request_profile(name)
        try:
            with api.profile_env_for_background_worker(
                name, purpose, logger_override=logger_override
            ):
                yield
        finally:
            api.clear_request_profile()

    return scope()


def _set_hermes_home(home: Path):
    """Set process HERMES_HOME and patch agent modules caching its paths."""
    api = profiles_api()
    os.environ["HERMES_HOME"] = str(home)
    api.patch_skill_home_modules(home)
    try:
        import cron.jobs as cron_jobs

        cron_jobs.HERMES_DIR = home
        cron_jobs.CRON_DIR = home / "cron"
        cron_jobs.JOBS_FILE = cron_jobs.CRON_DIR / "jobs.json"
        cron_jobs.OUTPUT_DIR = cron_jobs.CRON_DIR / "output"
    except (ImportError, AttributeError):
        api.logger.debug("Failed to patch cron.jobs module")
    try:
        import cron.scheduler as cron_scheduler

        cron_scheduler._hermes_home = home
        cron_scheduler._LOCK_DIR = home / "cron"
        cron_scheduler._LOCK_FILE = cron_scheduler._LOCK_DIR / ".tick.lock"
    except (ImportError, AttributeError):
        api.logger.debug("Failed to patch cron.scheduler module")


def _reload_dotenv(home: Path):
    """Replace prior profile dotenv keys with the selected profile's values."""
    api = profiles_api()
    for key in list(api._loaded_profile_env_keys):
        os.environ.pop(key, None)
    api._loaded_profile_env_keys = set()

    env_path = home / ".env"
    if not env_path.exists():
        return
    try:
        loaded_keys: set[str] = set()
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and value:
                    if key in api._PROTECTED_ENV_KEYS:
                        api.logger.warning(
                            "Ignoring protected key %s in profile .env %s; "
                            "operator/deployment env takes precedence",
                            key,
                            env_path,
                        )
                        continue
                    os.environ[key] = value
                    loaded_keys.add(key)
        api._loaded_profile_env_keys = loaded_keys
    except Exception:
        api._loaded_profile_env_keys = set()
        api.logger.debug("Failed to reload dotenv from %s", env_path)
