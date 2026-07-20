"""Cron task listing, execution, output, and mutation route domain."""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

if TYPE_CHECKING:
    from api.helpers import bad, j, require
    from api.sessions.store import _active_state_db_path
    from api.profiles import profiles_match as _profiles_match
    from api.routes import _publish_session_list_changed, logger

_RUNNING_CRON_JOBS: dict[str, float] = {}

_RUNNING_CRON_LOCK = threading.Lock()

_CRON_CREATE_SNAPSHOT_LOCK = threading.Lock()

_CRON_OUTPUT_CONTENT_LIMIT = 8000

_CRON_OUTPUT_HEADER_CONTEXT = 200

def _normalize_cron_job_ids(job_ids) -> list[str]:
    seen = set()
    normalized = []
    for job_id in job_ids or []:
        jid = str(job_id or "").strip()
        if not jid or jid in seen:
            continue
        seen.add(jid)
        normalized.append(jid)
    return normalized

def _latest_cron_session_info_for_jobs(
    job_ids, completed_job_ids=None
) -> dict[str, dict[str, int | str | None]]:
    """Return newest persisted cron session info keyed by completed cron job id."""
    normalized = _normalize_cron_job_ids(job_ids)
    requested = _normalize_cron_job_ids(completed_job_ids if completed_job_ids is not None else job_ids)
    if not requested:
        return {}
    if not normalized:
        return {jid: {"session_id": "", "message_count": None} for jid in requested}
    db_path = _active_state_db_path()
    if not db_path or not Path(db_path).exists():
        return {jid: {"session_id": "", "message_count": None} for jid in requested}
    try:
        with closing(sqlite3.connect(str(db_path))) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(sessions)")
            session_cols = {row[1] for row in cur.fetchall()}
            if "id" not in session_cols or "source" not in session_cols:
                return {jid: {"session_id": "", "message_count": None} for jid in requested}
            select_message_count = (
                "s.message_count AS message_count"
                if "message_count" in session_cols
                else "NULL AS message_count"
            )
            if "started_at" in session_cols:
                query = f"""
                    SELECT s.id,
                           {select_message_count}
                    FROM sessions s
                    WHERE LOWER(COALESCE(s.source, '')) = 'cron'
                    ORDER BY COALESCE(s.started_at, 0) DESC, s.id DESC  -- newest start, not last activity
                """
            else:
                query = f"""
                    SELECT s.id,
                           {select_message_count}
                    FROM sessions s
                    WHERE LOWER(COALESCE(s.source, '')) = 'cron'
                    ORDER BY s.id DESC
                """
            cur.execute(query)
            results = {
                jid: {"session_id": "", "message_count": None} for jid in requested
            }
            requested_ids = set(requested)
            prefixes = {jid: f"cron_{jid}_" for jid in normalized}
            for row in cur.fetchall():
                sid = str(row["id"] or "")
                if not sid:
                    continue
                matches = [
                    jid
                    for jid in normalized
                    if sid.startswith(prefixes[jid])
                ]
                if matches:
                    jid = max(matches, key=len)
                    if jid not in requested_ids or results[jid]["session_id"]:
                        continue
                    results[jid] = {
                        "session_id": sid,
                        "message_count": (
                            int(row["message_count"])
                            if row["message_count"] is not None
                            else None
                        ),
                    }
                if all(info["session_id"] for info in results.values()):
                    break
            return results
    except sqlite3.Error:
        return {jid: {"session_id": "", "message_count": None} for jid in requested}

def _mark_cron_running(job_id: str):
    with _RUNNING_CRON_LOCK:
        _RUNNING_CRON_JOBS[job_id] = time.time()

def _mark_cron_done(job_id: str):
    with _RUNNING_CRON_LOCK:
        _RUNNING_CRON_JOBS.pop(job_id, None)

def _is_cron_running(job_id: str) -> tuple[bool, float]:
    """Return (is_running, elapsed_seconds)."""
    with _RUNNING_CRON_LOCK:
        t = _RUNNING_CRON_JOBS.get(job_id)
        if t is None:
            return False, 0.0
        return True, time.time() - t

