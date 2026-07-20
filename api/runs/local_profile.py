"""Profile identity and environment preparation for detached local runs."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class LocalProfileContext:
    """Resolved profile state that belongs to the owning session."""

    home: str
    runtime_env: dict
    safe_runtime_env: dict
    resolved_name: str | None
    patch_skill_home_modules: object | None

    @classmethod
    def resolve(cls, session, *, logger: logging.Logger) -> "LocalProfileContext":
        try:
            from api.profiles import (
                filter_runtime_env_for_gateway_parity,
                get_hermes_home_for_profile,
                get_profile_runtime_env,
                patch_skill_home_modules,
            )

            profile_home_path = get_hermes_home_for_profile(
                getattr(session, "profile", None)
            )
            home = str(profile_home_path)
            runtime_env = get_profile_runtime_env(profile_home_path)
            safe_runtime_env = filter_runtime_env_for_gateway_parity(runtime_env)
        except ImportError:
            home = os.environ.get("HERMES_HOME", "")
            runtime_env = {}
            safe_runtime_env = {}
            patch_skill_home_modules = None

        resolved_name = getattr(session, "profile", None)
        if not resolved_name:
            try:
                from api.profiles import get_active_profile_name

                resolved_name = get_active_profile_name()
            except Exception:
                logger.debug("Could not resolve active profile for detached run")
                resolved_name = None

        return cls(
            home=home,
            runtime_env=runtime_env,
            safe_runtime_env=safe_runtime_env,
            resolved_name=resolved_name,
            patch_skill_home_modules=patch_skill_home_modules,
        )

    def enter_environment(self, environment, *, session_id: str, workspace: str) -> None:
        environment.enter(
            session_id=session_id,
            workspace=workspace,
            profile_home=self.home,
            profile_runtime_env=self.runtime_env,
            safe_profile_runtime_env=self.safe_runtime_env,
            patch_skill_home_modules=self.patch_skill_home_modules,
        )
