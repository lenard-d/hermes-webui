"""Route background-process completions to their owning WebUI sessions.

The hermes-agent ``tools.process_registry.ProcessRegistry`` exposes a thread-safe
``completion_queue`` (a ``queue.Queue``) that any background process pushes onto
when it exits or matches a ``watch_patterns`` rule. In the CLI and in the
gateway adapter this queue is drained by the host's main loop; in WebUI the
queue was never read, so the agent never woke up from a ``notify_on_complete``
finish. This module restores that behavior.

The coordinator:
    1. looks up the WebUI session_id from ``PROCESS_SESSION_INDEX`` (keyed on
       the per-process ``session_key`` env var captured at spawn time),
    2. formats a synthetic ``[IMPORTANT: ...]`` wakeup prompt, identical in
       intent to ``cli._format_process_notification`` and
       ``gateway.run._format_gateway_process_notification`` so the agent sees
       the same payload regardless of host,
    3. emits a canonical ``bg_task_complete`` SSE event (plus a temporary
       ``process_complete`` alias for the migration window) on the active
       stream(s) for that session (DEMOTED to pure live-view — an open tab
       streams the turn live), records a server-side marker in
       ``PENDING_BG_TASK_COMPLETIONS``,
       and — Option Z PIVOT — starts the agent wakeup turn **directly
       server-side** when the session is idle (``_start_server_side_wakeup_turn``
       → ``routes.start_session_turn``). This needs NO browser round-trip, so
       the closed-tab case works exactly like CLI / Telegram / gateway
       self-wake. When a turn is already active the wakeup is NOT started here;
       the ``PENDING_BG_TASK_COMPLETIONS`` marker is left for PR #2279's
       next-turn drain (``api/streaming._drain_webui_process_notifications``).

The marker is *not* required for delivery — it's a telemetry-style flag the
turn handler can read to know "this stream is a process_complete wakeup, not a
human-typed prompt". It also lets the PR #2279 next-turn drain deliver the
wakeup when a turn was active at completion time; the marker drains harmlessly
on the next turn for the session.

Watch-pattern events share the same queue but produce a different SSE payload.
Thread start/stop and queue draining live in :mod:`.lifecycle`; this module is
deliberately synchronous apart from the continuation turns it commissions.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from .completion_events import (
    build_payload,
    emit_coalesced,
    format_wakeup_prompt,
    mark_registry_completion_consumed,
)
from .deferred_wakeups import (
    record_deferred_wakeup,
    session_has_active_turn,
    start_server_side_turn,
)
from api.process_event_utils import (
    ASYNC_DELIVERY_ROUTING_RETRY_SECONDS,
    claim_async_delegation_delivery,
    complete_async_delegation_delivery,
    completion_delivery_id,
    release_async_delegation_delivery,
    requeue_async_delegation_event,
    schedule_async_delegation_claim_retry,
)
from api.session_state import (
    BG_TASK_COMPLETE_EVENTS_SEEN,
    BG_TASK_COMPLETE_EVENTS_SEEN_LOCK,
    PENDING_BG_TASK_COMPLETIONS,
    PROCESS_SESSION_INDEX,
    PROCESS_SESSION_INDEX_LOCK,
)

logger = logging.getLogger(__name__)
_PROCESS_RECOVERY_DONE = False
_PROCESS_CHECKPOINT_RECOVERED = False
_PROCESS_RECOVERY_LOCK = threading.Lock()


# ── xsession wakeup misroute defense-in-depth (Option 3) ───────────────────
# Option 1 (api/streaming._set_turn_session_identity) is the ROOT fix: it binds
# the per-turn session identity to a contextvar so a notify_on_complete spawn
# can no longer capture a concurrent turn's process-global env. Option 3 is an
# INDEPENDENT completion-time safety net at the wakeup-routing layer: even if
# some future regression reintroduces a capture race, a positively-detected
# mismatch must not wake the wrong session.
#
# The proc->owner link the WebUI drain trusts is ProcessSession.session_key,
# which the terminal tool captured from the (historically racy) env at spawn.
# An env-IMMUNE spawn-time owner would be the authoritative cross-check, but
# adding such a field to the core ProcessSession is out of scope for this
# WebUI-only change. So this resolver is forward-compatible by DUCK-TYPING:
# if a future core grows an env-immune spawn-owner attribute (any of the
# names below) AND it positively disagrees with the session_key-resolved
# target, re-route to the env-immune owner and log ERROR. When the owner is
# absent/empty/unknown (today's core, cron/CLI processes sharing the
# registry, pre-Option-1 spawns) it is a PURE PASS-THROUGH — it never
# suppresses a legitimate Option Z wakeup on uncertainty (Option Z must keep
# working).
_ENV_IMMUNE_OWNER_ATTRS = (
    "origin_ui_session_id",  # modern hermes-agent exact browser-tab return address
    "spawn_session_id",
    "owner_session_id",
    "turn_session_id",
)


def _env_immune_spawn_owner(proc_session) -> str:
    """Return the env-immune spawn-time owner sid from the ProcessSession, or
    "" when none is available (the only contract today; forward-compatible)."""
    if proc_session is None:
        return ""
    for attr in _ENV_IMMUNE_OWNER_ATTRS:
        try:
            val = getattr(proc_session, attr, "")
        except Exception:
            val = ""
        if val:
            return str(val)
    return ""


def _resolve_wakeup_target(
    *,
    process_id: str,
    session_key_resolved_sid: str,
    proc_session,
) -> str:
    """Cross-check the session_key-resolved wakeup target against the
    env-immune spawn owner. Returns the sid the server-side wakeup turn should
    actually target.

    - Owner unknown/empty  -> pass-through (return session_key_resolved_sid).
    - Owner == resolved    -> pass-through (the normal + post-Option-1 case).
    - Owner != resolved    -> POSITIVE mismatch: log ERROR and RE-ROUTE to the
      env-immune owner (do NOT wake the wrong session — this is the exact
      agent.log:6632 cross-session misroute).
    """
    resolved = str(session_key_resolved_sid or "")
    owner = _env_immune_spawn_owner(proc_session)
    if not owner or owner == resolved:
        return resolved
    logger.error(
        "xsession wakeup misroute BLOCKED (Option 3 safety net): process %r "
        "session_key resolved to session %r but the env-immune spawn owner "
        "is %r — re-routing the server-side wakeup to the true owner. This "
        "means a per-turn session-identity capture race occurred upstream "
        "(Option 1 should have prevented it); investigate streaming.py "
        "_set_turn_session_identity coverage.",
        process_id, resolved, owner,
    )
    return owner


def _requeue_async_delegation_event(
    process_registry,
    evt: dict,
    *,
    claim=None,
    delay: float = 0.5,
    stop_event: threading.Event | None = None,
) -> bool:
    """Retry without enqueueing new work once drain shutdown has started."""
    completion_queue = getattr(process_registry, "completion_queue", None)
    return requeue_async_delegation_event(
        evt,
        completion_queue,
        delay=delay,
        stop_event=stop_event,
        durable=(bool(getattr(claim, "durable", False)) if claim is not None else None),
    )


def _retry_unmapped_async_delegation_event(
    process_registry,
    evt: dict,
    *,
    stop_event: threading.Event | None = None,
) -> None:
    """Retry durable routing, or one bounded best-effort legacy routing pass."""
    completion_queue = getattr(process_registry, "completion_queue", None)
    if schedule_async_delegation_claim_retry(
        evt,
        completion_queue,
        delay=ASYNC_DELIVERY_ROUTING_RETRY_SECONDS,
    ):
        return
    if evt.get("_webui_routing_retry_attempted"):
        return
    retry_evt = dict(evt)
    retry_evt["_webui_routing_retry_attempted"] = True
    _requeue_async_delegation_event(
        process_registry,
        retry_evt,
        delay=ASYNC_DELIVERY_ROUTING_RETRY_SECONDS,
        stop_event=stop_event,
    )


def _record_async_delegation_accepted(
    evt: dict,
    *,
    session_id: str,
    claim,
) -> None:
    """ACK durable delivery and publish live-view state after turn acceptance."""

    complete_async_delegation_delivery(evt, claim)
    payload = build_payload(evt, session_id)
    try:
        emit_coalesced(session_id, payload)
    except Exception:
        logger.debug(
            "async delegation live-view emit failed for session %s",
            session_id,
            exc_info=True,
        )


def _start_async_delegation_wakeup_turn(
    session_id: str,
    wakeup_prompt: str,
    *,
    delegation_id: str,
    evt: dict,
    claim,
    process_registry,
    stop_event: threading.Event | None = None,
) -> None:
    """Start one autonomous delegation turn and ACK only after acceptance."""

    def _runner() -> None:
        try:
            from api.runs import start_session_turn

            resp = start_session_turn(
                session_id,
                wakeup_prompt,
                source="process_wakeup",
            )
            raw_status = (resp or {}).get("_status")
            if raw_status is None:
                status = 200 if (resp or {}).get("stream_id") else 500
            else:
                status = int(raw_status)
            if 200 <= status < 300:
                _record_async_delegation_accepted(
                    evt,
                    session_id=session_id,
                    claim=claim,
                )
                logger.info(
                    "async delegation wakeup turn accepted for session %s "
                    "(stream_id=%s)",
                    session_id,
                    (resp or {}).get("stream_id"),
                )
                return

            release_async_delegation_delivery(evt, claim)
            _requeue_async_delegation_event(
                process_registry,
                evt,
                claim=claim,
                stop_event=stop_event,
            )
            if status == 409 and (resp or {}).get("error") == "process_wakeup_paused":
                logger.info(
                    "async delegation wakeup paused for session %s; delivery remains retryable",
                    session_id,
                )
            else:
                logger.debug(
                    "async delegation wakeup not accepted for session %s: "
                    "status=%s err=%r; requeued",
                    session_id,
                    status,
                    (resp or {}).get("error"),
                )
        except Exception:
            release_async_delegation_delivery(evt, claim)
            _requeue_async_delegation_event(
                process_registry,
                evt,
                claim=claim,
                stop_event=stop_event,
            )
            logger.warning(
                "async delegation wakeup turn failed for session %s; requeued",
                session_id,
                exc_info=True,
            )

    threading.Thread(
        target=_runner,
        name=f"hermes-webui-delegation-wakeup-{str(session_id)[:8]}",
        daemon=True,
    ).start()


def _process_async_delegation_event(
    evt: dict,
    *,
    session_id: str,
    delegation_id: str,
    process_registry,
    stop_event: threading.Event | None = None,
) -> None:
    """Claim and route one async completion without private registry markers."""

    try:
        claim = claim_async_delegation_delivery(evt, "webui-background")
    except Exception:
        _requeue_async_delegation_event(
            process_registry,
            evt,
            stop_event=stop_event,
        )
        return
    if claim is None:
        completion_queue = getattr(process_registry, "completion_queue", None)
        schedule_async_delegation_claim_retry(evt, completion_queue)
        return

    try:
        wakeup_prompt_raw = format_wakeup_prompt(evt)
        wakeup_prompt = wakeup_prompt_raw.strip() if wakeup_prompt_raw else ""
        if not wakeup_prompt:
            raise RuntimeError("async delegation completion could not be formatted")

        # Do not persist async results in the process-local deferred list. If a
        # foreground turn owns the session, release the durable claim and retry
        # from the shared queue; the core record therefore remains restart-safe.
        if session_has_active_turn(session_id):
            release_async_delegation_delivery(evt, claim)
            _requeue_async_delegation_event(
                process_registry,
                evt,
                claim=claim,
                stop_event=stop_event,
            )
            return

        launch_kwargs: dict[str, Any] = {
            "delegation_id": delegation_id,
            "evt": evt,
            "claim": claim,
            "process_registry": process_registry,
        }
        if stop_event is not None:
            launch_kwargs["stop_event"] = stop_event
        _start_async_delegation_wakeup_turn(
            session_id,
            wakeup_prompt,
            **launch_kwargs,
        )
    except Exception:
        release_async_delegation_delivery(evt, claim)
        _requeue_async_delegation_event(
            process_registry,
            evt,
            claim=claim,
            stop_event=stop_event,
        )
        logger.warning(
            "server-side async delegation dispatch failed for session %s",
            session_id,
            exc_info=True,
        )


def _resolve_completion_target(
    *,
    session_key_resolved_sid: str,
    origin_ui_session_id: str,
) -> str:
    """Return the WebUI session that owns a detached completion event.

    Modern Hermes Agent events carry ``origin_ui_session_id`` as an exact,
    immutable return address captured from the commissioning browser turn.
    It is authoritative over the mutable/legacy session-key index. Older
    Agent events omit it and retain the existing session-key fallback.
    """
    resolved = str(session_key_resolved_sid or "")
    owner = str(origin_ui_session_id or "")
    if not owner:
        return resolved
    if resolved and resolved != owner:
        logger.error(
            "cross-session completion route BLOCKED: session_key resolved to %r "
            "but exact origin_ui_session_id is %r; routing to the exact owner",
            resolved,
            owner,
        )
    return owner


def process_one(
    evt: dict,
    *,
    stop_event: threading.Event | None = None,
) -> None:
    """Route a single completion_queue event to the matching WebUI session."""
    # Hoist the process-registry import once per event: it was imported in
    # three separate blocks below (session_key recovery, env-immune owner
    # cross-check, upstream is_completion_consumed dedupe) on every completion
    # event, paying repeated import-system overhead. Single local rebind keeps
    # the ImportError fallback contract (process_registry may be missing in
    # cut-down vendoring) while collapsing to one lookup per call.
    try:
        from tools.process_registry import process_registry as _process_registry
    except Exception:
        _process_registry = None

    process_id = completion_delivery_id(evt)
    session_key = str(evt.get("session_key") or "")
    origin_ui_session_id = str(evt.get("origin_ui_session_id") or "")
    # Root-cause fix (t_0f447014): the notify_on_complete completion event
    # enqueued by ProcessRegistry._move_to_finished() historically carried NO
    # "session_key" field — only the watch_match enqueue included one. Without
    # it the old `evt.get("session_key") or process_id` fell back to the
    # process id ("proc_xxxx"), which is never a PROCESS_SESSION_INDEX key.
    # Recover spawn-time routing metadata from the process registry.
    if process_id and (not session_key or not origin_ui_session_id):
        try:
            if _process_registry is not None:
                _ps = _process_registry.get(process_id)
                if _ps is not None:
                    if not session_key and getattr(_ps, "session_key", ""):
                        session_key = str(_ps.session_key)
                    if not origin_ui_session_id:
                        origin_ui_session_id = (
                            str(getattr(_ps, "origin_ui_session_id", "") or "")
                            or str(getattr(_ps, "spawn_session_id", "") or "")
                        )
        except Exception:
            logger.debug(
                "session ownership recovery from process registry failed for %r",
                process_id,
                exc_info=True,
            )
    if not session_key and not origin_ui_session_id:
        logger.debug(
            "process_complete drop: no recoverable session_key or exact UI owner "
            "for process_id=%r",
            process_id,
        )
        if evt.get("type") == "async_delegation":
            _retry_unmapped_async_delegation_event(
                _process_registry,
                evt,
                stop_event=stop_event,
            )
        return
    session_id = ""
    if session_key:
        with PROCESS_SESSION_INDEX_LOCK:
            session_id = PROCESS_SESSION_INDEX.get(session_key) or ""
    if not session_id and not origin_ui_session_id:
        # No mapping — could be a cron/gateway process that uses the same
        # registry but a non-WebUI session_key. Durable delegation events stay
        # pending and are retried because their WebUI ownership mapping can be
        # registered shortly after process restore.
        logger.debug("process_complete drop: no session mapping for key=%r", session_key)
        if evt.get("type") == "async_delegation":
            _retry_unmapped_async_delegation_event(
                _process_registry,
                evt,
                stop_event=stop_event,
            )
        return
    # ── xsession wakeup misroute defense-in-depth (Option 3) ──────────────
    # First retain the process-registry spawn-owner cross-check for legacy
    # terminal events. Then apply origin_ui_session_id as the final authority;
    # that exact owner is shared by process and async-delegation completions.
    try:
        _ps_xs = _process_registry.get(process_id) if (_process_registry is not None and process_id) else None
    except Exception:
        _ps_xs = None
    session_id = _resolve_wakeup_target(
        process_id=process_id,
        session_key_resolved_sid=session_id,
        proc_session=_ps_xs,
    )
    # origin_ui_session_id is the exact, immutable return address; it is the
    # FINAL routing authority over the (mutable) session-key/Option-3 result.
    session_id = _resolve_completion_target(
        session_key_resolved_sid=session_id,
        origin_ui_session_id=origin_ui_session_id,
    )
    if not session_id:
        logger.debug("process_complete drop: completion target resolved empty")
        # An async delegation event that resolves empty here must NOT silently
        # return: no durable claim was taken, so the core row stays pending and
        # the restart-restore sweep would re-deliver it forever. Route it into
        # the bounded retry instead (arms a durable retry / one best-effort
        # legacy requeue), so it stays retryable without a spurious ACK.
        if evt.get("type") == "async_delegation":
            _retry_unmapped_async_delegation_event(
                _process_registry,
                evt,
                stop_event=stop_event,
            )
        return
    # ── THE SEAM: async delegations take the durable-claim delivery path,
    # routed to the origin-resolved session. The claim/complete/release
    # lifecycle (keyed on the immutable delegation_id) is the sole dedupe +
    # restart-safety authority for async events, so they early-return before
    # the terminal-process idempotency/emit/Option-Z machinery below. ──
    if evt.get("type") == "async_delegation":
        _process_async_delegation_event(
            evt,
            session_id=session_id,
            delegation_id=process_id,
            process_registry=_process_registry,
            stop_event=stop_event,
        )
        return
    # ── Idempotency vs the REAL merged upstream #2279 (shared dedupe key) ──
    # The real merged #2279 next-turn drain
    # (api/streaming._drain_webui_process_notifications) dedupes ONLY via
    # process_registry.is_completion_consumed() / _completion_consumed — it
    # does NOT populate BG_TASK_COMPLETE_EVENTS_SEEN (that set is ours-original
    # and private to this module). So the cross-A/B shared dedupe contract is
    # process_registry._completion_consumed, NOT BG_TASK_COMPLETE_EVENTS_SEEN.
    # If the upstream A-drain already delivered this process_id (A-first
    # order), it marked _completion_consumed; B must early-return here or it
    # would double-fire a wakeup. This guard aligns our B-drain to the real
    # upstream key (verified against origin/master streaming.py).
    if process_id:
        try:
            if _process_registry is not None and _process_registry.is_completion_consumed(process_id):
                return
        except Exception:
            logger.debug(
                "is_completion_consumed check failed on B drain; "
                "falling back to BG_TASK_COMPLETE_EVENTS_SEEN gate",
                exc_info=True,
            )
    # Secondary (ours-original) idempotency: if we've already emitted for this
    # (session_id, process_id) pair via THIS module, skip the duplicate. Two
    # _move_to_finished() callers (kill_process racing the reader thread) can
    # occasionally enqueue twice despite the process_registry guard.
    with BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
        seen = BG_TASK_COMPLETE_EVENTS_SEEN.setdefault(session_id, set())
        if process_id and process_id in seen:
            return
        if process_id:
            seen.add(process_id)
    payload = build_payload(evt, session_id)
    emit_coalesced(session_id, payload)
    PENDING_BG_TASK_COMPLETIONS.add(session_id)
    # Mark the event consumed in the agent's process registry so the REAL
    # merged PR #2279's next-turn drain
    # (api/streaming._drain_webui_process_notifications) treats this process_id
    # as already-delivered and does not re-fire a wakeup (B-first order).
    # This is the SHARED upstream dedupe key (see _mark_registry_completion_
    # consumed for the coupling contract + why a future rename now fails loud).
    if process_id:
        mark_registry_completion_consumed(process_id)

    # ── Option Z (PRIMARY): server-side wakeup, NO browser round-trip ──────
    # The SSE emit above is now demoted to a pure live-view layer (an open tab
    # streams the turn live via the per-session SSE channel). The ACTUAL agent
    # wakeup is started HERE, server-side, so a CLOSED tab still gets the turn
    # — parity with how CLI / Telegram / gateway self-wake from a
    # notify_on_complete completion. This is the fix for the structural flaw:
    # "fire a long background task, close the tab, come back later" is THE
    # primary background-task use case and browser-mediated wakeup could never
    # serve it.
    #
    #   - turn ACTIVE → do NOT start a turn. Leave the PENDING_PROCESS_
    #     COMPLETIONS marker so PR #2279's next-turn drain
    #     (api/streaming._drain_webui_process_notifications) injects the wakeup
    #     when the active turn ends. (That path already works when a turn is
    #     active — it was never the gap.)
    #   - turn IDLE → start a new server-side turn directly with wakeup_prompt
    #     as the user message (the real gap Option Z closes).
    #
    # Idempotency is already guaranteed above: BG_TASK_COMPLETE_EVENTS_SEEN +
    # the registry _completion_consumed marker mean this process_id reached
    # here at most once, so the wakeup turn starts at most once.
    try:
        # ``wakeup_prompt`` is server-internal state used only by the
        # Option Z server-side wakeup; it was previously surfaced on the
        # SSE payload but T1 trimmed the payload to the minimal shape
        # `{session_id, task_id, completed_at, summary?, event_id}`, so
        # we derive the prompt directly from the evt here (same source the
        # prior _build_payload used).
        wakeup_prompt_raw = format_wakeup_prompt(evt)
        wakeup_prompt = wakeup_prompt_raw.strip() if wakeup_prompt_raw else ""
        if wakeup_prompt:
            if session_has_active_turn(session_id):
                # Defer-path fix: persist the prompt so a turn-teardown
                # idle-hook can redeliver it once the session goes idle.
                # The OLD behavior only logged + left a bare
                # PENDING_BG_TASK_COMPLETIONS session flag; the prompt was
                # discarded and the next-turn drain reads completion_queue
                # (already emptied by THIS drain thread), so for an
                # autonomous agent with no next user turn the wakeup was
                # lost forever. process_id is already in
                # BG_TASK_COMPLETE_EVENTS_SEEN + the registry
                # _completion_consumed marker (set above), so persisting it
                # here cannot cause a double-fire — the atomic claim in
                # ``claim_deferred_wakeups`` guarantees exactly one delivery.
                record_deferred_wakeup(session_id, process_id, wakeup_prompt)
                logger.debug(
                    "server-side wakeup deferred: turn active for session %s "
                    "(persisted for turn-teardown idle-hook redelivery)",
                    session_id,
                )
            else:
                # Idle-path sibling of the F1 (409/teardown) fix: pass
                # ``process_id`` so that if this idle wakeup's daemon thread
                # loses the per-session lock race and 409s, the re-defer in
                # ``_start_server_side_wakeup_turn`` records the entry WITH its
                # process_id — keeping the ``record_deferred_wakeup`` dedup
                # guard (``if process_id and any(...)``) live on that re-defer
                # path so a second 409 race cannot accumulate a duplicate
                # deferred entry (which would deliver the same wakeup twice).
                start_server_side_turn(
                    session_id, wakeup_prompt, process_id=process_id
                )
    except Exception:
        logger.warning(
            "server-side wakeup dispatch failed for session %s", session_id, exc_info=True
        )


def recover_processes_for_webui(process_registry=None, get_session_fn=None) -> int:
    """Recover core background processes and restore WebUI routing metadata.

    The core gateway performs this during gateway startup, but this WebUI host
    previously started only the queue drain. That left checkpointed processes
    invisible after a WebUI restart.
    """
    global _PROCESS_CHECKPOINT_RECOVERED, _PROCESS_RECOVERY_DONE
    if process_registry is None:
        try:
            from tools.process_registry import process_registry
        except ImportError:
            # Hermes Agent is optional in isolated WebUI/test environments.
            # The drain loop already treats a missing registry as unavailable;
            # startup recovery must preserve that fail-soft contract.
            logger.debug("process recovery unavailable: Hermes Agent is not installed")
            return 0
    if get_session_fn is None:
        from api.models import get_session as get_session_fn

    with _PROCESS_RECOVERY_LOCK:
        if _PROCESS_RECOVERY_DONE:
            return 0

        recovered = 0
        if not _PROCESS_CHECKPOINT_RECOVERED:
            recovered = process_registry.recover_from_checkpoint()
            _PROCESS_CHECKPOINT_RECOVERED = True

        for row in process_registry.list_sessions():
            process_id = str(row.get("session_id") or "")
            if not process_id:
                continue
            try:
                proc_session = process_registry.get(process_id)
                session_key = str(getattr(proc_session, "session_key", "") or "")
                if not session_key or get_session_fn(session_key, metadata_only=True) is None:
                    continue
            except Exception:
                logger.warning(
                    "Could not resolve recovered WebUI process %r",
                    process_id,
                    exc_info=True,
                )
                continue
            register_process_session(session_key, session_key)
        _PROCESS_RECOVERY_DONE = True
        if recovered:
            logger.info("Recovered %d background process(es) for WebUI", recovered)
        return recovered


def register_process_session(session_key: str, session_id: str) -> None:
    """Bind a process-registry session_key to a WebUI session_id.

    Called at chat-start time, before the agent thread spawns any background
    processes. The same ``session_key`` is exported to the child via
    ``HERMES_SESSION_KEY`` (already done by streaming.py), so when the child
    pushes onto ``completion_queue`` it carries the key we registered.
    """
    if not session_key or not session_id:
        return
    with PROCESS_SESSION_INDEX_LOCK:
        PROCESS_SESSION_INDEX[str(session_key)] = str(session_id)


def unregister_process_session(session_key: str) -> None:
    if not session_key:
        return
    with PROCESS_SESSION_INDEX_LOCK:
        PROCESS_SESSION_INDEX.pop(str(session_key), None)


def forget_bg_task_completion_dedup(session_id: str) -> None:
    """Drop a session's ``BG_TASK_COMPLETE_EVENTS_SEEN`` entry.

    Called on session deletion so a session deleted while a completion is still
    pending (undelivered) — which the reaper's delivery-gated sweep deliberately
    keeps — can't leak its dedup set forever. Safe for unknown ids (no-op).
    """
    if not session_id:
        return
    with BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
        BG_TASK_COMPLETE_EVENTS_SEEN.pop(str(session_id), None)
