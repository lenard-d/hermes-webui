"""Approval, clarify, and persistent session SSE channel ownership.

Each handler owns subscribe-plus-snapshot ordering and releases its subscriber
on every socket/header/write exit.
"""

from __future__ import annotations

import logging
import queue
from urllib.parse import parse_qs

from api.helpers import _CLIENT_DISCONNECT_ERRORS, bad, j
from api.route_approvals import (
    _approval_sse_subscribers,
    _approval_sse_unsubscribe,
    _gateway_queues,
    _lock,
    _pending,
    reconcile_gateway_pending_mirror_locked,
    submit_pending,
)
from api.sessions.store import get_session
from api.sse_chunked import end_sse_headers
from api.streaming.transport import (
    SSE_HEARTBEAT_INTERVAL_SECONDS as _SSE_HEARTBEAT_INTERVAL_SECONDS,
    _sse,
    _sse_set_write_deadline,
)

try:
    from api.clarify import (
        get_pending as get_clarify_pending,
        sse_subscribe as clarify_sse_subscribe,
        sse_unsubscribe as clarify_sse_unsubscribe,
        submit_pending as submit_clarify_pending,
    )
except ImportError:
    get_clarify_pending = lambda *a, **k: None
    clarify_sse_subscribe = None
    clarify_sse_unsubscribe = lambda *a, **k: None
    submit_clarify_pending = lambda *a, **k: None

logger = logging.getLogger(__name__)

def _handle_approval_pending(handler, parsed):
    sid = parse_qs(parsed.query).get("session_id", [""])[0]
    with _lock:
        _head, _total, _changed = reconcile_gateway_pending_mirror_locked(sid)
        queue = _pending.get(sid)
        # Support both the new list format and a legacy single-dict value.
        if isinstance(queue, list):
            p = queue[0] if queue else None
            total = len(queue)
        elif queue:
            p = queue
            total = 1
        else:
            p = None
            total = 0
        if p is None:
            gw_queue = _gateway_queues.get(sid) or []
            if gw_queue:
                raw = getattr(gw_queue[0], "data", None) or {}
                if raw:
                    p = raw
                    total = len(gw_queue)
                else:
                    logger.warning("Gateway queue entry for %s has no .data attribute", sid)
    if p:
        return j(handler, {"pending": dict(p), "pending_count": total})
    return j(handler, {"pending": None, "pending_count": 0})


def _handle_approval_sse_stream(handler, parsed):
    """SSE endpoint for real-time approval notifications.

    Long-lived connection that pushes approval events the moment they arrive,
    replacing the 1.5s polling loop.  The frontend uses EventSource and falls
    back to HTTP polling if the connection fails.
    """
    sid = parse_qs(parsed.query).get("session_id", [""])[0]
    if not sid:
        return bad(handler, "session_id is required")

    # Subscribe AND snapshot atomically under a single _lock acquisition so a
    # submit_pending() that fires between the two cannot be lost. If we
    # snapshot first then subscribe (the naive ordering), an approval that
    # arrives in the gap is appended to _pending (after our snapshot) AND
    # notified to subscribers (before we joined) — leaving the client unaware
    # until the next event arrives.
    q = queue.Queue(maxsize=16)
    initial_pending = None
    initial_count = 0
    with _lock:
        _approval_sse_subscribers.setdefault(sid, []).append(q)
        reconcile_gateway_pending_mirror_locked(sid)
        q_list = _pending.get(sid)
        if isinstance(q_list, list):
            initial_pending = dict(q_list[0]) if q_list else None
            initial_count = len(q_list)
        elif q_list:
            initial_pending = dict(q_list)
            initial_count = 1

    handler.send_response(200)
    handler.send_header('Content-Type', 'text/event-stream; charset=utf-8')
    handler.send_header('Cache-Control', 'no-cache')
    handler.send_header('X-Accel-Buffering', 'no')
    handler.send_header('Connection', 'close')
    end_sse_headers(handler)
    _sse_set_write_deadline(handler)  # Defect A: slow tab can't pin this thread

    from api.streaming import _sse

    # Push initial state immediately so the client doesn't miss anything.
    _sse(handler, 'initial', {"pending": initial_pending, "pending_count": initial_count})

    try:
        while True:
            try:
                payload = q.get(timeout=_SSE_HEARTBEAT_INTERVAL_SECONDS)
            except queue.Empty:
                # Keepalive — SSE comment line prevents proxy/CDN timeout.
                handler.wfile.write(b': keepalive\n\n')
                handler.wfile.flush()
                continue
            if payload is None:
                break  # signal to close
            _sse(handler, 'approval', payload)
    except _CLIENT_DISCONNECT_ERRORS:
        pass  # client went away — normal for long-lived connections
    finally:
        _approval_sse_unsubscribe(sid, q)


