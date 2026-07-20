"""Streaming writeback diagnostics and profile-scoped cron adaptation."""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import threading
import time
from pathlib import Path


logger = logging.getLogger(__name__)
_ENV_LOCK = threading.Lock()
_STREAM_WRITEBACK_DIAG_DEFAULT_THRESHOLD_MS = 250.0
_STREAMING_CRON_PROFILE_HOME: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "webui_streaming_cron_profile_home",
    default=None,
)
_STREAMING_CRONJOB_WRAPPER_INSTALLED = False


def _stream_writeback_diag_threshold_seconds(environ=None):
    if environ is None:
        environ = os.environ
    raw = str(
        environ.get(
            "HERMES_WEBUI_STREAM_WRITEBACK_DIAG_MS",
            _STREAM_WRITEBACK_DIAG_DEFAULT_THRESHOLD_MS,
        )
    ).strip()
    try:
        threshold_ms = float(raw)
    except (TypeError, ValueError):
        threshold_ms = _STREAM_WRITEBACK_DIAG_DEFAULT_THRESHOLD_MS
    if threshold_ms < 0:
        return None
    return threshold_ms / 1000.0

@contextlib.contextmanager
def _stream_writeback_stage(timings, name, *, clock=time.perf_counter):
    started = clock()
    try:
        yield
    finally:
        try:
            timings.append((str(name), max(0.0, float(clock() - started))))
        except Exception:
            pass

def _log_stream_writeback_timings(
    session_id,
    stream_id,
    timings,
    started,
    *,
    clock=time.perf_counter,
    log=logger,
    environ=None,
):
    threshold = _stream_writeback_diag_threshold_seconds(environ=environ)
    if threshold is None:
        return False
    try:
        total_seconds = max(0.0, float(clock() - started))
    except Exception:
        return False
    if total_seconds < threshold:
        return False
    parts = []
    for name, elapsed in timings or []:
        try:
            parts.append(f"{name}={float(elapsed) * 1000.0:.1f}ms")
        except Exception:
            continue
    log.debug(
        "stream final writeback timing session=%s stream=%s total=%.1fms stages=%s",
        session_id,
        stream_id,
        total_seconds * 1000.0,
        " ".join(parts),
    )
    return True

def _install_streaming_cronjob_profile_wrapper() -> None:
    """Wrap the agent cronjob tool so calls run under the streaming profile.

    The in-chat agent run already binds per-turn contextvars for other
    session-scoped state. Cron jobs are special because ``cron.jobs`` snapshots
    path constants at import time, so the model-facing ``cronjob`` tool must
    enter the existing WebUI cron profile context at the tool-call boundary.
    That context uses the cron-specific lock and restores the module caches as
    soon as the single cron tool call returns, avoiding long-lived global path
    mutation for the whole agent turn.
    """
    global _STREAMING_CRONJOB_WRAPPER_INSTALLED
    if _STREAMING_CRONJOB_WRAPPER_INSTALLED:
        return
    try:
        from tools.registry import registry
    except Exception:
        logger.debug("streaming cronjob wrapper: tools registry unavailable", exc_info=True)
        return

    entry = registry.get_entry("cronjob")
    if entry is None:
        try:
            import tools.cronjob_tools  # noqa: F401
        except Exception:
            logger.debug("streaming cronjob wrapper: cronjob tool import failed", exc_info=True)
        entry = registry.get_entry("cronjob")
    if entry is None:
        logger.debug("streaming cronjob wrapper: cronjob tool not registered")
        return
    original_handler = entry.handler
    if getattr(original_handler, "_webui_streaming_profile_wrapper", False):
        _STREAMING_CRONJOB_WRAPPER_INSTALLED = True
        return

    # This relies on the agent tool executor's ``propagate_context_to_thread``
    # using an unfiltered ``contextvars.copy_context()`` so this WebUI-owned
    # contextvar reaches the sync cronjob handler even when the tool call runs
    # on the agent's ThreadPoolExecutor worker.
    def _profile_scoped_cronjob_handler(args, **kwargs):
        profile_home = _STREAMING_CRON_PROFILE_HOME.get()
        if not profile_home:
            return original_handler(args, **kwargs)
        from api.profiles import cron_profile_context_for_home
        with cron_profile_context_for_home(Path(profile_home)):
            return original_handler(args, **kwargs)

    _profile_scoped_cronjob_handler.__dict__["_webui_streaming_profile_wrapper"] = True
    _profile_scoped_cronjob_handler.__dict__["_webui_original_handler"] = original_handler
    registry.register(
        name=entry.name,
        toolset=entry.toolset,
        schema=entry.schema,
        handler=_profile_scoped_cronjob_handler,
        check_fn=entry.check_fn,
        requires_env=entry.requires_env,
        is_async=entry.is_async,
        description=entry.description,
        emoji=entry.emoji,
        max_result_size_chars=entry.max_result_size_chars,
        dynamic_schema_overrides=entry.dynamic_schema_overrides,
    )
    _STREAMING_CRONJOB_WRAPPER_INSTALLED = True