def _cron_response_marker_index(text: str) -> int:
    """Return the start index of a markdown Response heading, if present."""
    candidates = []
    for heading in ("## Response", "# Response"):
        if text.startswith(heading):
            candidates.append(0)
        idx = text.find(f"\n{heading}")
        if idx >= 0:
            candidates.append(idx + 1)
    return min(candidates) if candidates else -1

def _cron_output_content_window(text: str, limit: int = _CRON_OUTPUT_CONTENT_LIMIT) -> str:
    """Return a bounded cron output window that preserves useful response text.

    Cron output files can contain large skill dumps in the Prompt section. The
    UI already extracts ``## Response`` when present, so keep that section in
    the API payload instead of blindly returning the first ``limit`` chars.
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text

    response_idx = _cron_response_marker_index(text)
    if response_idx >= 0:
        header = text[:min(_CRON_OUTPUT_HEADER_CONTEXT, response_idx)].rstrip()
        response = text[response_idx:].lstrip("\n")
        content = f"{header}\n...\n{response}" if header else response
        return content[:limit]

    return text[-limit:]

def _cron_job_for_api(job: dict) -> dict:
    """Return a cron job payload with optional UI settings normalized.

    Legacy jobs intentionally persist without ``profile`` so they keep the
    scheduler's server-default behavior. The API still returns ``profile: None``
    so the UI can label that state explicitly instead of guessing.

    ``toast_notifications`` is a WebUI preference for completion toasts. Legacy
    jobs default to enabled so existing behavior is preserved unless a job is
    explicitly muted.
    """
    payload = dict(job or {})
    payload.setdefault("profile", None)
    payload["toast_notifications"] = payload.get("toast_notifications") is not False
    return payload

def _cron_jobs_for_api(jobs) -> list[dict]:
    return [_cron_job_for_api(job) for job in (jobs or [])]

_AGENT_CRON_IMPORT_PATH_LOCK = threading.Lock()

_AGENT_CRON_IMPORT_PATH_READY: str | None = None

def _ensure_agent_cron_import_path() -> None:
    """Prefer the agent's cron package over unrelated top-level cron packages."""
    try:
        from api import config as api_config
    except Exception:
        return

    agent_dir = getattr(api_config, "_AGENT_DIR", None)
    if not agent_dir:
        return
    agent_path = str(Path(agent_dir).expanduser().resolve())
    agent_cron_path = str(Path(agent_path) / "cron")

    global _AGENT_CRON_IMPORT_PATH_READY
    with _AGENT_CRON_IMPORT_PATH_LOCK:
        cron_mod = sys.modules.get("cron")
        cron_file = str(getattr(cron_mod, "__file__", "") or "") if cron_mod else ""
        cron_is_agent = bool(cron_mod is not None and cron_file.startswith(agent_cron_path + os.sep))
        if _AGENT_CRON_IMPORT_PATH_READY == agent_path and (cron_mod is None or cron_is_agent):
            return

        while agent_path in sys.path:
            sys.path.remove(agent_path)
        shadow_indexes = [
            idx
            for idx, path_entry in enumerate(sys.path)
            if path_entry
            and Path(path_entry).resolve() != Path(agent_path)
            and (Path(path_entry) / "cron" / "__init__.py").exists()
        ]
        if shadow_indexes:
            sys.path.insert(min(shadow_indexes), agent_path)
        else:
            sys.path.append(agent_path)
        _AGENT_CRON_IMPORT_PATH_READY = agent_path

        # Keep in-memory test doubles or namespace stubs intact; only evict a
        # real on-disk shadow package so the agent's cron package can import.
        if cron_mod is not None and cron_file and not cron_is_agent:
            for name in list(sys.modules):
                if name == "cron" or name.startswith("cron."):
                    sys.modules.pop(name, None)

