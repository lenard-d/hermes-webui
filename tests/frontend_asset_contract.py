"""Authoritative frontend asset inventory used by architecture test harnesses.

Classic families list their direct browser load order. Native-module families
list their complete dependency surface and identify one page entrypoint.
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "static"

FRONTEND_FAMILIES = (
    "style",
    "i18n",
    "ui",
    "workspace",
    "sessions",
    "commands",
    "messages",
    "panels",
    "boot",
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
    "stream_anchor_scene.js",
    "stream_run_journal.js",
    "stream_live_tools.js",
    "stream_renderer.js",
    "stream.js",
    "composer_approvals.js",
    "session_events.js",
    "clarify.js",
    "notifications_background.js",
)

_COMMAND_MODULE_NAMES = (
    "desktop-companion.js",
    "manual-compression.js",
    "run-controls.js",
    "session-history.js",
    "registry.js",
    "index.js",
)

_BOOT_MODULE_NAMES = (
    "server-lifecycle.js",
    "run-control.js",
    "speech-capture.js",
    "public-interfaces.js",
    "appearance.js",
    "navigation.js",
    "composer.js",
    "voice-mode.js",
    "index.js",
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
    if family == "workspace":
        return (
            STATIC_DIR / "workspace.js",
            *_numbered_parts("workspace_parts", ".js"),
        )
    if family == "sessions":
        return (*_numbered_parts("sessions_parts", ".js"), STATIC_DIR / "sessions.js")
    if family == "commands":
        return tuple(
            STATIC_DIR / "modules" / "commands" / name
            for name in _COMMAND_MODULE_NAMES
        )
    if family == "messages":
        return (
            STATIC_DIR / "messages.js",
            *(STATIC_DIR / "messages_parts" / name for name in _MESSAGE_PART_NAMES),
        )
    if family == "panels":
        return (STATIC_DIR / "panels.js", *_numbered_parts("panels_parts", ".js"))
    if family == "boot":
        return tuple(
            STATIC_DIR / "modules" / "boot" / name
            for name in _BOOT_MODULE_NAMES
        )
    raise ValueError(f"unknown frontend asset family: {family}")


def family_source(family: str) -> str:
    """Read one family's source in browser load order for source-level tests."""

    return "".join(
        path.read_text(encoding="utf-8") for path in family_asset_paths(family)
    )


def family_entrypoint_path(family: str) -> Path | None:
    """Return the asset loaded by the page for a native-module family."""

    if family == "boot":
        return STATIC_DIR / "modules" / "boot" / "index.js"
    if family == "commands":
        return None  # imported by boot/index.js through the compatibility seam
    paths = family_asset_paths(family)
    return paths[0] if paths else None
