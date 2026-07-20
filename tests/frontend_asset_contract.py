"""Test-only path and source helpers for directly loaded frontend families.

Production loads every file with a static ``<link>`` or ``<script>`` tag.  These
helpers deliberately do not read the split manifests or model a runtime loader.
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "static"

FRONTEND_FAMILIES = (
    "style",
    "i18n",
    "ui",
    "sessions",
    "messages",
    "panels",
)

_I18N_PART_NAMES = (
    "helpers.js",
    "locale-en.js",
    "locale-it.js",
    "locale-ja.js",
    "locale-ru.js",
    "locale-es.js",
    "locale-de.js",
    "locale-zh.js",
    "locale-zh_hant.js",
    "locale-pt.js",
    "locale-ko.js",
    "locale-fr.js",
    "locale-cs.js",
    "locale-tr.js",
    "locale-pl.js",
    "locale-vi.js",
    "runtime.js",
)

_MESSAGE_PART_NAMES = (
    "markdown_tables.js",
    "composer_context.js",
    "send.js",
    "stream_lifecycle.js",
    "stream.js",
    "composer_approvals.js",
    "session_events.js",
    "clarify.js",
    "notifications_background.js",
)


def _numbered_parts(directory: str, suffix: str) -> tuple[Path, ...]:
    pattern = f"[0-9][0-9][0-9]-*{suffix}"
    return tuple(sorted((STATIC_DIR / directory).glob(pattern)))


def family_asset_paths(family: str) -> tuple[Path, ...]:
    """Return one family's required direct-load order without reading a manifest."""

    if family == "style":
        return (STATIC_DIR / "style.css", *_numbered_parts("style_parts", ".css"))
    if family == "i18n":
        return (
            STATIC_DIR / "i18n.js",
            *(STATIC_DIR / "i18n_parts" / name for name in _I18N_PART_NAMES),
        )
    if family == "ui":
        return (STATIC_DIR / "ui.js", *_numbered_parts("ui_parts", ".js"))
    if family == "sessions":
        return (*_numbered_parts("sessions_parts", ".js"), STATIC_DIR / "sessions.js")
    if family == "messages":
        return (
            STATIC_DIR / "messages.js",
            *(STATIC_DIR / "messages_parts" / name for name in _MESSAGE_PART_NAMES),
        )
    if family == "panels":
        return (STATIC_DIR / "panels.js", *_numbered_parts("panels_parts", ".js"))
    raise ValueError(f"unknown frontend asset family: {family}")


def family_source(family: str) -> str:
    """Read one family's source in browser load order for source-level tests."""

    return "".join(
        path.read_text(encoding="utf-8") for path in family_asset_paths(family)
    )