def _cron_jobs_cross_profile(active_profile: str) -> tuple[list[dict], list[dict]]:
    """Return active-profile rows plus foreign rows for the Tasks panel.

    Row ownership is intentionally distinct from a cron job's persisted
    ``profile`` field. The persisted field controls where the job executes;
    ``owner_profile`` tells the UI which profile home the row came from.
    """
    from cron.jobs import list_jobs
    from api.profiles import (
        cron_profile_context_for_home,
        get_hermes_home_for_profile,
        list_profiles_api,
    )

    def _home_key(path: Path) -> str:
        try:
            return str(Path(path).expanduser().resolve(strict=False))
        except Exception:
            return str(Path(path).expanduser())

    names: list[str] = []
    seen_names: set[str] = set()

    def _add_name(raw_name) -> None:
        name = str(raw_name or "").strip()
        if not name:
            return
        folded = name.casefold()
        if folded in seen_names:
            return
        seen_names.add(folded)
        names.append(name)

    _add_name(active_profile)
    for row in list_profiles_api():
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        if row.get("visible") is False and not _profiles_match(name, active_profile):
            continue
        _add_name(name)

    active_jobs: list[dict] = []
    other_jobs: list[dict] = []
    seen_homes: set[str] = set()
    for owner_profile in names:
        home = Path(get_hermes_home_for_profile(owner_profile))
        home_key = _home_key(home)
        if home_key in seen_homes:
            continue
        seen_homes.add(home_key)
        is_active = _profiles_match(owner_profile, active_profile)
        try:
            with cron_profile_context_for_home(home):
                jobs = _cron_jobs_for_api(list_jobs(include_disabled=True))
        except Exception:
            if not is_active:
                continue
            raise
        for job in jobs:
            row = dict(job)
            row["owner_profile"] = owner_profile
            row["read_only"] = not is_active
            if is_active:
                active_jobs.append(row)
            else:
                other_jobs.append(row)
    return active_jobs, other_jobs

def _available_cron_profile_names() -> set[str]:
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

def _normalize_cron_profile_value(value) -> str | None:
    if value is None:
        return None
    profile = str(value).strip()
    if not profile:
        return None
    if profile not in _available_cron_profile_names():
        raise ValueError(f"Unknown profile: {profile}")
    return profile

def _profile_home_for_cron_job(job: dict):
    """Resolve the execution profile for a cron job, with graceful fallback.

    A missing/blank profile preserves legacy server-default behavior. If a job
    points at a profile that was deleted after save, fall back to the active
    server profile and log a warning instead of crashing the Run Now path.
    """
    from api.profiles import get_active_hermes_home, get_hermes_home_for_profile

    raw = str((job or {}).get("profile") or "").strip()
    if not raw:
        return get_active_hermes_home()
    if raw not in _available_cron_profile_names():
        logger.warning(
            "Cron job %s references missing profile %r; falling back to server default",
            (job or {}).get("id", "?"), raw,
        )
        return get_active_hermes_home()
    return get_hermes_home_for_profile(raw)

def _event_profile_for_cron_job(job: dict) -> str | None:
    """Return the profile identity browsers should refresh for a manual cron run."""
    raw = str((job or {}).get("profile") or "").strip()
    if not raw:
        return None
    if raw not in _available_cron_profile_names():
        return None
    return raw

def _cron_job_subprocess_main(job, execution_profile_home, result_queue):
    """Run one cron job inside a child process pinned to a profile home."""
    try:
        def _run():
            from cron.scheduler import run_job

            return run_job(job)

        if execution_profile_home is None:
            result = _run()
        else:
            from api.profiles import cron_profile_context_for_home

            with cron_profile_context_for_home(execution_profile_home):
                result = _run()
        result_queue.put(("ok", result))
    except BaseException as exc:  # pragma: no cover - surfaced in parent
        import traceback

        result_queue.put(("error", f"{type(exc).__name__}: {exc}", traceback.format_exc()))

def _cron_subprocess_result_timeout_seconds(job):
    """Return how long the manual-run parent waits for child result payloads."""
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
    # Manual cron jobs can legitimately run for a long time.  Keep a recovery
    # path for wedged children without truncating normal long-running jobs.
    return 6 * 60 * 60.0

