"""Regression guards for Anchor-owned live progress echo cleanup."""

from __future__ import annotations
from tests.frontend_asset_contract import family_source

import json
import pathlib
import re
import shutil
import subprocess

import pytest


REPO = pathlib.Path(__file__).resolve().parent.parent
MESSAGES = family_source("messages")
UI = family_source("ui")
NODE = shutil.which("node")


def _run_node_module_script(script: str) -> dict:
    """Execute a native ESM owner through its real import graph."""
    assert NODE, "node is required for native-module live-event tests"
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _interim_listener_body() -> str:
    match = re.search(
        r"source\.addEventListener\('interim_assistant'\s*,\s*(?:e|ev|event)\s*=>\s*\{(.*?)\n\s*\}\);",
        MESSAGES,
        re.DOTALL,
    )
    assert match, "interim_assistant listener not found"
    return match.group(1)


def test_interim_reasoning_echo_cleans_live_and_anchor_thinking():
    body = _interim_listener_body()

    assert "if(data&&data.reasoning_echo) anchor.stripReasoningEcho(visible);" in body
    assert "function _stripAnchorReasoningEcho(visible)" in MESSAGES
    assert "function _removeLiveReasoningEchoRows(visible)" in MESSAGES
    assert "events.splice(i,1);" in MESSAGES
    assert '.agent-activity-thinking[data-anchor-scene-row="1"]' in MESSAGES
    assert "_removeLiveReasoningEchoRows(visible)" in MESSAGES
    assert "reasoningText:nextReasoning" in MESSAGES
    assert "liveReasoningText:nextLiveReasoning" in MESSAGES
    assert "_writeReasoning({" in MESSAGES


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_interim_anchor_render_runs_after_legacy_segment_flush_without_duplicate_process_row():
    """The content-event owner flushes the DOM before applying one anchor row."""
    rendering_url = (REPO / "static/modules/messages/rendering.js").as_uri()
    content_events_url = (REPO / "static/modules/messages/content-events.js").as_uri()
    script = f"""
globalThis.document = {{
  addEventListener() {{}}, removeEventListener() {{}},
  querySelector() {{ return null; }}, querySelectorAll() {{ return []; }},
  getElementById() {{ return null; }},
}};
globalThis.window = {{addEventListener() {{}}, removeEventListener() {{}}, _showThinking: true}};
globalThis.renderMd = value => `<p>${{value}}</p>`;
globalThis.esc = value => String(value ?? '');
globalThis.removeThinking = () => {{}};
globalThis.setTimeout = () => 0;
globalThis.requestAnimationFrame = () => 0;

const {{ createStreamRenderer }} = await import({json.dumps(rendering_url)});
const {{ createStreamContentEventOwner }} = await import({json.dumps(content_events_url)});
const order = [];
const state = {{
  assistantText: '', liveReasoningText: '', reasoningText: '', segmentStart: 0,
  streamFinalized: false,
  assistantRow: {{setAttribute() {{}}, parentElement: null}},
  assistantBody: {{innerHTML: '', classList: {{remove() {{}}}}}},
}};
const renderer = createStreamRenderer({{
  readState: () => state,
  upsertAnchorProse: () => order.push('duplicate-process-prose'),
  syncWorklogReasons: () => order.push('legacy-segment-flushed'),
}});
const streamRenderer = {{
  ...renderer,
  ensureAssistantRow: () => state.assistantRow,
}};
const turn = {{
  isTerminal: () => false,
  setLiveReasoningText: () => {{}},
  appendInterimAssistantText: value => {{ state.assistantText = value; }},
  pushInterimSnippet: () => {{}},
  syncInflight: () => {{}},
  assistantRow: () => state.assistantRow,
  interimSnippetCount: () => 1,
  recordActivityBoundary: () => {{}},
}};
const anchor = {{apply: () => order.push('anchor-interim-applied')}};
const listeners = new Map();
const source = {{
  addEventListener(name, listener) {{ listeners.set(name, listener); }},
}};
const owner = createStreamContentEventOwner({{
  sessionId: 'session-1', streamId: 'stream-1',
  state: {{session: {{session_id: 'session-1'}}}},
  turn, renderer: streamRenderer, anchor,
}});
owner.attach(source);
listeners.get('interim_assistant')({{data: JSON.stringify({{text: 'interim prose'}})}});
process.stdout.write(JSON.stringify({{
  rendered: state.assistantBody.innerHTML,
  order,
}}));
"""
    result = _run_node_module_script(script)

    assert result["rendered"] == "<p>interim prose</p>"
    assert result["order"] == ["legacy-segment-flushed", "anchor-interim-applied"]


def test_live_anchor_scene_hides_legacy_live_assistant_sources():
    start = UI.index("function renderLiveAnchorActivityScene")
    body = UI[start : UI.index("function _renderLiveAnchorActivitySceneForStream", start)]

    assert "blocks.querySelectorAll('[data-live-assistant=\"1\"]').forEach" in body
    assert "assistant-segment-worklog-source" in body
    assert "aria-hidden" in body
