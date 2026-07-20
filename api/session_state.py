"""Canonical process-local state shared across WebUI session lifecycles.

This module owns only coordination state whose identity spans HTTP, run,
background-process, and persistence adapters.  Durable session data remains in
``api.sessions``; live run transport remains in ``api.runs.runtime_state``.
Keeping the registries here prevents the configuration package from becoming
the owner of unrelated runtime lifecycles while preserving one canonical
object for legacy ``api.config`` aliases.
"""

from __future__ import annotations

import collections
import threading
import time
import weakref


# Serializes compact-session persistence/index mutations.  Per-session locks
# below provide the narrower owner for normal record writes; this lock remains
# for operations that mutate the shared cache/index as one transaction.
LOCK = threading.Lock()
CHAT_LOCK = threading.Lock()

# Compact in-memory Session projection cache.
SESSIONS: collections.OrderedDict = collections.OrderedDict()

# Goal/process-completion markers are claimed by either run admission or the
# background wakeup lifecycle.  The containers must be shared by identity.
PENDING_GOAL_CONTINUATION: set = set()
PROCESS_SESSION_INDEX: dict = {}
PROCESS_SESSION_INDEX_LOCK = threading.Lock()
PENDING_BG_TASK_COMPLETIONS: set = set()
BG_TASK_COMPLETE_EVENTS_SEEN: dict = {}
BG_TASK_COMPLETE_EVENTS_SEEN_LOCK = threading.Lock()
DEFERRED_PROCESS_WAKEUPS: dict = {}
DEFERRED_PROCESS_WAKEUPS_LOCK = threading.Lock()

# Session-level SSE channels outlive individual runs.  These values are public
# operator policy and remain patchable through the compatibility facade.
SESSION_CHANNEL_IDLE_TTL_SECS: int = 14_400
SESSION_CHANNEL_SUBSCRIBER_GRACE_SECS: int = 60

SERVER_START_TIME = time.time()


# A weak registry keeps a lock discoverable for exactly as long as a holder or
# waiter owns a strong reference.  Compression aliases old/new ids to the same
# object, preserving serialization across the session-id rotation.
SESSION_AGENT_LOCKS: weakref.WeakValueDictionary[str, threading.Lock] = (
    weakref.WeakValueDictionary()
)
SESSION_AGENT_LOCKS_LOCK = threading.Lock()


def session_agent_lock(session_id: str) -> threading.Lock:
    """Return the canonical mutation lock for one session id."""
    with SESSION_AGENT_LOCKS_LOCK:
        lock = SESSION_AGENT_LOCKS.get(session_id)
        if lock is None:
            lock = threading.Lock()
            SESSION_AGENT_LOCKS[session_id] = lock
        return lock


def alias_session_agent_lock(
    old_session_id: str,
    new_session_id: str,
    held_lock: threading.Lock,
) -> None:
    """Bind a compression continuation to its already-held session owner.

    A competing owner for either id is an invariant violation.  A missing old
    alias is valid: weak-registry pruning may race with the caller while the
    caller's ``held_lock`` reference still proves ownership.
    """
    with SESSION_AGENT_LOCKS_LOCK:
        old_owner = SESSION_AGENT_LOCKS.get(old_session_id)
        new_owner = SESSION_AGENT_LOCKS.get(new_session_id)
        if old_owner is not None and old_owner is not held_lock:
            raise RuntimeError("old session id has another lock owner")
        if new_owner is not None and new_owner is not held_lock:
            raise RuntimeError("new session id has another lock owner")
        SESSION_AGENT_LOCKS[old_session_id] = held_lock
        SESSION_AGENT_LOCKS[new_session_id] = held_lock


__all__ = [
    "BG_TASK_COMPLETE_EVENTS_SEEN",
    "BG_TASK_COMPLETE_EVENTS_SEEN_LOCK",
    "CHAT_LOCK",
    "DEFERRED_PROCESS_WAKEUPS",
    "DEFERRED_PROCESS_WAKEUPS_LOCK",
    "LOCK",
    "PENDING_BG_TASK_COMPLETIONS",
    "PENDING_GOAL_CONTINUATION",
    "PROCESS_SESSION_INDEX",
    "PROCESS_SESSION_INDEX_LOCK",
    "SERVER_START_TIME",
    "SESSIONS",
    "SESSION_AGENT_LOCKS",
    "SESSION_AGENT_LOCKS_LOCK",
    "SESSION_CHANNEL_IDLE_TTL_SECS",
    "SESSION_CHANNEL_SUBSCRIBER_GRACE_SECS",
    "alias_session_agent_lock",
    "session_agent_lock",
]