def _run_cron_job_in_profile_subprocess(job, execution_profile_home):
    """Execute cron.scheduler.run_job without holding the parent cron env lock.

    cron.scheduler/cron.jobs still rely on process-global HERMES_HOME and module
    constants, so running the job body in a child process gives each long cron
    execution its own globals. The parent process only uses cron_profile_context
    for short metadata reads/writes and remains responsive to unrelated cron UI
    and API calls while the job runs.
    """
    import multiprocessing
    import queue

    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue(maxsize=1)
    process = ctx.Process(
        target=_cron_job_subprocess_main,
        args=(job, execution_profile_home, result_queue),
    )
    process.start()

    result_timeout = _cron_subprocess_result_timeout_seconds(job)
    status = "error"
    payload = ["cron run subprocess failed before producing a result", ""]
    try:
        try:
            # Drain the potentially large pickled result before joining.  If the
            # child puts >~64 KiB on a multiprocessing.Queue, joining first can
            # deadlock while the child's feeder thread waits for the parent to
            # read from the pipe.
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

def _run_cron_tracked(
    job,
    profile_home=None,
    execution_profile_home=None,
    event_profile=None,
):
    """Wrapper that tracks running state around cron.scheduler.run_job.

    ``profile_home`` is the cron store that owns the job row/output metadata.
    ``execution_profile_home`` is the selected per-job profile used to load
    agent config/.env while running. When no job profile is selected, both homes
    are the same and legacy server-default behavior is preserved.
    """
    import importlib

    from cron.jobs import mark_job_run, save_job_output

    _cron_scheduler = importlib.import_module("cron.scheduler")

    _silent_marker = getattr(_cron_scheduler, "SILENT_MARKER", "[SILENT]")
    _deliver_result = getattr(_cron_scheduler, "_deliver_result", None)

    job_id = job.get("id", "")
    execution_profile_home = execution_profile_home or profile_home

    def _with_cron_home(home, fn):
        if home is None:
            return fn()
        from api.profiles import cron_profile_context_for_home

        with cron_profile_context_for_home(home):
            return fn()

    try:
        success, output, final_response, error = _run_cron_job_in_profile_subprocess(
            job, execution_profile_home
        )

        # Persist output, deliver the same content the scheduled cron path would
        # send, and write run metadata back to the job's owning cron store even
        # when the selected execution profile is different.
        def _persist_success():
            save_job_output(job_id, output)

            deliver_content = (
                final_response
                if success
                else f"⚠️ Cron job '{job.get('name', job_id)}' failed:\n{error}"
            )
            should_deliver = bool(deliver_content)
            if should_deliver and success and _silent_marker in deliver_content.strip().upper():
                should_deliver = False

            delivery_error = None
            if should_deliver and _deliver_result is not None:
                try:
                    delivery_error = _deliver_result(job, deliver_content)
                except Exception as de:
                    delivery_error = str(de)
                    logger.error("Delivery failed for manual cron job %s: %s", job_id, de)

            # Match the scheduled cron path: an apparently successful run with no
            # final response should not leave the job looking healthy.
            _success, _error = success, error
            if _success and not final_response:
                _success = False
                _error = "Agent completed but produced empty response (model error, timeout, or misconfiguration)"

            try:
                mark_job_run(job_id, _success, _error, delivery_error=delivery_error)
            except TypeError:
                # Older/fake cron.jobs modules used by focused WebUI tests may
                # not expose the newer delivery_error parameter. Real Hermes
                # scheduler builds do, so this is only a compatibility shim for
                # legacy test doubles and deployments.
                mark_job_run(job_id, _success, _error)

        _with_cron_home(profile_home, _persist_success)
    except Exception as e:  # noqa: F841  consumed synchronously by the lambda below
        logger.exception("Manual cron run failed for job %s", job_id)
        try:
            _with_cron_home(profile_home, lambda: mark_job_run(job_id, False, str(e)))  # noqa: F821  e is bound by the enclosing `except ... as e` and the lambda runs synchronously here
        except Exception:
            logger.debug("Failed to mark manual cron run failure for %s", job_id)
    finally:
        _mark_cron_done(job_id)
        _publish_session_list_changed("cron_complete", profile=event_profile)

