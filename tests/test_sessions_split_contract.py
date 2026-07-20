from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tests.test_sessions_split_support import (
    REPO_ROOT,
    SESSIONS_MANIFEST,
    read_sessions_source,
    sessions_part_paths,
)


def test_sessions_manifest_is_a_strict_ordered_relative_file_list():
    manifest = json.loads(SESSIONS_MANIFEST.read_text(encoding="utf-8"))

    assert set(manifest) == {"version", "entrypoint", "modules"}
    assert manifest["version"] == 2
    assert manifest["entrypoint"] == "index.js"
    assert 10 <= len(manifest["modules"]) <= 14
    assert len(manifest["modules"]) == len(set(manifest["modules"]))
    assert manifest["modules"][-1] == manifest["entrypoint"]
    assert not any(name[:3].isdigit() for name in manifest["modules"])
    for name in manifest["modules"]:
        assert Path(name).name == name
        assert name.endswith(".js")
        assert (SESSIONS_MANIFEST.parent / name).is_file()


def test_every_session_module_fits_the_coarse_module_budget():
    production_files = [SESSIONS_MANIFEST, *sessions_part_paths()]

    for path in production_files:
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        assert line_count <= 1400, f"{path.relative_to(REPO_ROOT)} has {line_count} lines"


def test_sessions_modules_are_individually_parseable_and_entrypoint_typechecks():
    for path in sessions_part_paths():
        source = path.read_text(encoding="utf-8")
        assert source.endswith("\n"), f"{path.name} must own its concatenation separator"
        subprocess.run(["node", "--check", str(path)], check=True, capture_output=True, text=True)

    subprocess.run(
        ["deno", "check", str(SESSIONS_MANIFEST.parent / "index.js")],
        check=True,
        capture_output=True,
        text=True,
    )


def test_sessions_modules_publish_semantic_interfaces_and_one_legacy_seam():
    source = read_sessions_source()

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
        "sessionState",
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
        if path.name not in {"index.js", "legacy-adapter.js"}
    ]
    for path in internal_paths:
        module_source = path.read_text(encoding="utf-8")
        assert "HermesSessions" not in module_source
        assert "window.HermesSessions" not in module_source

    entrypoint = (SESSIONS_MANIFEST.parent / "index.js").read_text(encoding="utf-8")
    adapter = (SESSIONS_MANIFEST.parent / "legacy-adapter.js").read_text(encoding="utf-8")
    assert "installLegacySessionGlobals" in entrypoint
    assert "Object.defineProperty(root,'HermesSessions'" in adapter


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
