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
    "compression-events.js",
    "content-events.js",
    "control-events.js",
    "send.js",
    "stream-lifecycle.js",
    "stream-progress.js",
    "anchor-scene.js",
    "anchor-live.js",
    "run-journal.js",
    "live-tools.js",
    "rendering.js",
    "stream.js",
    "approvals.js",
    "session-events.js",
    "session-recovery.js",
    "stream-transcript.js",
    "stream-transport.js",
    "terminal-events.js",
    "clarify.js",
    "notifications.js",
    "index.js",
)

_COMMAND_MODULE_NAMES = (
    "forced-skill-directive.js",
    "capability-commands.js",
    "desktop-companion.js",
    "manual-compression.js",
    "model-command.js",
    "preference-commands.js",
    "run-controls.js",
    "session-history.js",
    "workspace-commands.js",
    "command-catalog.js",
    "remote-command-catalog.js",
    "slash-autocomplete.js",
    "command-dropdown.js",
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
    "legacy-interface.js",
    "index.js",
)

_SESSION_MODULE_NAMES = (
    "session-load-state.js",
    "session-list-coordination.js",
    "session-profile-scope.js",
    "session-run-registry.js",
    "session-display.js",
    "session-source.js",
    "composer-drafts.js",
    "session-unread.js",
    "session-run-state.js",
    "session-live-recovery.js",
    "session-visit.js",
    "lifecycle.js",
    "message-loading.js",
    "message-timeline.js",
    "sidebar-render-port.js",
    "sidebar-store.js",
    "sidebar-motion.js",
    "session-navigation.js",
    "sidebar-cache.js",
    "sidebar-selection.js",
    "sidebar-actions.js",
    "sidebar-render-state.js",
    "sidebar-state.js",
    "session-list-render-port.js",
    "session-list-skeleton.js",
    "session-list-reconciliation.js",
    "session-list-loader.js",
    "session-list-refresh.js",
    "sidebar-session-events.js",
    "session-list.js",
    "session-discovery.js",
    "sidebar-interactions.js",
    "sidebar-renderer.js",
    "management.js",
    "index.js",
)

_ASSISTANT_TURN_ANCHOR_MODULE_NAMES = (
    "model.js",
    "activity-scene.js",
    "index.js",
)

_PANEL_MODULE_NAMES = (
    "state.js",
    "core.js",
    "cron-list.js",
    "cron-editor.js",
    "kanban-board.js",
    "kanban-tasks.js",
    "kanban-boards.js",
    "diagnostics.js",
    "skills-memory.js",
    "workspaces.js",
    "profiles.js",
    "settings-state.js",
    "settings-navigation.js",
    "settings-preferences.js",
    "extensions/configuration.js",
    "extensions/installed.js",
    "extensions/installations.js",
    "extensions/catalog.js",
    "extensions/sidecars.js",
    "extensions/diagnostics.js",
    "settings-extensions.js",
    "settings-plugins.js",
    "settings-providers.js",
    "settings-models-auth.js",
    "settings-save.js",
    "runtime-alerts.js",
    "settings-system.js",
    "legacy-interface.js",
    "index.js",
)