def _handle_cron_history(handler, parsed):
    """List cron run output files with metadata (no content).

    Returns lightweight file listing so the frontend can render a run history
    without fetching full output for every run.
    """
    from cron.jobs import OUTPUT_DIR as CRON_OUT
    import re as _re

    qs = parse_qs(parsed.query)
    job_id = qs.get("job_id", [""])[0]
    if not job_id:
        return j(handler, {"error": "job_id required"}, status=400)
    # Defense-in-depth: cron job_ids are 12-char hex from the agent's scheduler.
    # Without validation, a job_id of "../<other>" would let an authenticated
    # caller enumerate .md filenames in adjacent directories under CRON_OUT's
    # parent. Mirror the rollback checkpoint id regex shape.
    # (Opus pre-release advisor finding.)
    if not _re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}", job_id) or job_id in (".", ".."):
        return j(handler, {"error": "invalid job_id"}, status=400)
    # Reject malformed offset/limit instead of letting int() raise ValueError
    # and surface as a confusing 500. Clamp to safe ranges.
    try:
        offset = max(0, int(qs.get("offset", ["0"])[0]))
        limit = max(1, min(500, int(qs.get("limit", ["50"])[0])))
    except (ValueError, TypeError):
        return j(handler, {"error": "offset and limit must be integers"}, status=400)
    out_dir = CRON_OUT / job_id
    runs = []
    total = 0
    if out_dir.exists():
        all_files = sorted(out_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
        total = len(all_files)
        page = all_files[offset:offset + limit]
        for f in page:
            try:
                st = f.stat()
                usage = _cron_output_usage_metadata(
                    f.read_text(encoding="utf-8", errors="replace")
                )
                runs.append({
                    "filename": f.name,
                    "size": st.st_size,
                    "modified": st.st_mtime,
                    "usage": usage,
                })
            except OSError:
                logger.debug("Failed to stat cron output file %s", f)
    return j(handler, {"job_id": job_id, "runs": runs, "total": total, "offset": offset})

def _handle_cron_run_detail(handler, parsed):
    """Return full content of a single cron run output file."""
    from cron.jobs import OUTPUT_DIR as CRON_OUT
    import re as _re

    qs = parse_qs(parsed.query)
    job_id = qs.get("job_id", [""])[0]
    filename = qs.get("filename", [""])[0]
    if not job_id or not filename:
        return j(handler, {"error": "job_id and filename required"}, status=400)
    # Validate job_id shape (defense-in-depth even though the resolve+is_relative_to
    # check below catches traversal — fail-closed at the parameter boundary so
    # malformed job_ids return a 400 from the validator rather than a 400 from
    # the path resolver).
    if not _re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}", job_id) or job_id in (".", ".."):
        return j(handler, {"error": "invalid job_id"}, status=400)
    # Prevent path traversal — resolve and verify it stays within the job's output dir
    fpath = (CRON_OUT / job_id / filename).resolve()
    if not fpath.is_relative_to(CRON_OUT.resolve()):
        return j(handler, {"error": "invalid filename"}, status=400)
    if not fpath.exists():
        return j(handler, {"error": "run not found"}, status=404)
    try:
        content = fpath.read_text(encoding="utf-8", errors="replace")
        snippet = _cron_output_snippet(content)
        usage = _cron_output_usage_metadata(content)
        return j(handler, {"job_id": job_id, "filename": filename,
                           "content": content, "snippet": snippet,
                           "usage": usage})
    except Exception as e:
        return j(handler, {"error": str(e)}, status=500)

