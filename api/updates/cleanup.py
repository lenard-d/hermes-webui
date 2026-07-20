"""Filesystem cleanup performed immediately before loading updated code."""

from __future__ import annotations

import shutil
from pathlib import Path


def purge_python_bytecode(repo_dir: Path | None) -> None:
    """Remove stale bytecode that could outlive a same-second source update."""
    if repo_dir is None or not repo_dir.exists():
        return
    try:
        for pycache in repo_dir.rglob("__pycache__"):
            try:
                shutil.rmtree(pycache, ignore_errors=True)
            except OSError:
                pass
    except Exception:
        pass


__all__ = ["purge_python_bytecode"]
