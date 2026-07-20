from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_PARTS_DIR = REPO_ROOT / "static" / "modules" / "sessions"
SESSIONS_MANIFEST = SESSIONS_PARTS_DIR / "manifest.json"


def sessions_part_paths() -> list[Path]:
    manifest = json.loads(SESSIONS_MANIFEST.read_text(encoding="utf-8"))
    return [SESSIONS_PARTS_DIR / name for name in manifest["modules"]]


def read_sessions_source() -> str:
    source = "".join(
        path.read_text(encoding="utf-8") for path in sessions_part_paths()
    )
    return re.sub(r"\b[A-Za-z][A-Za-z0-9]*Bindings\.", "", source)


SESSIONS_SOURCE = read_sessions_source()