def _cron_output_usage_metadata(text: str) -> dict:
    """Extract optional token/cost metadata from a cron output markdown file."""
    import re as _re

    head = text.split("## Response", 1)[0].split("# Response", 1)[0]
    usage: dict = {}

    def _intish(value: str):
        cleaned = _re.sub(r"[^0-9]", "", value or "")
        return int(cleaned) if cleaned else None

    def _floatish(value: str):
        match = _re.search(r"[-+]?\d+(?:\.\d+)?", (value or "").replace(",", ""))
        return float(match.group(0)) if match else None

    for raw_line in head.splitlines():
        line = raw_line.strip()
        model_match = _re.match(r"\*\*(?:Model|Model Used):\*\*\s*(.+)$", line, _re.I)
        if model_match:
            usage["model"] = model_match.group(1).strip()
            continue
        provider_match = _re.match(r"\*\*Provider:\*\*\s*(.+)$", line, _re.I)
        if provider_match:
            usage["provider"] = provider_match.group(1).strip()
            continue
        cost_match = _re.match(r"\*\*(?:Estimated cost|Cost):\*\*\s*(.+)$", line, _re.I)
        if cost_match:
            cost = _floatish(cost_match.group(1))
            if cost is not None:
                usage["estimated_cost_usd"] = cost
            continue
        duration_match = _re.match(r"\*\*(?:Duration|Elapsed):\*\*\s*(.+)$", line, _re.I)
        if duration_match:
            seconds = _floatish(duration_match.group(1))
            if seconds is not None:
                usage["duration_seconds"] = seconds
            continue
        tokens_match = _re.match(r"\*\*Tokens:\*\*\s*(.+)$", line, _re.I)
        if tokens_match:
            value = tokens_match.group(1)
            input_match = _re.search(r"([0-9][0-9,]*)\s*(?:input|in)\b", value, _re.I)
            output_match = _re.search(r"([0-9][0-9,]*)\s*(?:output|out)\b", value, _re.I)
            total_match = _re.search(r"([0-9][0-9,]*)\s*(?:total\s*)?tokens?\b", value, _re.I)
            if input_match:
                usage["input_tokens"] = _intish(input_match.group(1))
            if output_match:
                usage["output_tokens"] = _intish(output_match.group(1))
            if total_match and "total_tokens" not in usage:
                usage["total_tokens"] = _intish(total_match.group(1))

    if "total_tokens" not in usage:
        total = sum(int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens"))
        if total:
            usage["total_tokens"] = total
    return usage

def _cron_output_snippet(text: str, limit: int = 600) -> str:
    """Extract the response body from a cron output .md file for preview.

    Contract: cron output files use markdown front-matter followed by a
    ``## Response`` (or ``# Response``) heading that marks the start of the
    agent's reply.  This function locates that heading and returns everything
    after it (up to *limit* chars).  If no heading is found the entire text
    is returned — callers should be aware that front-matter fields (model,
    timestamp, …) may appear in the snippet.
    """
    lines = text.split("\n")
    response_idx = -1
    for i, line in enumerate(lines):
        if line.startswith("## Response") or line.startswith("# Response"):
            response_idx = i
            break
    body = ("\n".join(lines[response_idx + 1:]) if response_idx >= 0 else "\n".join(lines)).strip()
    return body[:limit] or "(empty)"

def _handle_cron_output(handler, parsed):
    from cron.jobs import OUTPUT_DIR as CRON_OUT
    import re as _re

    qs = parse_qs(parsed.query)
    job_id = qs.get("job_id", [""])[0]
    if not job_id:
        return j(handler, {"error": "job_id required"}, status=400)
    # Match the job_id boundary enforced by the newer cron history/detail
    # handlers.  This endpoint also builds CRON_OUT / job_id before globbing
    # markdown outputs, so reject traversal-shaped IDs before path resolution.
    if not _re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}", job_id):
        return j(handler, {"error": "invalid job_id"}, status=400)
    # Reject malformed limit instead of letting int() raise ValueError and
    # surface as a confusing 500. Clamp to a safe range; a negative value must
    # never reach the slice below — files is sorted newest-first, so a negative
    # limit on `files[:limit]` slices as `files[:-n]` and drops the n OLDEST
    # entries (or all of them when |n| >= len), returning a truncated/empty list
    # instead of the newest outputs. Mirrors _handle_cron_run_detail.
    try:
        limit = max(1, min(500, int(qs.get("limit", ["5"])[0])))
    except (ValueError, TypeError):
        limit = 5
    out_dir = CRON_OUT / job_id
    outputs = []
    if out_dir.exists():
        files = sorted(out_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)[:limit]
        for f in files:
            try:
                txt = f.read_text(encoding="utf-8", errors="replace")
                outputs.append({"filename": f.name, "content": _cron_output_content_window(txt)})
            except Exception:
                logger.debug("Failed to read cron output file %s", f)
    return j(handler, {"job_id": job_id, "outputs": outputs})

def _handle_cron_status(handler, parsed):
    """Return running status for one or all cron jobs."""
    qs = parse_qs(parsed.query)
    job_id = qs.get("job_id", [""])[0]
    if job_id:
        running, elapsed = _is_cron_running(job_id)
        return j(handler, {"job_id": job_id, "running": running, "elapsed": round(elapsed, 1)})
    # Return status for all running jobs
    with _RUNNING_CRON_LOCK:
        all_running = {jid: round(time.time() - t, 1) for jid, t in _RUNNING_CRON_JOBS.items()}
    return j(handler, {"running": all_running})

