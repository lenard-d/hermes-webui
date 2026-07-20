from __future__ import annotations

from pathlib import Path

from tests.frontend_asset_contract import (
    module_family_paths,
    normalize_session_source_for_harnesses,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_PARTS_DIR = REPO_ROOT / "static" / "modules" / "sessions"


def sessions_part_paths() -> list[Path]:
    return list(module_family_paths("sessions"))


def read_sessions_source() -> str:
    source = "".join(
        path.read_text(encoding="utf-8") for path in sessions_part_paths()
    )
    return normalize_session_source_for_harnesses(source)


SESSIONS_SOURCE = read_sessions_source()
