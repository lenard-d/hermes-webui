"""Legacy imports for run-owned background task tracking.

New code should import :mod:`api.runs.background`.  The two mutable registries
below are the owner objects themselves; this adapter never creates state.
"""

# ruff: noqa: F401 -- compatibility exports are consumed by importers

from api.runs.background import (
    _BACKGROUND_TASKS,
    _BTW_TRACKING,
    cleanup_btw,
    complete_background,
    get_background_tasks,
    get_results,
    track_background,
    track_btw,
)

__all__ = [
    "cleanup_btw",
    "complete_background",
    "get_background_tasks",
    "get_results",
    "track_background",
    "track_btw",
]
