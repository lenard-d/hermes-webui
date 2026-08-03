"""Regression checks for #2066 stale sidebar spinner state."""
import json
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SESSION_RUN_STATE_JS = (REPO / "static/modules/sessions/session-run-state.js").read_text(encoding="utf-8")
SIDEBAR_LIST_ORCHESTRATOR_JS = (REPO / "static/modules/sessions/sidebar-list-orchestrator.js").read_text(encoding="utf-8")


def _function_body(source: str, name: str) -> str:
    start = source.find(f"function {name}")
    assert start != -1, f"{name} not found in its owner module"
    brace = source.find("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"{name} body did not terminate")


def test_local_streaming_only_uses_active_session_busy_state():
    block = _function_body(SESSION_RUN_STATE_JS, "_isSessionLocallyStreaming")

    assert "const isActive = S.session && s.session_id === S.session.session_id;" in block
    assert "return isActive && Boolean(S.busy);" in block
    assert "INFLIGHT[s.session_id]" not in block
    assert "INFLIGHT && INFLIGHT[s.session_id]" not in block


def test_cache_render_purges_stale_non_streaming_inflight_entries():
    purge_block = _function_body(SESSION_RUN_STATE_JS, "_purgeStaleInflightEntries")
    render_block = _function_body(SIDEBAR_LIST_ORCHESTRATOR_JS, "renderSessionListFromCache")

    assert "const sessionsById = new Map();" in purge_block
    assert "if (s && s.session_id) sessionsById.set(s.session_id, s);" in purge_block
    assert "const s = sessionsById.get(sid);" in purge_block
    assert "_allSessionsById" not in purge_block
    # Non-streaming sessions that ARE in _allSessions are purged (original #2066
    # semantics).  Sessions absent from _allSessions are also purged (adds #2092
    # ghost-entry cleanup); the guard check for !sessionsById.has(sid) must come
    # before the non-streaming check for code clarity and correctness.
    assert "if (!sessionsById.has(sid))" in purge_block
    assert "!s.is_streaming" in purge_block
    assert "delete INFLIGHT[sid];" in purge_block
    assert "clearInflightState(sid);" in purge_block
    assert "_purgeStaleInflightEntries();" in render_block


def test_stale_inflight_purge_executes_without_undeclared_session_map():
    purge_block = _function_body(SESSION_RUN_STATE_JS, "_purgeStaleInflightEntries")
    script = f"""
const sidebarStateBindings = {{ _allSessions: [
  {{session_id: 'done-session', is_streaming: false}},
  {{session_id: 'running-session', is_streaming: true}}
] }};
const _sessionListSourceById = new Map();
let _sendInProgress = false;
let _sendInProgressSid = null;
let INFLIGHT = {{
  'done-session': true,
  'running-session': true,
  'unknown-session': true
}};
let cleared = [];
function clearInflightState(sid) {{
  cleared.push(sid);
}}
{purge_block}
_purgeStaleInflightEntries();
console.log(JSON.stringify({{inflight: INFLIGHT, cleared}}));
"""
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)

    # With #2092, sessions absent from _allSessions (like `unknown-session`)
    # are also purged and have clearInflightState called for them.  `done-session`
    # remains in _allSessions with is_streaming=false so it is still purged too.
    assert payload == {
        "inflight": {
            "running-session": True,
        },
        "cleared": sorted(["unknown-session", "done-session"]),
    }