def _handle_cron_recent(handler, parsed):
    """Return cron jobs that have completed since a given timestamp."""
    import datetime

    qs = parse_qs(parsed.query)
    # Reject a malformed `since` instead of letting float() raise ValueError and
    # surface as a confusing 500. A bad/absent value means "from the epoch", so
    # the client still gets a well-formed (if unfiltered) response.
    try:
        since = float(qs.get("since", ["0"])[0])
    except (ValueError, TypeError):
        since = 0.0
    try:
        from cron.jobs import list_jobs

        jobs = list_jobs(include_disabled=True)
        completions = []
        for job in jobs:
            job_id = str(job.get("id", "") or "")
            last_run = job.get("last_run_at")
            if not last_run:
                continue
            if isinstance(last_run, str):
                try:
                    ts = datetime.datetime.fromisoformat(
                        last_run.replace("Z", "+00:00")
                    ).timestamp()
                except (ValueError, TypeError):
                    continue
            else:
                ts = float(last_run)
            if ts > since:
                completions.append(
                    {
                        "job_id": job_id,
                        "name": job.get("name", "Unknown"),
                        "status": job.get("last_status", "unknown"),
                        "completed_at": ts,
                        "toast_notifications": job.get("toast_notifications") is not False,
                    }
                )
        latest_session_info = _latest_cron_session_info_for_jobs(
            [job.get("id", "") for job in jobs],
            [c["job_id"] for c in completions],
        )
        for completion in completions:
            info = latest_session_info.get(str(completion.get("job_id", "") or ""), {})
            completion["session_id"] = str(info.get("session_id", "") or "")
            if info.get("message_count") is not None:
                completion["message_count"] = int(info["message_count"])
        return j(handler, {"completions": completions, "since": since})
    except ImportError:
        return j(handler, {"completions": [], "since": since})

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
        with _CRON_CREATE_SNAPSHOT_LOCK:
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

def _handle_cron_create(handler, body):
    try:
        require(body, "prompt", "schedule")
    except ValueError as e:
        return bad(handler, str(e))
    try:
        from cron.jobs import create_job, update_job

        profile = _normalize_cron_profile_value(body.get("profile"))
        toast_notifications = body.get("toast_notifications") is not False
        requested_model = body.get("model") or None
        requested_provider = body.get("provider") or None
        job = create_job(
            prompt=body["prompt"],
            schedule=body["schedule"],
            name=body.get("name") or None,
            deliver=body.get("deliver") or "local",
            skills=body.get("skills") or [],
            model=requested_model,
            provider=requested_provider,
        )
        post_create_updates = {}
        if profile is not None:
            post_create_updates["profile"] = profile
            post_create_updates.update(
                _selected_profile_snapshot_updates(
                    profile,
                    provider=requested_provider,
                    model=requested_model,
                )
            )
        if not toast_notifications:
            post_create_updates["toast_notifications"] = False
        if post_create_updates:
            job = update_job(job["id"], post_create_updates) or job
        return j(handler, {"ok": True, "job": _cron_job_for_api(job)})
    except Exception as e:
        return j(handler, {"error": str(e)}, status=400)

def _handle_cron_delivery_options(handler):
    """Return available delivery platforms for cron jobs."""
    try:
        from cron.scheduler import _KNOWN_DELIVERY_PLATFORMS
    except Exception:
        _KNOWN_DELIVERY_PLATFORMS = frozenset()
    platforms = [
        {"value": "local", "label": "Local (save output only)"},
        {"value": "origin", "label": "Origin (reply to creator)"}
    ]
    for name in sorted(_KNOWN_DELIVERY_PLATFORMS):
        platforms.append({"value": name, "label": name.capitalize()})
    return j(handler, {"platforms": platforms})

