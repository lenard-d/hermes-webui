"""Persistent per-session event-channel ownership and reconnect helpers.

``StreamChannel`` owns one agent turn and disappears at terminal cleanup.  This
module owns the lower-frequency observation channel that survives between turns:
the registry, its lock ordering, subscriber lifetime, and the read-only recovery
lookups used when a browser reconnects.  The background-process maintenance
thread remains in :mod:`api.background_process`; it asks this module to collect
expired channels and then cleans its own completion-delivery state.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Optional


# Preserve the established log category while ``api.background_process`` keeps
# the compatibility facade; operational filters should not change for a split.
logger = logging.getLogger("api.background_process")


# SESSION_CHANNELS maps WebUI session_id -> SessionChannel. Each channel owns
# zero or more queue.Queue subscribers (one per active EventSource tab) and is
# collected after the last subscriber drops + a grace period, or after the
# session has been idle for SESSION_CHANNEL_IDLE_TTL_SECS.
SESSION_CHANNELS: dict[str, "SessionChannel"] = {}
SESSION_CHANNELS_LOCK = threading.Lock()


class SessionChannel:
    """A long-lived multi-subscriber SSE channel for one WebUI session.

    Subscribers are ``queue.Queue`` instances owned by the SSE route handler —
    one per active EventSource (tab). ``emit`` broadcasts to every live
    subscriber; subscribers whose buffer is full silently drop the event.

    Lifecycle:
      - Created on demand when the first tab subscribes.
      - ``subscribe`` / ``unsubscribe`` are refcount-style: zero subscribers
        does not immediately collect the channel; the reaper waits through the
        configured navigation grace period.
      - An unsubscribed channel is collectable after that grace period or the
        configured idle TTL. A live subscriber always keeps it alive.
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        now = time.time()
        self.created_at = now
        self.last_event_at = now
        self.last_subscriber_drop_at: float | None = None

    def subscribe(self, maxsize: int = 16) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._subscribers.append(q)
            self.last_subscriber_drop_at = None
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass
            if not self._subscribers:
                self.last_subscriber_drop_at = time.time()

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def emit(self, event: str, data: Any) -> int:
        """Broadcast ``(event, data)`` and return the delivered subscriber count."""
        delivered = 0
        with self._lock:
            subscribers = list(self._subscribers)
            self.last_event_at = time.time()
        for subscriber in subscribers:
            try:
                subscriber.put_nowait((event, data))
                delivered += 1
            except queue.Full:
                logger.debug("SessionChannel emit: subscriber buffer full, dropping")
            except Exception:
                logger.debug("SessionChannel emit failed", exc_info=True)
        return delivered

    def reaper_should_collect(self, now: float) -> bool:
        """Return whether an unsubscribed channel has exceeded a lifecycle limit."""
        from api import config as _cfg

        with self._lock:
            subscriber_count = len(self._subscribers)
            drop_at = self.last_subscriber_drop_at
            created_at = self.created_at

        if subscriber_count > 0:
            return False
        grace = float(getattr(_cfg, "SESSION_CHANNEL_SUBSCRIBER_GRACE_SECS", 60))
        if drop_at is not None and (now - drop_at) >= grace:
            return True
        ttl = float(getattr(_cfg, "SESSION_CHANNEL_IDLE_TTL_SECS", 14400))
        return (now - created_at) >= ttl


def get_or_create_session_channel(session_id: str) -> SessionChannel:
    """Return the channel for ``session_id``, creating it on first access."""
    with SESSION_CHANNELS_LOCK:
        channel = SESSION_CHANNELS.get(session_id)
        if channel is None:
            channel = SessionChannel(session_id)
            SESSION_CHANNELS[session_id] = channel
        return channel


def get_session_channel(session_id: str) -> Optional[SessionChannel]:
    """Return an existing channel without creating one."""
    with SESSION_CHANNELS_LOCK:
        return SESSION_CHANNELS.get(session_id)


def subscribe_to_session_channel(
    session_id: str, maxsize: int = 16
) -> tuple[SessionChannel, queue.Queue]:
    """Atomically resolve a channel and acquire one subscriber slot.

    Lock order is ``SESSION_CHANNELS_LOCK`` then ``SessionChannel._lock``, the
    same order used by expiry collection. The caller owns the returned slot and
    must call ``channel.unsubscribe(queue)`` on every exit path.
    """
    with SESSION_CHANNELS_LOCK:
        channel = SESSION_CHANNELS.get(session_id)
        if channel is None:
            channel = SessionChannel(session_id)
            SESSION_CHANNELS[session_id] = channel
        subscriber = channel.subscribe(maxsize=maxsize)
        return channel, subscriber


def collect_expired_session_channels(now: float) -> list[str]:
    """Remove and return all channels collectable at ``now`` atomically.

    The registry lock stays held while each channel snapshots its subscriber
    state, preserving the same lock order as atomic subscription.  Returning
    the collected ids lets the maintenance owner clean related state without
    exposing registry mutation to it.
    """
    collected: list[str] = []
    with SESSION_CHANNELS_LOCK:
        for session_id, channel in list(SESSION_CHANNELS.items()):
            if channel.reaper_should_collect(now):
                SESSION_CHANNELS.pop(session_id, None)
                collected.append(session_id)
    return collected


def active_stream_id_for_session(session_id: str) -> Optional[str]:
    """Return a live stream id for ``session_id``, if one is registered."""
    from api import config as _cfg

    try:
        with _cfg.ACTIVE_RUNS_LOCK:
            for stream_id, metadata in (_cfg.ACTIVE_RUNS or {}).items():
                if (
                    isinstance(metadata, dict)
                    and metadata.get("session_id") == session_id
                ):
                    return str(stream_id)
    except Exception:
        logger.debug(
            "active_stream_id_for_session lookup failed for %s",
            session_id,
            exc_info=True,
        )
    return None


def persisted_message_count_for_session(session_id: str) -> Optional[int]:
    """Return the persisted message count for reconnect reconciliation."""
    try:
        from api.sessions.store import get_session

        session = get_session(session_id, metadata_only=True)
        count = getattr(session, "_metadata_message_count", None)
        if count is None:
            messages = getattr(session, "messages", None)
            count = len(messages) if isinstance(messages, list) and messages else None
        return int(count) if count is not None else None
    except Exception:
        logger.debug(
            "persisted_message_count_for_session lookup failed for %s",
            session_id,
            exc_info=True,
        )
        return None


def should_emit_session_updated(
    subscriber_known_count: Optional[int],
    persisted_count: Optional[int],
) -> bool:
    """Return whether reconnect recovery should announce newer persisted state."""
    if subscriber_known_count is None or persisted_count is None:
        return False
    return persisted_count > subscriber_known_count