def _handle_approval_inject(handler, parsed):
    """Inject a fake pending approval -- loopback-only, used by automated tests."""
    qs = parse_qs(parsed.query)
    sid = qs.get("session_id", [""])[0]
    key = qs.get("pattern_key", ["test_pattern"])[0]
    cmd = qs.get("command", ["rm -rf /tmp/test"])[0]
    if sid:
        submit_pending(
            sid,
            {
                "command": cmd,
                "pattern_key": key,
                "pattern_keys": [key],
                "description": "test pattern",
            },
        )
        return j(handler, {"ok": True, "session_id": sid})
    return j(handler, {"error": "session_id required"}, status=400)


def _handle_clarify_pending(handler, parsed):
    sid = parse_qs(parsed.query).get("session_id", [""])[0]
    pending = get_clarify_pending(sid)
    if pending:
        return j(handler, {"pending": pending})
    return j(handler, {"pending": None})


def _handle_clarify_sse_stream(handler, parsed):
    """SSE endpoint for real-time clarify notifications.

    Long-lived connection that pushes clarify events the moment they arrive,
    replacing the 1.5s polling loop.  The frontend uses EventSource and falls
    back to HTTP polling if the connection fails.
    """
    if clarify_sse_subscribe is None:
        return bad(handler, "clarify SSE not available")

    sid = parse_qs(parsed.query).get("session_id", [""])[0]
    if not sid:
        return bad(handler, "session_id is required")

    # Subscribe AND snapshot atomically.  We import clarify's _lock so that
    # subscribe and the snapshot read happen under the same mutex — same
    # pattern as the approval SSE handler.
    #
    # NOTE: We must NOT call clarify.get_pending() here — it acquires _lock
    # internally, which would deadlock since clarify._lock is a non-reentrant
    # threading.Lock.  Instead, read _gateway_queues / _pending inline under
    # the lock we already hold.
    from api.clarify import (
        _lock as _clarify_lock,
        _clarify_sse_subscribers as _clarify_subs,
        _gateway_queues as _clarify_gateway_queues,
        _pending as _clarify_pending,
    )
    q = queue.Queue(maxsize=16)
    initial_pending = None
    initial_count = 0
    with _clarify_lock:
        _clarify_subs.setdefault(sid, []).append(q)
        gw_q = _clarify_gateway_queues.get(sid) or []
        if gw_q:
            initial_pending = dict(gw_q[0].data)
            initial_count = len(gw_q)
        else:
            _legacy = _clarify_pending.get(sid)
            if _legacy:
                initial_pending = dict(_legacy)
                initial_count = 1

    handler.send_response(200)
    handler.send_header('Content-Type', 'text/event-stream; charset=utf-8')
    handler.send_header('Cache-Control', 'no-cache')
    handler.send_header('X-Accel-Buffering', 'no')
    handler.send_header('Connection', 'close')
    end_sse_headers(handler)
    _sse_set_write_deadline(handler)  # Defect A: slow tab can't pin this thread

    from api.streaming import _sse

    # Push initial state immediately so the client doesn't miss anything.
    _sse(handler, 'initial', {"pending": initial_pending, "pending_count": initial_count})

    try:
        while True:
            try:
                payload = q.get(timeout=_SSE_HEARTBEAT_INTERVAL_SECONDS)
            except queue.Empty:
                handler.wfile.write(b': keepalive\n\n')
                handler.wfile.flush()
                continue
            if payload is None:
                break
            _sse(handler, 'clarify', payload)
    except _CLIENT_DISCONNECT_ERRORS:
        pass
    finally:
        clarify_sse_unsubscribe(sid, q)


