"""Cron profile-home resolution and process-global adapter isolation.

``cron.jobs`` and ``cron.scheduler`` cache or read process-global Hermes paths.
This module owns the serialized adapter that temporarily projects one profile
into those globals and restores every value on success or failure.
"""

import os
from pathlib import Path

from api import profiles as _profiles_module


def _cron_profile_context_depth() -> int:
    api = _profiles_module
    return int(getattr(api._tls, "cron_profile_depth", 0) or 0)


def _push_cron_profile_context_depth() -> None:
    api = _profiles_module
    api._tls.cron_profile_depth = api._cron_profile_context_depth() + 1


def _pop_cron_profile_context_depth() -> None:
    api = _profiles_module
    depth = api._cron_profile_context_depth()
    api._tls.cron_profile_depth = max(0, depth - 1)


def _home_for_scheduled_cron_job(job: dict) -> Path:
    """Resolve a scheduler job's profile home, falling back safely."""
    api = _profiles_module
    raw = str((job or {}).get("profile") or "").strip()
    if api._is_isolated_profile_mode():
        active = api._isolated_profile_name()
        if raw and not api._profiles_match(raw, active):
            api.logger.warning(
                "Cron job %s references profile %r outside isolated profile %r; "
                "falling back to isolated home",
                (job or {}).get("id", "?"),
                raw,
                active,
            )
        return api.get_active_hermes_home()
    if not raw:
        return api.get_active_hermes_home()
    if api._is_root_profile(raw):
        return api._DEFAULT_HERMES_HOME
    if not api._PROFILE_ID_RE.fullmatch(raw):
        api.logger.warning(
            "Cron job %s has invalid profile %r; falling back to server default",
            (job or {}).get("id", "?"),
            raw,
        )
        return api.get_active_hermes_home()
    home = api._resolve_named_profile_home(raw)
    if not home.is_dir():
        api.logger.warning(
            "Cron job %s references missing profile %r; falling back to server default",
            (job or {}).get("id", "?"),
            raw,
        )
        return api.get_active_hermes_home()
    return home


def install_cron_scheduler_profile_isolation() -> None:
    """Patch in-process scheduler runs with persisted profile isolation."""
    api = _profiles_module
    try:
        import cron.scheduler as cron_scheduler
    except ImportError:
        api.logger.debug(
            "install_cron_scheduler_profile_isolation: cron.scheduler unavailable"
        )
        return

    original = getattr(cron_scheduler, "run_job", None)
    if original is None or getattr(original, "_webui_profile_isolated", False):
        return

    def isolated_run_job(job, *args, **kwargs):
        if api._cron_profile_context_depth() > 0:
            return original(job, *args, **kwargs)
        try:
            with api.cron_profile_context_for_home(
                api._home_for_scheduled_cron_job(job)
            ):
                return original(job, *args, **kwargs)
        finally:
            event_profile = str((job or {}).get("profile") or "").strip() or None
            if api._is_isolated_profile_mode():
                event_profile = api._isolated_profile_name()
            try:
                api.publish_session_list_changed(
                    "cron_complete", profile=event_profile
                )
            except TypeError:
                api.publish_session_list_changed("cron_complete")

    isolated_run_job._webui_profile_isolated = True
    isolated_run_job._webui_original_run_job = original
    cron_scheduler.run_job = isolated_run_job


