"""Bounded multi-subscriber channel for live browser turn events."""

from __future__ import annotations

import collections
import logging
import queue
import threading


logger = logging.getLogger(__name__)


class StreamChannel:
    """Broadcast SSE events to every connected browser tab for a stream.

    While no tab is connected, events are buffered so the first/reconnected
    subscriber still receives the stream tail that arrived during the gap.
    Once one or more subscribers are attached, new events are broadcast to all
    of them instead of being consumed destructively by a single queue reader.
    """

    # Cap on the offline replay buffer (drop-oldest). While no tab is subscribed,
    # put_nowait() buffers the stream tail so a first/reconnecting subscriber can
    # catch up. But a client that disconnects without cancelling leaves the turn
    # running with zero subscribers, so an unbounded buffer here grows for the
    # WHOLE turn (a busy turn emits thousands of coalesced token frames) — an OOM
    # risk per abandoned turn (#4633). Bounding to the most recent N frames keeps
    # a reconnecting tab's needed *tail* intact; older dropped frames stay
    # recoverable via the run journal by last_event_id. 8192 is generous enough
    # to hold a long multi-tool turn's backlog while capping worst-case memory to
    # a fixed number of small (event, data, id) tuples — deliberately far above
    # the per-subscriber queue cap below (that queue drops on a *slow* reader;
    # this buffer must survive a legitimate reconnect gap).
    _OFFLINE_BUFFER_MAXLEN = 8192
    # Per-subscriber queue cap (drop-oldest on full). Each connected tab gets its
    # own queue; a slow/backpressured or backgrounded tab used to hold an
    # UNBOUNDED queue.Queue that grew for the WHOLE turn (the producer is the
    # agent token stream), an OOM risk with many tabs × long agentic turns. This
    # caps the per-tab live-broadcast growth to a fixed bound.
    #
    # Bound is intentionally EQUAL to _OFFLINE_BUFFER_MAXLEN, not the much
    # smaller SessionChannel per-subscriber cap of 16. StreamChannel carries the
    # live chat token stream (thousands of frames per turn) and, unlike
    # SessionChannel's low-frequency UI pings, has a reconnect-replay contract:
    # a tab that briefly disconnected must receive the full retained offline
    # tail on resubscribe. Capping below the offline buffer would truncate that
    # replay and force every flaky-network reconnect through the run journal
    # (disk reads) instead of the in-memory fast path. Matching the offline
    # buffer bound preserves that contract while bounding live-broadcast memory
    # to the SAME worst-case #4633 already accepted (a fixed number of small
    # (event, data, id) tuples). The SSE write deadline
    # (SSE_WRITE_DEADLINE_SECONDS) independently breaks a stuck socket within
    # ~20s, so the overflow window is short; this cap bounds it by frame count
    # too. Older frames stay recoverable via the run journal by last_event_id.
    _SUBSCRIBER_QUEUE_MAXSIZE = _OFFLINE_BUFFER_MAXLEN

    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        self._offline_buffer: collections.deque = collections.deque(
            maxlen=self._OFFLINE_BUFFER_MAXLEN
        )
        # Frames evicted at the cap from the CURRENT buffer content. Scoped to
        # the buffer, NOT to an attach cycle: it resets exactly when the buffer
        # itself is cleared (first live broadcast), never on subscribe/
        # unsubscribe alone — a drain is a non-destructive copy, so after a
        # transient attach the buffer is STILL truncated and reporting 0 would
        # hand the next subscriber a silently-holed tail. Whether a given
        # reconnect actually NEEDS the evicted frames is decided server-side
        # against offline_first_event_id (its cursor may sit inside the
        # retained tail). Also gates the one-shot eviction log.
        self._offline_dropped = 0
        # Cumulative evictions over the channel's lifetime, never reset — for ops
        # visibility via diagnostic_snapshot().
        self._offline_dropped_total = 0
        # Cumulative per-subscriber queue drops over the channel's lifetime
        # (broadcast + replay paths), never reset — ops visibility for slow tabs.
        self._subscriber_dropped_total = 0
        self._last_event_id: str | None = None

    def subscribe(self) -> queue.Queue:
        q, _snapshot = self.subscribe_with_snapshot()
        return q

    def subscribe_with_snapshot(self) -> tuple[queue.Queue, dict[str, object]]:
        q: queue.Queue = queue.Queue(maxsize=self._SUBSCRIBER_QUEUE_MAXSIZE)
        with self._lock:
            # Replay buffered events to the new subscriber INSIDE the lock so a
            # concurrent put_nowait() can't broadcast a newer event before we
            # finish replaying the older buffered tail. The queue is bounded, so
            # put_nowait raises queue.Full once the cap is reached — drop the
            # OLDEST already-replayed frame and retry, keeping the most recent
            # tail (a reconnecting tab needs the tail; older frames stay
            # recoverable via the run journal by last_event_id). Holding the
            # lock here is safe: no other put_nowait() can interleave. Per Opus
            # advisor on stage-292.
            replayed_dropped = 0
            for item in self._offline_buffer:
                while True:
                    try:
                        q.put_nowait(item)
                        break
                    except queue.Full:
                        # Drop oldest to make room for the newer (more useful)
                        # tail frame. The drained frame is the oldest in this
                        # subscriber's replay window only.
                        try:
                            q.get_nowait()
                        except queue.Empty:
                            # A concurrent consumer drained the queue between
                            # our Full and get_nowait — the queue now has space,
                            # so retry the put instead of dropping `item`. This
                            # path runs under self._lock with a freshly-created
                            # queue (no concurrent consumer), so it is not
                            # reached in practice, but `continue` is the
                            # correct, race-safe rule (see the broadcast path).
                            continue
                        replayed_dropped += 1
            if replayed_dropped:
                self._subscriber_dropped_total += replayed_dropped
                logger.debug(
                    "StreamChannel subscriber replay dropped %d oldest frames "
                    "(cap=%d) while catching up on %d buffered events",
                    replayed_dropped,
                    self._SUBSCRIBER_QUEUE_MAXSIZE,
                    len(self._offline_buffer),
                )
            first = self._offline_buffer[0] if self._offline_buffer else None
            snapshot = {
                "offline_buffered_events": len(self._offline_buffer),
                # Surface eviction so the SSE handler can tell the tail it is
                # about to drain may be truncated (older frames were dropped at
                # the cap) and must be proven contiguous before streaming.
                "offline_dropped_events": self._offline_dropped,
                # Event id of the oldest retained frame: the handler needs run-
                # journal coverage only for (client cursor → this frame); a
                # cursor already inside the retained tail needs no journal.
                "offline_first_event_id": (
                    first[2] if first is not None and len(first) >= 3 else None
                ),
                "last_event_id": self._last_event_id,
            }
            self._subscribers.append(q)
        return q, snapshot

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def note_last_event_id(self, event_id: str | None) -> None:
        """Record the latest journal event id without changing the queue shape."""
        if not event_id:
            return
        with self._lock:
            self._last_event_id = event_id

    def put_nowait(self, item: tuple[str, object] | tuple[str, object, str | None]) -> None:
        event_id = item[2] if len(item) >= 3 else None
        with self._lock:
            if event_id:
                self._last_event_id = event_id
            subscribers = list(self._subscribers)
            if not subscribers:
                # deque(maxlen) evicts the oldest frame automatically when full.
                # Log once on the first eviction (debug: an abandoned/disconnected
                # turn is expected to hit this) and keep a running dropped count
                # for diagnostics.
                if len(self._offline_buffer) >= self._OFFLINE_BUFFER_MAXLEN:
                    if self._offline_dropped == 0:  # first eviction this cycle
                        logger.debug(
                            "StreamChannel offline buffer full (cap=%d); dropping "
                            "oldest frames while no subscriber is connected",
                            self._OFFLINE_BUFFER_MAXLEN,
                        )
                    self._offline_dropped += 1
                    self._offline_dropped_total += 1
                self._offline_buffer.append(item)
                return
            # A subscriber is live: events now broadcast directly, so the offline
            # tail is drained. Reset the per-cycle eviction count (which also
            # re-arms the one-shot log) so the NEXT disconnect/overflow cycle
            # reports and logs its own truncation, not a stale carry-over.
            self._offline_buffer.clear()
            self._offline_dropped = 0
        # Broadcast outside the lock so a slow put_nowait doesn't block other
        # subscribers or producers. The queue is bounded; on queue.Full drop the
        # OLDEST frame and retry so a slow/backpressured tab keeps its most
        # recent tail instead of growing unbounded for the whole turn. Older
        # frames stay recoverable via the run journal by last_event_id. Mirrors
        # SessionChannel.emit's drop-on-full contract.
        broadcast_dropped = 0
        for q in subscribers:
            while True:
                try:
                    q.put_nowait(item)
                    break
                except queue.Full:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        # A concurrent consumer (the SSE handler thread)
                        # drained the queue between our Full and get_nowait.
                        # The queue now has space — retry the put so `item` is
                        # delivered. `break` here would silently discard `item`,
                        # and if `item` is a terminal frame (stream_end/error/
                        # cancel) the subscriber never receives it and the client
                        # stays attached indefinitely (spinner-forever). Having
                        # space is exactly the condition we want, so continue.
                        continue
                    broadcast_dropped += 1
        if broadcast_dropped:
            with self._lock:
                self._subscriber_dropped_total += broadcast_dropped
            logger.debug(
                "StreamChannel broadcast dropped %d oldest frames across %d "
                "subscriber queue(s) (cap=%d)",
                broadcast_dropped,
                len(subscribers),
                self._SUBSCRIBER_QUEUE_MAXSIZE,
            )

    def diagnostic_snapshot(self) -> dict[str, object]:
        """Return non-sensitive stream observation counters for health checks."""
        with self._lock:
            return {
                "subscriber_count": len(self._subscribers),
                "offline_buffered_events": len(self._offline_buffer),
                # Cumulative over the channel lifetime (ops visibility), vs. the
                # per-cycle count subscribe_with_snapshot() reports for truncation.
                "offline_dropped_events": self._offline_dropped_total,
                # Cumulative per-subscriber queue drops (replay + broadcast) over
                # the channel lifetime — surfaces slow/backpressured tabs.
                "subscriber_dropped_events": self._subscriber_dropped_total,
            }


def create_stream_channel() -> StreamChannel:
    return StreamChannel()