def _handle_session_sse_stream(handler, parsed):
    """SSE endpoint for the persistent per-session channel (Option X).

    Subscribes to ``api.background_process.SESSION_CHANNELS[sid]`` — a channel
    that lives across agent turns (unlike STREAMS, which is torn down at
    end-of-turn). Used to deliver ``bg_task_complete`` events that fire while
    no agent turn is active.

    Lifecycle: opened by the frontend at session mount, closed at unmount or
    on tab close. Multiple tabs share one SessionChannel (refcounted via
    subscribe/unsubscribe). 30s SSE keepalive comments keep the proxy alive.
    Reaper-driven idle TTL (default 4h) prevents zombie channels.
    """
    sid = parse_qs(parsed.query).get("session_id", [""])[0]
    if not sid:
        return bad(handler, "session_id is required")

    # The (re)subscribing tab reports its last-known message_count via
    # ?known_count=N so the on-subscribe self-heal can detect a server-initiated
    # turn that started AND finished entirely inside this tab's SSE gap (see the
    # "server-initiated turn finished during the gap" self-heal block below).
    # Absent/blank/non-numeric => None ("tab didn't report", never triggers).
    _known_count_raw = parse_qs(parsed.query).get("known_count", [""])[0]
    try:
        subscriber_known_count = int(_known_count_raw) if _known_count_raw != "" else None
    except (TypeError, ValueError):
        subscriber_known_count = None

    from api.background_process import (
        subscribe_to_session_channel,
        active_stream_id_for_session,
        persisted_message_count_for_session,
        should_emit_session_updated,
    )

    # Atomic get-or-create + subscribe under SESSION_CHANNELS_LOCK. Doing these
    # two steps separately (get_or_create_session_channel then ch.subscribe)
    # left a TOCTOU gap where the reaper — which also holds
    # SESSION_CHANNELS_LOCK and collects idle 0-subscriber channels in one
    # critical section — could collect the channel between the two calls,
    # orphaning this subscriber on a channel no longer in SESSION_CHANNELS.
    # bg_task_complete emits would then never reach this queue. See
    # subscribe_to_session_channel for the full rationale (PR #2971 Greptile P1).
    ch, q = subscribe_to_session_channel(sid, maxsize=64)

    # NOTE: ``subscribe_to_session_channel`` above acquires a subscriber slot
    # that MUST be released on every exit path. Header setup
    # (``send_response`` / ``send_header`` / ``end_headers`` /
    # ``_sse_set_write_deadline``) and the initial-frame + on-subscribe
    # recovery writes below all touch the socket and can raise a member of
    # ``_CLIENT_DISCONNECT_ERRORS`` (BrokenPipeError / ConnectionResetError) if
    # the client drops immediately after subscribing. If that happened outside
    # this try/finally the ``ch.unsubscribe(q)`` cleanup would be skipped,
    # permanently leaking a subscriber. Because
    # ``SessionChannel.reaper_should_collect()`` refuses to collect any channel
    # with ``sub_count > 0``, a single ghost subscriber blocks the reaper
    # forever and the channel zombies in SESSION_CHANNELS. So EVERYTHING from
    # the subscribe onward — header setup included — runs inside one
    # try/finally that unconditionally unsubscribes.
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        handler.send_header('Cache-Control', 'no-cache')
        handler.send_header('X-Accel-Buffering', 'no')
        # #3103: omit the Connection header — rely on the HTTP/1.1 keep-alive
        # default, matching the other long-lived SSE handlers (gateway/session
        # events) that fixed the reconnect-storm. An explicit value here is a
        # third, inconsistent approach (greptile flag).
        end_sse_headers(handler)
        _sse_set_write_deadline(handler)  # Defect A: slow tab can't pin this thread

        from api.streaming import _sse

        # Push an initial frame so the client has confirmation the channel is
        # live (mirrors approval/clarify which send an 'initial' frame). No
        # snapshot data is needed — this channel only carries forward-looking
        # events, not pending state.
        _sse(handler, 'initial', {"session_id": sid})

        # ── Open-tab live-view self-heal (root cause: lost server_turn_started) ──
        # The `server_turn_started` fan-out (routes.start_session_turn) is a
        # fire-and-forget SessionChannel.emit with NO replay buffer: it reaches
        # only the subscribers connected at the exact emit instant. A tab whose
        # per-session EventSource was momentarily absent at that instant — a
        # transient SSE drop, a reverse-proxy idle-timeout, or browser
        # connection-pool starvation (all common behind a corporate proxy) —
        # misses the frame permanently, so a SERVER-initiated wakeup turn never
        # renders live and the user must hard-refresh (the reported defect). The
        # server-side wakeup itself ran and persisted fine; only the live-view
        # was lost. On (re)subscribe, if the session has a live run RIGHT NOW,
        # replay a synthetic `server_turn_started` to THIS new subscriber so the
        # open tab attaches its existing chat-stream renderer (attachLiveStream)
        # and self-heals with no refresh. `recovered: True` lets the frontend
        # use the replay (reconnecting) attach so the renderer picks up the
        # in-progress stream from the run journal rather than expecting token 0.
        # Idempotent: the frontend dedupes by (session_id, stream_id) — if the
        # original frame WAS delivered this is a harmless no-op there.
        try:
            recover_stream_id = active_stream_id_for_session(sid)
            if recover_stream_id:
                pending_started_at = None
                try:
                    recover_session = get_session(sid, metadata_only=True)
                    pending_started_at = getattr(recover_session, "pending_started_at", None)
                except Exception:
                    logger.debug(
                        "session-stream recovery could not read pending_started_at for %s",
                        sid,
                        exc_info=True,
                    )
                _sse(handler, 'server_turn_started', {
                    "session_id": sid,
                    "stream_id": recover_stream_id,
                    "pending_started_at": pending_started_at,
                    "source": "subscribe_recovery",
                    "recovered": True,
                })
            else:
                # ── Server-initiated turn that FINISHED during the SSE gap ──
                # The block above only heals a turn that is live RIGHT NOW. But
                # a server-initiated turn (self-wake / cron / restart hook) can
                # start AND finish entirely inside the gap: the fire-and-forget
                # `server_turn_started` reached no subscriber, and by the time
                # this tab reconnects the run has already cleared from
                # ACTIVE_RUNS — so active_stream_id_for_session returns None and
                # nothing above replays. The turn IS persisted, but this tab's
                # transcript stays stale until a hard refresh (the reported
                # visible-tab defect). Detect it by comparing the persisted
                # message_count against what this (re)subscribing tab last knew
                # (?known_count). If the server is AHEAD, emit a lightweight
                # `session-updated` frame so the tab does an INCREMENTAL,
                # swap-in-place message sync (frontend reuses #5189's
                # keepStaleUntilLoaded loadSession path — NO clear+refetch, so
                # the #5177/#5189 blank-gap jump is not reintroduced). Carries
                # only counts (no transcript) to stay cheap. Skipped
                # entirely when the tab didn't report a count or the persisted
                # count is unknown (legacy sidecar) → never a spurious reload.
                if subscriber_known_count is not None:
                    persisted_count = persisted_message_count_for_session(sid)
                    if should_emit_session_updated(subscriber_known_count, persisted_count):
                        _sse(handler, 'session-updated', {
                            "session_id": sid,
                            "message_count": persisted_count,
                            "known_count": subscriber_known_count,
                            "source": "subscribe_recovery",
                        })
        except _CLIENT_DISCONNECT_ERRORS:
            # Client vanished mid-recovery — re-raise so the outer handler
            # treats it as a normal disconnect and the finally still cleans up.
            raise
        except Exception:
            logger.debug(
                "session-stream on-subscribe recovery failed for %s", sid,
                exc_info=True,
            )

        while True:
            try:
                payload = q.get(timeout=_SSE_HEARTBEAT_INTERVAL_SECONDS)
            except queue.Empty:
                handler.wfile.write(b': keepalive\n\n')
                handler.wfile.flush()
                continue
            if payload is None:
                break
            event_name, data = payload
            _sse(handler, event_name, data)
    except _CLIENT_DISCONNECT_ERRORS:
        pass  # client went away — normal for long-lived connections
    finally:
        ch.unsubscribe(q)


def _handle_clarify_inject(handler, parsed):
    """Inject a fake pending clarify prompt -- loopback-only, used by automated tests."""
    qs = parse_qs(parsed.query)
    sid = qs.get("session_id", [""])[0]
    question = qs.get("question", ["Which option?"])[0]
    choices = qs.get("choices", [])
    if sid:
        submit_clarify_pending(
            sid,
            {
                "question": question,
                "choices_offered": choices,
                "session_id": sid,
                "kind": "clarify",
            },
        )
        return j(handler, {"ok": True, "session_id": sid})
    return j(handler, {"error": "session_id required"}, status=400)


__routes_exports__ = (
    "_handle_approval_pending",
    "_handle_approval_sse_stream",
    "_handle_approval_inject",
    "_handle_clarify_pending",
    "_handle_clarify_sse_stream",
    "_handle_session_sse_stream",
    "_handle_clarify_inject",
)
