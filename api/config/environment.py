"""Profile-scoped environment ownership and restoration invariants."""

from __future__ import annotations

import threading
from contextlib import contextmanager


# The context exists before config.yaml is loaded because env expansion consults
# it during the package entrypoint's import-time reload.
_thread_ctx = threading.local()

# Every process-wide environment writer shares this owner.  Live transport does
# not own process environment synchronization.
environment_mutation_lock = threading.Lock()


def _set_thread_env(**kwargs) -> None:
    _thread_ctx.env = kwargs


def _clear_thread_env() -> None:
    _thread_ctx.env = {}


def set_thread_env(env: dict[str, str]) -> None:
    """Install one normalized thread-local profile environment."""
    _set_thread_env(**dict(env))


def clear_thread_env() -> None:
    """Clear the current thread-local profile environment."""
    _clear_thread_env()


def is_process_env_fallback_blocked() -> bool:
    """Return whether this profile scope rejects process-env fallback."""
    return bool(getattr(_thread_ctx, "block_process_env_fallback", False))


@contextmanager
def thread_env_scope(env: dict[str, str], *, block_process_env_fallback: bool = False):
    """Install one environment and restore the complete previous scope."""
    previous_env = dict(getattr(_thread_ctx, "env", {}))
    previous_block = bool(getattr(_thread_ctx, "block_process_env_fallback", False))
    _set_thread_env(**dict(env))
    _thread_ctx.block_process_env_fallback = bool(block_process_env_fallback)
    try:
        yield
    finally:
        _thread_ctx.block_process_env_fallback = previous_block
        if previous_env:
            _set_thread_env(**previous_env)
        else:
            _clear_thread_env()
