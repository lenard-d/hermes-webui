from __future__ import annotations

import json
import re
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

    assert set(manifest) == {"version", "parts"}
    assert manifest["version"] == 1
    assert manifest["parts"]
    assert 8 <= len(manifest["parts"]) <= 12
    assert len(manifest["parts"]) == len(set(manifest["parts"]))
    assert manifest["parts"] == sorted(manifest["parts"])
    for name in manifest["parts"]:
        assert Path(name).name == name
        assert name.endswith(".js")
        assert (SESSIONS_MANIFEST.parent / name).is_file()


def test_sessions_facade_and_every_production_part_fit_the_coarse_module_budget():
    production_files = [
        REPO_ROOT / "static" / "sessions.js",
        SESSIONS_MANIFEST,
        *sessions_part_paths(),
    ]

    for path in production_files:
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        assert line_count <= 1400, f"{path.relative_to(REPO_ROOT)} has {line_count} lines"


def test_sessions_parts_are_individually_parseable_and_concatenate_parseably(tmp_path):
    for path in sessions_part_paths():
        source = path.read_text(encoding="utf-8")
        assert source.endswith("\n"), f"{path.name} must own its concatenation separator"
        subprocess.run(["node", "--check", str(path)], check=True, capture_output=True, text=True)

    composed = tmp_path / "sessions-composed.js"
    composed.write_text(read_sessions_source(), encoding="utf-8")
    subprocess.run(["node", "--check", str(composed)], check=True, capture_output=True, text=True)
    subprocess.run(
        ["node", "--check", str(REPO_ROOT / "static" / "sessions.js")],
        check=True,
        capture_output=True,
        text=True,
    )


def test_sessions_composition_preserves_order_namespaces_and_compatibility_globals():
    source = read_sessions_source()

    ordered_sentinels = [
        "window.HermesSessions=window.HermesSessions||{};",
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
        assert f"window.HermesSessions.parts.{module}=Object.freeze(" in source

    # Classic-script function declarations remain the compatibility globals
    # consumed by boot.js, messages.js, panels.js, extensions and Node harnesses.
    for name in (
        "newSession",
        "loadSession",
        "renderSessionList",
        "renderSessionListFromCache",
        "deleteSession",
        "navigateSession",
    ):
        assert f"function {name}(" in source

    facade = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    part_paths = sessions_part_paths()
    facade_positions = [facade.index(f"'{path.name}'") for path in part_paths]
    assert facade_positions == sorted(facade_positions)
    load_order_match = re.search(
        r"sessions\.loadOrder=Object\.freeze\(\[(.*?)\]\);",
        facade,
        re.DOTALL,
    )
    assert load_order_match is not None
    assert re.findall(r"'([^']+\.js)'", load_order_match.group(1)) == [
        path.name for path in part_paths
    ]
    assert "Object.assign(root,sessions.api);" in facade


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