UI_MODULE_DIR = STATIC_DIR / "modules" / "ui"
UI_ENTRYPOINT = UI_MODULE_DIR / "index.js"
_UI_MODULE_NAMES = (
    "state.js",
    "navigation.js",
    "media-and-quota.js",
    "model-state.js",
    "model-catalog.js",
    "model-picker-rendering.js",
    "model-selection.js",
    "reasoning-effort.js",
    "composer-footer-fit.js",
    "composer-menu-registry.js",
    "toolsets-controls.js",
    "mobile-composer-config.js",
    "artifact-postprocessing.js",
    "code-postprocessing.js",
    "message-scroll-follow.js",
    "activity-timing.js",
    "composer-controls.js",
    "activity-and-scroll.js",
    "topbar-presentation.js",
    "assistant-turn-presentation.js",
    "activity-presentation.js",
    "composer.js",
    "composer-primary-control.js",
    "composer-queue.js",
    "composer-state.js",
    "app-dialogs.js",
    "clipboard.js",
    "inflight-state.js",
    "live-turn-recovery.js",
    "message-copy-actions.js",
    "reconnect-banner.js",
    "text-to-speech.js",
    "todo-state.js",
    "agent-health-monitor.js",
    "session-recovery.js",
    "system-health-monitor.js",
    "update-banner.js",
    "update-lifecycle.js",
    "update-summary.js",
    "presentation.js",
    "transparent-worklog.js",
    "anchor-scenes.js",
    "live-run-status.js",
    "compression-ui.js",
    "handoff-ui.js",
    "message-render-cache.js",
    "cli-tool-presentation.js",
    "message-scroll-snapshot.js",
    "markdown-renderer.js",
    "live-activity.js",
    "live-turn-preservation.js",
    "markdown-postprocessing.js",
    "message-editing.js",
    "render-support.js",
    "renderer.js",
    "settled-activity-renderer.js",
    "settled-turn-finalization.js",
    "tool-identity.js",
    "tool-call-presentation.js",
    "tool-card-presentation.js",
    "worklog-disclosure-identity.js",
    "worklog-tool-groups.js",
    "live-tool-worklog.js",
    "tool-worklog.js",
    "thinking-lifecycle.js",
    "transparent-turn-presentation.js",
    "worklog-disclosure.js",
    "worklog-reasoning.js",
    "worklog-step-presentation.js",
    "anchor-scene-presentation.js",
    "live-anchor-reconciliation.js",
    "content-postprocessing.js",
    "workspace-preferences.js",
    "workspace-drag-drop.js",
    "workspace-file-actions.js",
    "workspace-tree.js",
    "upload-tray.js",
    "upload-status.js",
    "upload-transport.js",
    "toast-notifications.js",
    "workspace-and-uploads.js",
)


# Extracted-function Node harnesses declare the historical closure variables in
# their own script. Native modules mutate state owned by another module through
# these explicit binding objects, so the harness needs the same live-binding
# semantics without evaluating the complete browser module graph.
UI_TEST_BINDING_PROXIES = r"""
function _uiTestBindingProxy() {
  return new Proxy({}, {
    get(_target, name) { return eval(String(name)); },
    set(_target, name, value) { eval(String(name) + ' = value'); return true; },
  });
}
const composerBindings = _uiTestBindingProxy();
const composerControlsBindings = _uiTestBindingProxy();
const composerStateBindings = _uiTestBindingProxy();
const liveActivityBindings = _uiTestBindingProxy();
const stateBindings = _uiTestBindingProxy();
const transparentWorklogBindings = _uiTestBindingProxy();
"""


def normalize_session_source_for_harnesses(source: str) -> str:
    """Project native owner state onto legacy local names for Node fixtures."""

    source = re.sub(
        r"(?m)^export\s+(?=(?:async\s+)?function|const|let|class)",
        "",
        source,
    )
    source = re.sub(r"\b[A-Za-z][A-Za-z0-9]*Bindings\.", "", source)
    source = source.replace(
        "const _loadGeneration=sessionLoadState.begin(sid);",
        "const _loadGeneration = ++_loadSessionGeneration;\n  _loadingSessionId = sid;",
    )
    source = source.replace(
        "sessionLoadState.isCurrent(sid,_loadGeneration)",
        "(_loadingSessionId===sid&&_loadSessionGeneration===_loadGeneration)",
    )
    source = source.replace(
        "const _isCurrentLoad=()=>"
        "(_loadingSessionId===sid&&_loadSessionGeneration===_loadGeneration);",
        "const _isCurrentLoad = () => _loadingSessionId === sid "
        "&& _loadSessionGeneration === _loadGeneration;",
    )
    owner_aliases = {
        "sessionLoadState.loadingSessionId": "_loadingSessionId",
        "sessionLoadState.generation": "_loadSessionGeneration",
        "sessionLoadState.pendingCarryForwardSnapshot": "_pendingCarryForwardSnapshot",
        "sessionListCoordination.pendingApplyTimer": "_pendingSessionListApplyTimer",
        "sessionListCoordination.pendingPayload": "_pendingSessionListPayload",
        "sessionListCoordination.hasLoadedOnce": "_sessionListHasLoadedOnce",
        "sessionListCoordination.lastScrollAt": "_sessionListLastScrollAt",
        "sessionListCoordination.loadError": "_sessionListLoadError",
        "sessionListCoordination.pointerActive": "_sessionListPointerActive",
    }
    for owner_path, fixture_name in owner_aliases.items():
        source = source.replace(owner_path, fixture_name)
    return source


def ui_module_paths() -> tuple[Path, ...]:
    """Return the semantic UI modules in stable inventory order."""

    return tuple(UI_MODULE_DIR / name for name in _UI_MODULE_NAMES)


