"""Profile ownership and execution identity for cron jobs."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from api.profiles import profiles_match


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CronExecutionIdentity:
    """Homes and browser profile identity captured before a worker starts."""

    owner_home: Path
    execution_home: Path
    event_profile: str | None


def job_for_api(job: dict) -> dict:
    """Normalize optional WebUI fields without changing persisted legacy jobs."""
    payload = dict(job or {})
    payload.setdefault("profile", None)
    payload["toast_notifications"] = payload.get("toast_notifications") is not False
    return payload


def jobs_for_api(jobs) -> list[dict]:
    return [job_for_api(job) for job in (jobs or [])]


def list_jobs_across_profiles(active_profile: str) -> tuple[list[dict], list[dict]]:
    """Return active-profile rows and read-only foreign-profile rows.

    Row ownership is distinct from the persisted ``profile`` field.  The
    persisted field selects the execution profile; ``owner_profile`` records
    which profile home owns the jobs.json row.
    """
    from cron.jobs import list_jobs
    from api.profiles import (
        cron_profile_context_for_home,
        get_hermes_home_for_profile,
        list_profiles_api,
    )

    def home_key(path: Path) -> str:
        try:
            return str(Path(path).expanduser().resolve(strict=False))
        except Exception:
            return str(Path(path).expanduser())

    names: list[str] = []
    seen_names: set[str] = set()

    def add_name(raw_name) -> None:
        name = str(raw_name or "").strip()
        folded = name.casefold()
        if not name or folded in seen_names:
            return
        seen_names.add(folded)
        names.append(name)

    add_name(active_profile)
    for row in list_profiles_api():
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        if row.get("visible") is False and not profiles_match(name, active_profile):
            continue
        add_name(name)

    active_jobs: list[dict] = []
    other_jobs: list[dict] = []
    seen_homes: set[str] = set()
    for owner_profile in names:
        home = Path(get_hermes_home_for_profile(owner_profile))
        key = home_key(home)
        if key in seen_homes:
            continue
        seen_homes.add(key)
        is_active = profiles_match(owner_profile, active_profile)
        try:
            with cron_profile_context_for_home(home):
                jobs = jobs_for_api(list_jobs(include_disabled=True))
        except Exception:
            if not is_active:
                continue
            raise
        for job in jobs:
            row = dict(job)
            row["owner_profile"] = owner_profile
            row["read_only"] = not is_active
            (active_jobs if is_active else other_jobs).append(row)
    return active_jobs, other_jobs


def available_profile_names() -> set[str]:
    from api.profiles import list_profiles_api

    names = {"default"}
    for profile in list_profiles_api():
        try:
            name = str(profile.get("name") or "").strip()
        except AttributeError:
            continue
        if name:
            names.add(name)
    return names


def normalize_profile(value) -> str | None:
    if value is None:
        return None
    profile = str(value).strip()
    if not profile:
        return None
    if profile not in available_profile_names():
        raise ValueError(f"Unknown profile: {profile}")
    return profile


def event_profile_for_job(job: dict) -> str | None:
    """Return the profile identity browsers should refresh after a run."""
    raw = str((job or {}).get("profile") or "").strip()
    if not raw or raw not in available_profile_names():
        return None
    return raw


def execution_identity_for_job(job: dict) -> CronExecutionIdentity:
    """Capture owner and execution homes before leaving request-local context.

    Missing/deleted execution profiles preserve legacy server-default behavior.
    Profile lookup failures themselves are deliberately not swallowed: running
    unpinned would risk cross-profile state corruption.
    """
    from api.profiles import get_active_hermes_home, get_hermes_home_for_profile

    owner_home = Path(get_active_hermes_home())
    raw = str((job or {}).get("profile") or "").strip()
    if not raw:
        execution_home = owner_home
        event_profile = None
    elif raw not in available_profile_names():
        logger.warning(
            "Cron job %s references missing profile %r; falling back to server default",
            (job or {}).get("id", "?"),
            raw,
        )
        execution_home = owner_home
        event_profile = None
    else:
        execution_home = Path(get_hermes_home_for_profile(raw))
        event_profile = raw
    return CronExecutionIdentity(owner_home, execution_home, event_profile)
