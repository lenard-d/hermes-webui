"""Atomic filesystem primitives shared by session persistence owners."""

from __future__ import annotations

import os
import time
from pathlib import Path

WINDOWS_REPLACE_MAX_RETRIES = 5
WINDOWS_REPLACE_INITIAL_DELAY = 0.05


def safe_replace(src: Path, dst: Path) -> None:
    """Atomically replace *dst*, retrying transient Windows file locks."""
    if os.name != "nt":
        os.replace(src, dst)
        return

    delay = WINDOWS_REPLACE_INITIAL_DELAY
    for attempt in range(WINDOWS_REPLACE_MAX_RETRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == WINDOWS_REPLACE_MAX_RETRIES - 1:
                raise
            time.sleep(delay)
            delay *= 2