def _handle_cron_update(handler, body):
    try:
        require(body, "job_id")
    except ValueError as e:
        return bad(handler, str(e))
    from cron.jobs import update_job

    try:
        updates = {}
        for k, v in body.items():
            if k == "job_id":
                continue
            if k == "profile":
                updates[k] = _normalize_cron_profile_value(v)
            elif k in ("model", "provider"):
                updates[k] = v if v else None
            elif v is not None:
                updates[k] = v
    except ValueError as e:
        return bad(handler, str(e))
    job = update_job(body["job_id"], updates)
    if not job:
        return bad(handler, "Job not found", 404)
    return j(handler, {"ok": True, "job": _cron_job_for_api(job)})

def _handle_cron_delete(handler, body):
    try:
        require(body, "job_id")
    except ValueError as e:
        return bad(handler, str(e))
    from cron.jobs import remove_job

    ok = remove_job(body["job_id"])
    if not ok:
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
    # Prevent double-run: reject if the job is already tracked as running
    already_running, elapsed = _is_cron_running(job_id)
    if already_running:
        return j(handler, {"ok": False, "job_id": job_id, "status": "already_running",
                            "elapsed": round(elapsed, 1)})
    _mark_cron_running(job_id)
    # Capture the TLS-active profile home now — the thread runs after the
    # request finishes, so TLS is gone by then.
    #
    # Resolve directly without a try/except: get_active_hermes_home() does
    # in-memory dict reads + a single Path.is_dir() stat, so the only way
    # it could raise from inside a request handler is if api.profiles
    # itself partially failed to import (in which case we'd already be
    # 500-ing the whole request). A silent fallback to None here would
    # re-introduce the exact bug #1573 fixes — the worker thread would
    # run unpinned against the process-global HERMES_HOME — so we'd
    # rather let any unexpected exception 500 the request than corrupt
    # cross-profile state.
    from api.profiles import get_active_hermes_home

    _profile_home = get_active_hermes_home()
    _execution_profile_home = _profile_home_for_cron_job(job)
    _event_profile = _event_profile_for_cron_job(job)
    threading.Thread(target=_run_cron_tracked, args=(job, _profile_home, _execution_profile_home, _event_profile), daemon=True).start()
    return j(handler, {"ok": True, "job_id": job_id, "status": "running"})

def _handle_cron_pause(handler, body):
    job_id = body.get("job_id", "")
    if not job_id:
        return bad(handler, "job_id required")
    from cron.jobs import pause_job

    result = pause_job(job_id, reason=body.get("reason"))
    if result:
        return j(handler, {"ok": True, "job": result})
    return bad(handler, "Job not found", 404)

def _handle_cron_resume(handler, body):
    job_id = body.get("job_id", "")
    if not job_id:
        return bad(handler, "job_id required")
    from cron.jobs import resume_job

    result = resume_job(job_id)
    if result:
        return j(handler, {"ok": True, "job": result})
    return bad(handler, "Job not found", 404)

__routes_exports__ = ('_RUNNING_CRON_JOBS', '_RUNNING_CRON_LOCK', '_CRON_CREATE_SNAPSHOT_LOCK', '_CRON_OUTPUT_CONTENT_LIMIT', '_CRON_OUTPUT_HEADER_CONTEXT', '_normalize_cron_job_ids', '_latest_cron_session_info_for_jobs', '_mark_cron_running', '_mark_cron_done', '_is_cron_running', '_cron_response_marker_index', '_cron_output_content_window', '_cron_job_for_api', '_cron_jobs_for_api', '_AGENT_CRON_IMPORT_PATH_LOCK', '_AGENT_CRON_IMPORT_PATH_READY', '_ensure_agent_cron_import_path', '_cron_jobs_cross_profile', '_available_cron_profile_names', '_normalize_cron_profile_value', '_profile_home_for_cron_job', '_event_profile_for_cron_job', '_cron_job_subprocess_main', '_cron_subprocess_result_timeout_seconds', '_run_cron_job_in_profile_subprocess', '_run_cron_tracked', '_handle_cron_history', '_handle_cron_run_detail', '_cron_output_usage_metadata', '_cron_output_snippet', '_handle_cron_output', '_handle_cron_status', '_handle_cron_recent', '_selected_profile_snapshot_updates', '_handle_cron_create', '_handle_cron_delivery_options', '_handle_cron_update', '_handle_cron_delete', '_handle_cron_run', '_handle_cron_pause', '_handle_cron_resume')
