"""Background-process coordination interface.

Callers use this package interface for lifecycle control, process/session
registration, deferred wakeups, and session-channel access. Internal modules
use direct imports; the underscored aliases below exist only for historical
integrations and tests and are not an internal dispatch mechanism.
"""

from __future__ import annotations

import threading  # noqa: F401 - historical compatibility export
import time  # noqa: F401 - historical compatibility export
import uuid  # noqa: F401 - historical compatibility export
from typing import Any  # Historical compatibility export.

from api.session_channel import (
    SESSION_CHANNELS,
    SESSION_CHANNELS_LOCK,
    SessionChannel,
    active_stream_id_for_session,
    collect_expired_session_channels,
    get_or_create_session_channel,
    get_session_channel,
    persisted_message_count_for_session,
    should_emit_session_updated,
    subscribe_to_session_channel,
)

from . import completion_events as _completion_events
from . import deferred_wakeups as _deferred_wakeups
from . import lifecycle as _lifecycle
from . import process_coordination as _process_coordination
from .completion_events import format_wakeup_prompt
from .deferred_wakeups import (
    claim_deferred_wakeups,
    drain_for_session as drain_deferred_wakeups_for_session,
    record_deferred_wakeup,
)
from .lifecycle import (
    start_drain_thread,
    start_session_channel_reaper,
    stop_drain_thread,
    stop_session_channel_reaper,
)
from .process_coordination import (
    forget_bg_task_completion_dedup,
    recover_processes_for_webui,
    register_process_session,
    unregister_process_session,
)


__all__ = [
    "SESSION_CHANNELS",
    "SESSION_CHANNELS_LOCK",
    "SessionChannel",
    "active_stream_id_for_session",
    "claim_deferred_wakeups",
    "collect_expired_session_channels",
    "drain_deferred_wakeups_for_session",
    "forget_bg_task_completion_dedup",
    "format_wakeup_prompt",
    "get_or_create_session_channel",
    "get_session_channel",
    "persisted_message_count_for_session",
    "record_deferred_wakeup",
    "recover_processes_for_webui",
    "register_process_session",
    "should_emit_session_updated",
    "start_drain_thread",
    "start_session_channel_reaper",
    "stop_drain_thread",
    "stop_session_channel_reaper",
    "subscribe_to_session_channel",
    "unregister_process_session",
]


# Completion-event compatibility aliases. Their identity exposes the real
# owner for existing observers without causing owner code to consult this file.
_EMIT_COALESCE_WINDOW_SECS = _completion_events.EMIT_COALESCE_WINDOW_SECS
_EMIT_COALESCE_LOCK = _completion_events.EMIT_COALESCE_LOCK
_LAST_EMIT_TS = _completion_events.LAST_EMIT_TS
_PENDING_EMIT_PAYLOADS = _completion_events.PENDING_EMIT_PAYLOADS
_PENDING_EMIT_TIMERS = _completion_events.PENDING_EMIT_TIMERS
_REGISTRY_CONSUMED_CONTRACT = _completion_events.REGISTRY_CONSUMED_CONTRACT
_truncate = _completion_events.truncate
_build_payload = _completion_events.build_payload
_emit_to_session_streams = _completion_events.emit_to_session_streams
_emit_bg_task_complete_events_now = _completion_events.emit_now
_flush_coalesced_bg_task_complete = _completion_events.flush_coalesced
_emit_bg_task_complete_events_coalesced = _completion_events.emit_coalesced
_mark_registry_completion_consumed = (
    _completion_events.mark_registry_completion_consumed
)

# Deferred-wakeup compatibility aliases.
_session_has_active_turn = _deferred_wakeups.session_has_active_turn
_start_server_side_wakeup_turn = _deferred_wakeups.start_server_side_turn

# Completion coordination compatibility aliases.
ASYNC_DELIVERY_ROUTING_RETRY_SECONDS = (
    _process_coordination.ASYNC_DELIVERY_ROUTING_RETRY_SECONDS
)
completion_delivery_id = _process_coordination.completion_delivery_id
_env_immune_spawn_owner = _process_coordination._env_immune_spawn_owner
_resolve_wakeup_target = _process_coordination._resolve_wakeup_target
_resolve_completion_target = _process_coordination._resolve_completion_target
_record_async_delegation_accepted = (
    _process_coordination._record_async_delegation_accepted
)
_start_async_delegation_wakeup_turn = (
    _process_coordination._start_async_delegation_wakeup_turn
)
_process_async_delegation_event = (
    _process_coordination._process_async_delegation_event
)
_retry_unmapped_async_delegation_event = (
    _process_coordination._retry_unmapped_async_delegation_event
)


def _process_one(evt: dict) -> None:
    """Compatibility entrypoint; the lifecycle calls its owner directly."""
    _process_coordination.process_one(evt)


def _requeue_async_delegation_event(
    process_registry,
    evt: dict,
    *,
    claim=None,
    delay: float = 0.5,
) -> bool:
    """Compatibility entrypoint preserving drain-stop-aware retry behavior."""
    return _process_coordination._requeue_async_delegation_event(
        process_registry,
        evt,
        claim=claim,
        delay=delay,
        stop_event=_lifecycle._DRAIN_STOP,
    )


# Mutable event/lock objects retain identity across the compatibility seam.
_DRAIN_STOP = _lifecycle._DRAIN_STOP
_REAPER_STOP = _lifecycle._REAPER_STOP
_THREAD_LIFECYCLE_LOCK = _lifecycle._THREAD_LIFECYCLE_LOCK
_drain_loop = _lifecycle._drain_loop
_reaper_loop = _lifecycle._reaper_loop
_PROCESS_RECOVERY_LOCK = _process_coordination._PROCESS_RECOVERY_LOCK


def __getattr__(name: str) -> Any:
    """Read legacy scalar state from its canonical owner.

    Assignment is intentionally not proxied: tests and adapters that replace a
    lifecycle collaborator patch the owner module explicitly, while historical
    read-only introspection continues to report the live value.
    """
    lifecycle_scalars = {
        "_DRAIN_THREAD",
        "_REAPER_THREAD",
        "_REAPER_INTERVAL_SECS",
    }
    if name in lifecycle_scalars:
        return getattr(_lifecycle, name)
    coordination_scalars = {
        "_PROCESS_CHECKPOINT_RECOVERED",
        "_PROCESS_RECOVERY_DONE",
    }
    if name in coordination_scalars:
        return getattr(_process_coordination, name)
    raise AttributeError(name)
