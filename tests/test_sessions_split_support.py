from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_PARTS_DIR = REPO_ROOT / "static" / "sessions_parts"
SESSIONS_MANIFEST = SESSIONS_PARTS_DIR / "manifest.json"


def sessions_part_paths() -> list[Path]:
    manifest = json.loads(SESSIONS_MANIFEST.read_text(encoding="utf-8"))
    return [SESSIONS_PARTS_DIR / name for name in manifest["parts"]]


def read_sessions_source() -> str:
    return "".join(path.read_text(encoding="utf-8") for path in sessions_part_paths())


SESSIONS_SOURCE = read_sessions_source()
