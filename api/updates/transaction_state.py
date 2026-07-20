"""Process-local state and target identity for update transactions."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from api.config import REPO_ROOT as _DEFAULT_REPO_ROOT
from api.config import get_agent_source_dir

from .policy import DEFAULT_UPDATE_CHANNEL, _normalize_channel


@dataclass(frozen=True)
class UpdateTarget:
    """A resolved checkout and the channel that may advance it."""

    name: str
    path: Path
    channel: str


REPO_ROOT = _DEFAULT_REPO_ROOT
_AGENT_DIR = get_agent_source_dir()
_apply_lock = threading.Lock()
_status_cache_lock = threading.Lock()
_status_cache: dict = {"checked_at": 0}


def configure_status_cache(*, cache: dict, lock) -> None:
    """Install the update-status cache invalidated after checkout mutations."""
    global _status_cache, _status_cache_lock
    _status_cache = cache
    _status_cache_lock = lock


def apply_lock():
    """Return the single process-local lock serializing checkout mutations."""
    return _apply_lock


def invalidate_status_cache() -> None:
    """Force the next status request to inspect the mutated checkouts."""
    with _status_cache_lock:
        _status_cache["checked_at"] = 0


def resolve_target(target: str, channel: str | None) -> UpdateTarget | dict:
    """Resolve target identity and fail closed for unknown or missing checkouts."""
    resolved_channel = _normalize_channel(channel or DEFAULT_UPDATE_CHANNEL)
    if target == "webui":
        path = REPO_ROOT
    elif target == "agent":
        path = _AGENT_DIR
        resolved_channel = DEFAULT_UPDATE_CHANNEL
    else:
        return {"ok": False, "message": f"Unknown target: {target}"}

    if path is None or not (path / ".git").exists():
        return {"ok": False, "message": "Not a git repository"}
    return UpdateTarget(target, Path(path), resolved_channel)


__all__ = [
    "UpdateTarget",
    "apply_lock",
    "configure_status_cache",
    "invalidate_status_cache",
    "resolve_target",
]
