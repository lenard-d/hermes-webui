"""Atomic manual-run admission, subprocess execution, and persistence."""

from __future__ import annotations

import importlib
import logging
import threading
import time
from dataclasses import dataclass

from api.cron.profiles import CronExecutionIdentity


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StartResult:
    started: bool
    elapsed: float = 0.0


class ManualRunRegistry:
    """Own running-job state and make duplicate admission atomic."""

    def __init__(self):
        self._lock = threading.Lock()
        self._started_at: dict[str, float] = {}

    def claim(self, job_id: str) -> StartResult:
        now = time.time()
        with self._lock:
            started_at = self._started_at.get(job_id)
            if started_at is not None:
                return StartResult(False, max(0.0, now - started_at))
            self._started_at[job_id] = now
            return StartResult(True)

    def finish(self, job_id: str) -> None:
        with self._lock:
            self._started_at.pop(job_id, None)

    def status(self, job_id: str) -> tuple[bool, float]:
        with self._lock:
            started_at = self._started_at.get(job_id)
        if started_at is None:
            return False, 0.0
        return True, max(0.0, time.time() - started_at)

    def snapshot(self) -> dict[str, float]:
        now = time.time()
        with self._lock:
            return {
                job_id: max(0.0, now - started_at)
                for job_id, started_at in self._started_at.items()
            }


_RUNS = ManualRunRegistry()


def running_status(job_id: str) -> tuple[bool, float]:
    return _RUNS.status(job_id)


def running_snapshot() -> dict[str, float]:
    return _RUNS.snapshot()


def _cron_job_subprocess_main(job, execution_profile_home, result_queue):
    """Run one job inside a child process pinned to an execution profile."""
    try:
        def run():
            from cron.scheduler import run_job

            return run_job(job)

        if execution_profile_home is None:
            result = run()
        else:
            from api.profiles import cron_profile_context_for_home

            with cron_profile_context_for_home(execution_profile_home):
                result = run()
        result_queue.put(("ok", result))
    except BaseException as exc:  # pragma: no cover - surfaced in parent
        import traceback

        result_queue.put(("error", f"{type(exc).__name__}: {exc}", traceback.format_exc()))


def subprocess_result_timeout_seconds(job) -> float:
    for key in ("timeout_seconds", "max_runtime_seconds", "timeout"):
        raw = (job or {}).get(key)
        if raw in (None, ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return max(60.0, value + 30.0)
    return 6 * 60 * 60.0


def run_in_profile_subprocess(job, execution_profile_home):
    """Execute ``cron.scheduler.run_job`` without holding the parent env lock."""
    import multiprocessing
    import queue

    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue(maxsize=1)
    process = ctx.Process(
        target=_cron_job_subprocess_main,
        args=(job, execution_profile_home, result_queue),
    )
    process.start()

    result_timeout = subprocess_result_timeout_seconds(job)
    status = "error"
    payload = ["cron run subprocess failed before producing a result", ""]
    try:
        try:
            # Drain before joining: a large Queue payload can otherwise block
            # the child feeder thread and deadlock the parent join.
            status, *payload = result_queue.get(timeout=result_timeout)
        except queue.Empty:
            status = "error"
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
                payload = [
                    f"cron run subprocess produced no result within {result_timeout:g}s and was terminated",
                    "",
                ]
            else:
                payload = [
                    f"cron run subprocess exited with code {process.exitcode} without producing a result",
                    "",
                ]
        finally:
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
                if status == "ok":
                    status = "error"
                    payload = [
                        "cron run subprocess did not exit after returning a result",
                        "",
                    ]
    finally:
        result_queue.close()
        result_queue.join_thread()

    if status == "ok":
        return payload[0]
    message = payload[0]
    traceback_text = payload[1] if len(payload) > 1 else ""
    if traceback_text:
        logger.error("Manual cron subprocess failed:\n%s", traceback_text)
    raise RuntimeError(message)


def _with_cron_home(home, operation):
    if home is None:
        return operation()
    from api.profiles import cron_profile_context_for_home

    with cron_profile_context_for_home(home):
        return operation()


def run_tracked(job, identity: CronExecutionIdentity) -> None:
    """Execute, persist, deliver, and release one admitted manual run."""
    from cron.jobs import mark_job_run, save_job_output

    scheduler = importlib.import_module("cron.scheduler")
    silent_marker = getattr(scheduler, "SILENT_MARKER", "[SILENT]")
    deliver_result = getattr(scheduler, "_deliver_result", None)
    job_id = str(job.get("id", "") or "")

    try:
        success, output, final_response, error = run_in_profile_subprocess(
            job, identity.execution_home
        )

        def persist_success():
            save_job_output(job_id, output)
            delivery_content = (
                final_response
                if success
                else f"⚠️ Cron job '{job.get('name', job_id)}' failed:\n{error}"
            )
            should_deliver = bool(delivery_content)
            if success and silent_marker in delivery_content.strip().upper():
                should_deliver = False

            delivery_error = None
            if should_deliver and deliver_result is not None:
                try:
                    delivery_error = deliver_result(job, delivery_content)
                except Exception as delivery_exception:
                    delivery_error = str(delivery_exception)
                    logger.error(
                        "Delivery failed for manual cron job %s: %s",
                        job_id,
                        delivery_exception,
                    )

            persisted_success, persisted_error = success, error
            if persisted_success and not final_response:
                persisted_success = False
                persisted_error = (
                    "Agent completed but produced empty response "
                    "(model error, timeout, or misconfiguration)"
                )
            try:
                mark_job_run(
                    job_id,
                    persisted_success,
                    persisted_error,
                    delivery_error=delivery_error,
                )
            except TypeError:
                mark_job_run(job_id, persisted_success, persisted_error)

        _with_cron_home(identity.owner_home, persist_success)
    except Exception as error:
        logger.exception("Manual cron run failed for job %s", job_id)
        error_message = str(error)

        def persist_failure():
            mark_job_run(job_id, False, error_message)

        try:
            _with_cron_home(identity.owner_home, persist_failure)
        except Exception:
            logger.debug("Failed to mark manual cron run failure for %s", job_id)
    finally:
        _RUNS.finish(job_id)
        from api.sessions.title_publication import _publish_session_list_changed

        _publish_session_list_changed("cron_complete", profile=identity.event_profile)


def start_manual_run(job: dict, identity: CronExecutionIdentity) -> StartResult:
    """Atomically admit and start one manual run.

    If thread creation fails after admission, the claim is released so the job
    cannot remain permanently stuck in the running state.
    """
    job_id = str(job.get("id", "") or "")
    result = _RUNS.claim(job_id)
    if not result.started:
        return result
    thread = threading.Thread(target=run_tracked, args=(job, identity), daemon=True)
    try:
        thread.start()
    except BaseException:
        _RUNS.finish(job_id)
        raise
    return result
