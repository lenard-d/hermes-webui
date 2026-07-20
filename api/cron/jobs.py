"""Cron job mutation policy and selected-profile snapshots."""

from __future__ import annotations

import logging
import threading

from api.cron.profiles import job_for_api, normalize_profile


logger = logging.getLogger(__name__)
_SNAPSHOT_LOCK = threading.Lock()


def _selected_profile_snapshot_updates(
    profile: str | None,
    *,
    provider,
    model,
) -> dict[str, str | None]:
    selected_profile = str(profile or "").strip()
    if not selected_profile or (provider is not None and model is not None):
        return {}

    try:
        from api.profiles import profile_env_for_background_worker
        from cron.jobs import _compute_provider_model_snapshots
    except Exception:
        logger.warning(
            "Selected-profile cron snapshot repair unavailable; saving ambient snapshots",
            exc_info=True,
        )
        return {}

    try:
        with _SNAPSHOT_LOCK:
            with profile_env_for_background_worker(
                selected_profile,
                "cron create snapshot",
                logger_override=logger,
            ):
                provider_snapshot, model_snapshot = _compute_provider_model_snapshots(
                    provider=provider,
                    model=model,
                    base_url=None,
                    no_agent=False,
                )
    except Exception:
        logger.warning(
            "Selected-profile cron snapshot repair failed for %s; saving ambient snapshots",
            selected_profile,
            exc_info=True,
        )
        return {}

    updates = {}
    if provider is None:
        updates["provider_snapshot"] = provider_snapshot
    if model is None:
        updates["model_snapshot"] = model_snapshot
    return updates


def create_job_from_payload(payload: dict) -> dict:
    """Create a cron job and persist WebUI-owned optional fields.

    Schedule parsing and validation remain authoritative in ``cron.jobs``;
    WebUI does not maintain a competing cron-expression implementation.
    """
    from cron.jobs import create_job, update_job

    profile = normalize_profile(payload.get("profile"))
    toast_notifications = payload.get("toast_notifications") is not False
    requested_model = payload.get("model") or None
    requested_provider = payload.get("provider") or None
    job = create_job(
        prompt=payload["prompt"],
        schedule=payload["schedule"],
        name=payload.get("name") or None,
        deliver=payload.get("deliver") or "local",
        skills=payload.get("skills") or [],
        model=requested_model,
        provider=requested_provider,
    )
    updates = {}
    if profile is not None:
        updates["profile"] = profile
        updates.update(
            _selected_profile_snapshot_updates(
                profile,
                provider=requested_provider,
                model=requested_model,
            )
        )
    if not toast_notifications:
        updates["toast_notifications"] = False
    if updates:
        job = update_job(job["id"], updates) or job
    return job_for_api(job)


def update_job_from_payload(job_id: str, payload: dict) -> dict | None:
    from cron.jobs import update_job

    updates = {}
    for key, value in payload.items():
        if key == "job_id":
            continue
        if key == "profile":
            updates[key] = normalize_profile(value)
        elif key in ("model", "provider"):
            updates[key] = value if value else None
        elif value is not None:
            updates[key] = value
    job = update_job(job_id, updates)
    return job_for_api(job) if job else None


def delete_job(job_id: str) -> bool:
    from cron.jobs import remove_job

    return bool(remove_job(job_id))


def pause_job(job_id: str, *, reason=None) -> dict | None:
    from cron.jobs import pause_job as agent_pause_job

    return agent_pause_job(job_id, reason=reason)


def resume_job(job_id: str) -> dict | None:
    from cron.jobs import resume_job as agent_resume_job

    return agent_resume_job(job_id)


def delivery_options() -> list[dict[str, str]]:
    try:
        from cron.scheduler import _KNOWN_DELIVERY_PLATFORMS
    except Exception:
        _KNOWN_DELIVERY_PLATFORMS = frozenset()
    platforms = [
        {"value": "local", "label": "Local (save output only)"},
        {"value": "origin", "label": "Origin (reply to creator)"},
    ]
    platforms.extend(
        {"value": name, "label": name.capitalize()}
        for name in sorted(_KNOWN_DELIVERY_PLATFORMS)
    )
    return platforms
