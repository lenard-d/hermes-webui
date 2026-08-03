from __future__ import annotations

import re
import subprocess

from tests.test_sessions_split_support import (
    REPO_ROOT,
    SESSIONS_PARTS_DIR,
    read_sessions_source,
    sessions_part_paths,
)


def test_sessions_use_a_semantic_module_inventory_without_runtime_manifests():
    modules = sessions_part_paths()
    assert len(modules) >= 10
    assert len(modules) == len(set(modules))
    assert {
        "composer-drafts.js",
        "session-display.js",
        "session-list-coordination.js",
        "session-live-recovery.js",
        "session-load-state.js",
        "session-profile-scope.js",
        "session-run-registry.js",
        "session-run-state.js",
        "session-source.js",
        "session-unread.js",
        "session-visit.js",
    } <= {path.name for path in modules}
    assert modules[-1].name == "index.js"
    assert not (SESSIONS_PARTS_DIR / "manifest.json").exists()
    assert not any(path.name[:3].isdigit() for path in modules)
    assert all(path.is_file() and path.suffix == ".js" for path in modules)


def test_every_session_module_fits_the_coarse_module_budget():
    production_files = sessions_part_paths()

    for path in production_files:
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        assert line_count <= 1400, f"{path.relative_to(REPO_ROOT)} has {line_count} lines"


def test_sessions_modules_are_individually_parseable_and_entrypoint_typechecks():
    for path in sessions_part_paths():
        source = path.read_text(encoding="utf-8")
        assert source.endswith("\n"), f"{path.name} must own its concatenation separator"
        subprocess.run(["node", "--check", str(path)], check=True, capture_output=True, text=True)

    subprocess.run(
        ["deno", "check", str(SESSIONS_PARTS_DIR / "index.js")],
        check=True,
        capture_output=True,
        text=True,
    )


def test_sidebar_motion_publishes_its_consumer_api():
    """Import the real ESM owner and prove every consumer binding is callable."""
    module_url = (SESSIONS_PARTS_DIR / "sidebar-motion.js").as_uri()
    expected_exports = (
        "_captureSessionReflowPositions",
        "_makeSessionSwipeAffordance",
        "_playSessionRowsReflowFromPositions",
        "_sessionPrefersReducedMotion",
        "_waitForSessionMotion",
    )
    script = (
        f"const motion = await import({module_url!r});"
        f"for (const name of {list(expected_exports)!r}) {{"
        "if (typeof motion[name] !== 'function') throw new Error(`missing motion export: ${name}`);"
        "}"
    )
    subprocess.run(
        ["deno", "eval", script],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def test_session_owner_import_graph_is_acyclic():
    modules = {path.name: path for path in sessions_part_paths()}
    graph = {}
    for name, path in modules.items():
        source = path.read_text(encoding="utf-8")
        graph[name] = [
            dependency
            for dependency in re.findall(r"from\s+['\"]\./([^'\"]+\.js)['\"]", source)
            if dependency in modules
        ]

    visited = set()
    active = []

    def visit(name):
        if name in active:
            cycle = " -> ".join((*active[active.index(name) :], name))
            raise AssertionError(f"session owner import cycle: {cycle}")
        if name in visited:
            return
        active.append(name)
        for dependency in graph[name]:
            visit(dependency)
        active.pop()
        visited.add(name)

    for name in modules:
        visit(name)


def test_sessions_modules_publish_semantic_interfaces_and_one_legacy_seam():
    source = "".join(
        path.read_text(encoding="utf-8") for path in sessions_part_paths()
    )

    ordered_sentinels = [
        "async function newSession(",
        "async function loadSession(",
        "async function _ensureMessagesLoaded(",
        "async function renderSessionList(",
        "function _resolveSessionIdFromSidebarLineage(",
        "function _renderOneSession(",
        "function renderSessionListFromCache(",
        "async function deleteSession(",
        "function navigateSession(",
    ]
    positions = [source.index(sentinel) for sentinel in ordered_sentinels]
    assert positions == sorted(positions)

    expected_modules = {
        "composerDrafts",
        "sessionState",
        "sessionRuntime",
        "sessionUnread",
        "sessionVisits",
        "sessionLifecycle",
        "sessionMessages",
        "messageTimeline",
        "sidebarControls",
        "listUpdates",
        "sessionDiscovery",
        "sidebarRowBehavior",
        "sidebarRendering",
        "sessionManagement",
    }
    for module in expected_modules:
        assert f"export const {module}=Object.freeze(" in source

    internal_paths = [
        path for path in sessions_part_paths()
        if path.name != "index.js"
    ]
    for path in internal_paths:
        module_source = path.read_text(encoding="utf-8")
        assert "HermesSessions" not in module_source
        assert "window.HermesSessions" not in module_source

    entrypoint = (SESSIONS_PARTS_DIR / "index.js").read_text(encoding="utf-8")
    compatibility = (SESSIONS_PARTS_DIR.parent / "compatibility.js").read_text(encoding="utf-8")
    assert "publishCompatibilityDomain('sessions'" in entrypoint
    assert "from '../compatibility.js'" in entrypoint
    assert "Object.defineProperty(globalThis" in compatibility
    assert not (SESSIONS_PARTS_DIR / "legacy-adapter.js").exists()


def test_oversized_list_renderer_was_deepened_without_changing_call_order():
    source = read_sessions_source()

    fork_gesture = source.index("function _installForkChildSwipe(")
    row_gesture = source.index("function _installSessionRowGestures(")
    row_render = source.index("function _renderOneSession(")
    list_render = source.index("function renderSessionListFromCache(")
    assert fork_gesture < row_gesture < row_render < list_render

    row_body = source[row_render:list_render]
    assert "_installForkChildSwipe(row, child, actions, committedSwipeDuration, committedSwipeReflowDelay);" in row_body
    assert "_installSessionRowGestures(el, s, actions, readOnly, committedSwipeDuration, committedSwipeReflowDelay, startRename);" in row_body
    assert "_renderOneSession(s, Boolean(g.isPinned), rowRenderContext)" in source[list_render:]
