"""HTTP adapters for cron task queries and mutations."""

from __future__ import annotations

import logging
from urllib.parse import parse_qs

from api.cron.completions import recent_completions
from api.cron.jobs import (
    create_job_from_payload,
    delete_job,
    delivery_options,
    pause_job,
    resume_job,
    update_job_from_payload,
)
from api.cron.manual_runs import running_snapshot, running_status, start_manual_run
from api.cron.output_history import (
    CronRunNotFound,
    InvalidCronOutputPath,
    active_output_store,
    response_snippet,
    usage_metadata,
    validate_job_id,
)
from api.cron.profiles import execution_identity_for_job
from api.helpers import bad, j, require


logger = logging.getLogger(__name__)


def _handle_cron_history(handler, parsed):
    qs = parse_qs(parsed.query)
    job_id = qs.get("job_id", [""])[0]
    if not job_id:
        return j(handler, {"error": "job_id required"}, status=400)
    try:
        validate_job_id(job_id)
        offset = max(0, int(qs.get("offset", ["0"])[0]))
        limit = max(1, min(500, int(qs.get("limit", ["50"])[0])))
    except InvalidCronOutputPath as error:
        return j(handler, {"error": str(error)}, status=400)
    except (ValueError, TypeError):
        return j(handler, {"error": "offset and limit must be integers"}, status=400)

    runs, total = active_output_store().list_runs(job_id, offset=offset, limit=limit)
    return j(
        handler,
        {"job_id": job_id, "runs": runs, "total": total, "offset": offset},
    )


def _handle_cron_run_detail(handler, parsed):
    qs = parse_qs(parsed.query)
    job_id = qs.get("job_id", [""])[0]
    filename = qs.get("filename", [""])[0]
    if not job_id or not filename:
        return j(handler, {"error": "job_id and filename required"}, status=400)
    try:
        content = active_output_store().read_run(job_id, filename)
    except InvalidCronOutputPath as error:
        return j(handler, {"error": str(error)}, status=400)
    except CronRunNotFound:
        return j(handler, {"error": "run not found"}, status=404)
    except Exception as error:
        return j(handler, {"error": str(error)}, status=500)
    return j(
        handler,
        {
            "job_id": job_id,
            "filename": filename,
            "content": content,
            "snippet": response_snippet(content),
            "usage": usage_metadata(content),
        },
    )


def _handle_cron_output(handler, parsed):
    qs = parse_qs(parsed.query)
    job_id = qs.get("job_id", [""])[0]
    if not job_id:
        return j(handler, {"error": "job_id required"}, status=400)
    try:
        limit = max(1, min(500, int(qs.get("limit", ["5"])[0])))
    except (ValueError, TypeError):
        limit = 5
    try:
        outputs = active_output_store().recent_outputs(job_id, limit=limit)
    except InvalidCronOutputPath as error:
        return j(handler, {"error": str(error)}, status=400)
    return j(handler, {"job_id": job_id, "outputs": outputs})


def _handle_cron_status(handler, parsed):
    qs = parse_qs(parsed.query)
    job_id = qs.get("job_id", [""])[0]
    if job_id:
        running, elapsed = running_status(job_id)
        return j(
            handler,
            {"job_id": job_id, "running": running, "elapsed": round(elapsed, 1)},
        )
    return j(
        handler,
        {
            "running": {
                running_job_id: round(elapsed, 1)
                for running_job_id, elapsed in running_snapshot().items()
            }
        },
    )


def _handle_cron_recent(handler, parsed):
    qs = parse_qs(parsed.query)
    try:
        since = float(qs.get("since", ["0"])[0])
    except (ValueError, TypeError):
        since = 0.0
    try:
        from cron.jobs import list_jobs

        jobs = list_jobs(include_disabled=True)
    except ImportError:
        return j(handler, {"completions": [], "since": since})
    return j(
        handler,
        {"completions": recent_completions(jobs, since), "since": since},
    )


def _handle_cron_create(handler, body):
    try:
        require(body, "prompt", "schedule")
    except ValueError as error:
        return bad(handler, str(error))
    try:
        job = create_job_from_payload(body)
    except Exception as error:
        return j(handler, {"error": str(error)}, status=400)
    return j(handler, {"ok": True, "job": job})


def _handle_cron_delivery_options(handler):
    return j(handler, {"platforms": delivery_options()})


def _handle_cron_update(handler, body):
    try:
        require(body, "job_id")
        job = update_job_from_payload(body["job_id"], body)
    except ValueError as error:
        return bad(handler, str(error))
    if not job:
        return bad(handler, "Job not found", 404)
    return j(handler, {"ok": True, "job": job})


def _handle_cron_delete(handler, body):
    try:
        require(body, "job_id")
    except ValueError as error:
        return bad(handler, str(error))
    if not delete_job(body["job_id"]):
        return bad(handler, "Job not found", 404)
    return j(handler, {"ok": True, "job_id": body["job_id"]})


def _handle_cron_run(handler, body):
    job_id = body.get("job_id", "")
    if not job_id:
        return bad(handler, "job_id required")
    from cron.jobs import get_job

    job = get_job(job_id)
    if not job:
        return bad(handler, "Job not found", 404)

    # Capture request-local owner identity before the daemon thread starts.
    # Resolution errors must surface rather than silently running unpinned.
    identity = execution_identity_for_job(job)
    result = start_manual_run(job, identity)
    if not result.started:
        return j(
            handler,
            {
                "ok": False,
                "job_id": job_id,
                "status": "already_running",
                "elapsed": round(result.elapsed, 1),
            },
        )
    return j(handler, {"ok": True, "job_id": job_id, "status": "running"})


def _handle_cron_pause(handler, body):
    job_id = body.get("job_id", "")
    if not job_id:
        return bad(handler, "job_id required")
    result = pause_job(job_id, reason=body.get("reason"))
    if result:
        return j(handler, {"ok": True, "job": result})
    return bad(handler, "Job not found", 404)


def _handle_cron_resume(handler, body):
    job_id = body.get("job_id", "")
    if not job_id:
        return bad(handler, "job_id required")
    result = resume_job(job_id)
    if result:
        return j(handler, {"ok": True, "job": result})
    return bad(handler, "Job not found", 404)


__routes_exports__ = (
    "_handle_cron_history",
    "_handle_cron_run_detail",
    "_handle_cron_output",
    "_handle_cron_status",
    "_handle_cron_recent",
    "_handle_cron_create",
    "_handle_cron_delivery_options",
    "_handle_cron_update",
    "_handle_cron_delete",
    "_handle_cron_run",
    "_handle_cron_pause",
    "_handle_cron_resume",
)