def module_family_paths(family: str) -> tuple[Path, ...]:
    """Return a native module family's complete source inventory."""

    names = {
        "assistant-turn-anchors": _ASSISTANT_TURN_ANCHOR_MODULE_NAMES,
        "boot": _BOOT_MODULE_NAMES,
        "commands": _COMMAND_MODULE_NAMES,
        "messages": _MESSAGE_MODULE_NAMES,
        "panels": _PANEL_MODULE_NAMES,
        "sessions": _SESSION_MODULE_NAMES,
    }.get(family)
    if names is None:
        if family == "ui":
            return (*ui_module_paths(), UI_ENTRYPOINT)
        raise ValueError(f"unknown frontend module family: {family}")
    return tuple(STATIC_DIR / "modules" / family / name for name in names)

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
        return (UI_ENTRYPOINT, *ui_module_paths())
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
        return tuple(STATIC_DIR / "modules" / "panels" / name for name in _PANEL_MODULE_NAMES)
    if family == "boot":
        return tuple(
            STATIC_DIR / "modules" / "boot" / name
            for name in _BOOT_MODULE_NAMES
        )
    raise ValueError(f"unknown frontend asset family: {family}")


def family_source(family: str) -> str:
    """Read one family's implementation sources in their documented order."""

    if family == "ui":
        return "".join(path.read_text(encoding="utf-8") for path in ui_module_paths())

    paths = (
        module_family_paths(family)
        if family in {"boot", "commands", "messages", "panels", "sessions"}
        else family_asset_paths(family)
    )
    if family == "panels":
        # Source-extraction harnesses predate ESM. Reconstruct the logical owner
        # without import/export syntax while production imports the real graph.
        state_source = (STATIC_DIR / "modules" / "panels" / "state.js").read_text(encoding="utf-8")
        declarations = []
        for match in re.finditer(
            r"^  ([A-Za-z_$][A-Za-z0-9_$]*): (.*?), // owner:.*$",
            state_source,
            re.MULTILINE,
        ):
            declarations.append(f"let {match.group(1)} = {match.group(2)};\n")
        implementation = "".join(
            path.read_text(encoding="utf-8")
            for path in paths
            if path.name not in {"state.js", "legacy-interface.js", "index.js"}
        )
        implementation = re.sub(r"^import .*?;\n", "", implementation, flags=re.MULTILINE)
        implementation = re.sub(r"^export ", "", implementation, flags=re.MULTILINE)
        implementation = re.sub(r"\bstate\.(?=_[$A-Za-z])", "", implementation)
        return "".join(declarations) + implementation
    source = "".join(
        path.read_text(encoding="utf-8") for path in paths
    )
    if family == "sessions":
        # Transitional source-extraction harnesses evaluate individual functions
        # outside their module. Present owner-backed mutable bindings under their
        # former local names there; production tests import the real ESM graph.
        source = normalize_session_source_for_harnesses(source)
    return source


def family_entrypoint_path(family: str) -> Path | None:
    """Return the asset loaded by the page for a native-module family."""

    if family == "boot":
        return STATIC_DIR / "modules" / "boot" / "index.js"
    if family == "commands":
        return None  # imported by boot/index.js through the compatibility seam
    if family == "assistant-turn-anchors":
        return STATIC_DIR / "modules" / "assistant-turn-anchors" / "index.js"
    if family == "sessions":
        return STATIC_DIR / "modules" / "sessions" / "index.js"
    if family == "messages":
        return STATIC_DIR / "modules" / "messages" / "index.js"
    if family == "panels":
        return STATIC_DIR / "modules" / "panels" / "index.js"
    if family == "ui":
        return UI_ENTRYPOINT
    paths = family_asset_paths(family)
    return paths[0] if paths else None


def family_direct_asset_paths(family: str) -> tuple[Path, ...]:
    """Return browser entrypoints; native-module dependencies load by import."""

    entrypoint = family_entrypoint_path(family)
    if family in {"boot", "messages", "panels", "sessions", "ui"}:
        assert entrypoint is not None
        return (entrypoint,)
    if family == "commands":
        return ()
    return family_asset_paths(family)


def family_entry_paths(family: str) -> tuple[Path, ...]:
    """Backward-compatible name for the direct browser entrypoint inventory."""

    return family_direct_asset_paths(family)


def direct_family_asset_paths(family: str) -> tuple[Path, ...]:
    """Compatibility alias for the direct browser entrypoint inventory."""

    return family_direct_asset_paths(family)