class CronProfileContextForHome:
    """Pin cron globals to an explicit profile home for a worker body."""

    def __init__(self, home: Path):
        self._home = Path(home)

    def __enter__(self):
        api = _profiles_module
        api._cron_env_lock.acquire()
        api._push_cron_profile_context_depth()
        try:
            self._prev_env = os.environ.get("HERMES_HOME")
            os.environ["HERMES_HOME"] = str(self._home)
            self._prev_cj = None
            try:
                import cron.jobs as cron_jobs

                self._prev_cj = (
                    cron_jobs.HERMES_DIR,
                    cron_jobs.CRON_DIR,
                    cron_jobs.JOBS_FILE,
                    cron_jobs.OUTPUT_DIR,
                )
                cron_jobs.HERMES_DIR = self._home
                cron_jobs.CRON_DIR = self._home / "cron"
                cron_jobs.JOBS_FILE = cron_jobs.CRON_DIR / "jobs.json"
                cron_jobs.OUTPUT_DIR = cron_jobs.CRON_DIR / "output"
            except (ImportError, AttributeError):
                api.logger.debug(
                    "cron_profile_context_for_home: cron.jobs unavailable"
                )

            self._prev_cs = None
            try:
                import cron.scheduler as cron_scheduler

                self._prev_cs = (
                    getattr(cron_scheduler, "_hermes_home", None),
                    getattr(cron_scheduler, "_LOCK_DIR", None),
                    getattr(cron_scheduler, "_LOCK_FILE", None),
                )
                cron_scheduler._hermes_home = self._home
                cron_scheduler._LOCK_DIR = self._home / "cron"
                cron_scheduler._LOCK_FILE = cron_scheduler._LOCK_DIR / ".tick.lock"
            except (ImportError, AttributeError):
                api.logger.debug(
                    "cron_profile_context_for_home: cron.scheduler unavailable"
                )
        except Exception:
            api._pop_cron_profile_context_depth()
            api._cron_env_lock.release()
            raise
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        api = _profiles_module
        try:
            if self._prev_env is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = self._prev_env
            if self._prev_cj is not None:
                try:
                    import cron.jobs as cron_jobs

                    (
                        cron_jobs.HERMES_DIR,
                        cron_jobs.CRON_DIR,
                        cron_jobs.JOBS_FILE,
                        cron_jobs.OUTPUT_DIR,
                    ) = self._prev_cj
                except (ImportError, AttributeError):
                    pass
            if self._prev_cs is not None:
                try:
                    import cron.scheduler as cron_scheduler

                    (
                        cron_scheduler._hermes_home,
                        cron_scheduler._LOCK_DIR,
                        cron_scheduler._LOCK_FILE,
                    ) = self._prev_cs
                except (ImportError, AttributeError):
                    pass
        finally:
            api._pop_cron_profile_context_depth()
            api._cron_env_lock.release()
        return False


class CronProfileContext:
    """Pin cron globals to the request/TLS-active profile for a request body."""

    def __enter__(self):
        api = _profiles_module
        api._cron_env_lock.acquire()
        api._push_cron_profile_context_depth()
        try:
            self._prev_env = os.environ.get("HERMES_HOME")
            home = api.get_active_hermes_home()
            os.environ["HERMES_HOME"] = str(home)
            self._prev_cj = None
            try:
                import cron.jobs as cron_jobs

                self._prev_cj = (
                    cron_jobs.HERMES_DIR,
                    cron_jobs.CRON_DIR,
                    cron_jobs.JOBS_FILE,
                    cron_jobs.OUTPUT_DIR,
                )
                cron_jobs.HERMES_DIR = home
                cron_jobs.CRON_DIR = home / "cron"
                cron_jobs.JOBS_FILE = cron_jobs.CRON_DIR / "jobs.json"
                cron_jobs.OUTPUT_DIR = cron_jobs.CRON_DIR / "output"
            except (ImportError, AttributeError):
                api.logger.debug(
                    "cron_profile_context: cron.jobs unavailable; env-var only"
                )
            self._prev_cs = None
            try:
                import cron.scheduler as cron_scheduler

                self._prev_cs = (
                    getattr(cron_scheduler, "_hermes_home", None),
                    getattr(cron_scheduler, "_LOCK_DIR", None),
                    getattr(cron_scheduler, "_LOCK_FILE", None),
                )
                cron_scheduler._hermes_home = home
                cron_scheduler._LOCK_DIR = home / "cron"
                cron_scheduler._LOCK_FILE = cron_scheduler._LOCK_DIR / ".tick.lock"
            except (ImportError, AttributeError):
                api.logger.debug(
                    "cron_profile_context: cron.scheduler unavailable; env-var only"
                )
        except Exception:
            api._pop_cron_profile_context_depth()
            api._cron_env_lock.release()
            raise
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        api = _profiles_module
        try:
            if self._prev_env is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = self._prev_env
            if self._prev_cj is not None:
                try:
                    import cron.jobs as cron_jobs

                    (
                        cron_jobs.HERMES_DIR,
                        cron_jobs.CRON_DIR,
                        cron_jobs.JOBS_FILE,
                        cron_jobs.OUTPUT_DIR,
                    ) = self._prev_cj
                except (ImportError, AttributeError):
                    pass
            if self._prev_cs is not None:
                try:
                    import cron.scheduler as cron_scheduler

                    (
                        cron_scheduler._hermes_home,
                        cron_scheduler._LOCK_DIR,
                        cron_scheduler._LOCK_FILE,
                    ) = self._prev_cs
                except (ImportError, AttributeError):
                    pass
        finally:
            api._pop_cron_profile_context_depth()
            api._cron_env_lock.release()
        return False
