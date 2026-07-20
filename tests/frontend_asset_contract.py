"""Authoritative frontend asset inventory used by architecture test harnesses.

Classic families list their direct browser load order. Native-module families
list their complete dependency surface and identify one page entrypoint.
"""

from __future__ import annotations

from pathlib import Path
import re


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

_MESSAGE_MODULE_NAMES = (
    "core.js",
    "markdown-tables.js",
    "composer-context.js",
    "send.js",
    "stream-lifecycle.js",
    "anchor-scene.js",
    "run-journal.js",
    "live-tools.js",
    "rendering.js",
    "stream.js",
    "approvals.js",
    "session-events.js",
    "clarify.js",
    "notifications.js",
    "compatibility.js",
    "index.js",
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

_SESSION_MODULE_NAMES = (
    "state.js",
    "lifecycle.js",
    "message-loading.js",
    "message-timeline.js",
    "sidebar-state.js",
    "session-list.js",
    "session-discovery.js",
    "sidebar-interactions.js",
    "sidebar-renderer.js",
    "management.js",
    "legacy-adapter.js",
    "index.js",
)

_ASSISTANT_TURN_ANCHOR_MODULE_NAMES = (
    "model.js",
    "activity-scene.js",
    "legacy-adapter.js",
    "index.js",
)


def module_family_paths(family: str) -> tuple[Path, ...]:
    """Return a native module family's complete source inventory."""

    if family == "sessions":
        directory = STATIC_DIR / "modules" / "sessions"
        return tuple(directory / name for name in _SESSION_MODULE_NAMES)
    if family == "assistant-turn-anchors":
        directory = STATIC_DIR / "modules" / "assistant-turn-anchors"
        return tuple(directory / name for name in _ASSISTANT_TURN_ANCHOR_MODULE_NAMES)
    if family == "boot":
        directory = STATIC_DIR / "modules" / "boot"
        return tuple(directory / name for name in _BOOT_MODULE_NAMES)
    if family == "commands":
        directory = STATIC_DIR / "modules" / "commands"
        return tuple(directory / name for name in _COMMAND_MODULE_NAMES)
    if family == "messages":
        directory = STATIC_DIR / "modules" / "messages"
        return tuple(directory / name for name in _MESSAGE_MODULE_NAMES)
    raise ValueError(f"unknown frontend module family: {family}")

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
        return (STATIC_DIR / "modules" / "sessions" / "index.js",)
    if family == "commands":
        return tuple(
            STATIC_DIR / "modules" / "commands" / name
            for name in _COMMAND_MODULE_NAMES
        )
    if family == "messages":
        return tuple(
            STATIC_DIR / "modules" / "messages" / name
            for name in _MESSAGE_MODULE_NAMES
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
    """Read one family's implementation sources in their documented order."""

    paths = (
        module_family_paths(family)
        if family in {"boot", "commands", "messages", "sessions"}
        else family_asset_paths(family)
    )
    source = "".join(
        path.read_text(encoding="utf-8") for path in paths
    )
    if family == "sessions":
        # Transitional source-extraction harnesses evaluate individual functions
        # outside their module. Present owner-backed mutable bindings under their
        # former local names there; production tests import the real ESM graph.
        source = re.sub(r"\b[A-Za-z][A-Za-z0-9]*Bindings\.", "", source)
    return source


def family_entrypoint_path(family: str) -> Path | None:
    """Return the asset loaded by the page for a native-module family."""

    if family == "boot":
        return STATIC_DIR / "modules" / "boot" / "index.js"
    if family == "commands":
        return None  # imported by boot/index.js through the compatibility seam
    if family == "sessions":
        return STATIC_DIR / "modules" / "sessions" / "index.js"
    if family == "messages":
        return STATIC_DIR / "modules" / "messages" / "index.js"
    paths = family_asset_paths(family)
    return paths[0] if paths else None


def family_direct_asset_paths(family: str) -> tuple[Path, ...]:
    """Return browser entrypoints; native-module dependencies load by import."""

    entrypoint = family_entrypoint_path(family)
    if family in {"boot", "messages", "sessions"}:
        assert entrypoint is not None
        return (entrypoint,)
    if family == "commands":
        return ()
    return family_asset_paths(family)
