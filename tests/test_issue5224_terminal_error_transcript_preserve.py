"""Regression tests for #5224: preserve terminal-visible transcript on recovery."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent.resolve()
STREAM_TRANSCRIPT_JS = REPO_ROOT / "static" / "modules" / "messages" / "stream-transcript.js"
SESSION_RECOVERY_JS = REPO_ROOT / "static" / "modules" / "messages" / "session-recovery.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is required to execute message runtime tests")


_DRIVER = r"""
import { pathToFileURL } from 'node:url';

const scenario = JSON.parse(process.argv[2] || '{}');
globalThis.document = {
  addEventListener() {},
  getElementById() { return null; },
  baseURI: 'http://localhost/',
};
globalThis.window = globalThis;
globalThis.window.addEventListener = () => {};
globalThis.location = { href: 'http://localhost/' };

const { createStreamTranscriptProjection } = await import(pathToFileURL(process.argv[3]).href);
const { createStreamSessionRecovery } = await import(pathToFileURL(process.argv[4]).href);

const activeSid = scenario.activeSid || 'session-5224';
const streamId = scenario.streamId || 'stream-5224';
const calls = [];
globalThis.S = JSON.parse(JSON.stringify(scenario.state || {}));
if (!S.session) S.session = { session_id: activeSid };
if (!Object.prototype.hasOwnProperty.call(S, 'activeStreamId')) S.activeStreamId = streamId;
globalThis._queueDrainSid = null;
globalThis._messageRenderWindowSize = 20;
globalThis._messageRenderableMessageCount = () => 50;
globalThis._currentMessageRenderWindowSize = () => 12;
globalThis.finalizeThinkingCard = () => calls.push('finalizeThinkingCard');
globalThis.clearLiveToolCards = () => calls.push('clearLiveToolCards');
globalThis.removeThinking = () => calls.push('removeThinking');
globalThis.renderMessages = () => calls.push('renderMessages');
globalThis.renderSessionList = () => calls.push('renderSessionList');
globalThis.syncTopbar = () => calls.push('syncTopbar');
globalThis._markSessionCompletionUnread = () => calls.push('markSessionCompletionUnread');
globalThis._hydrateTodosFromSession = () => calls.push('hydrateTodos');
globalThis._setActiveSessionUrl = () => calls.push('setActiveSessionUrl');
globalThis.showToast = () => calls.push('showToast');
globalThis.trackBackgroundError = () => calls.push('trackBackgroundError');
globalThis._isMessagePaneNearBottom = () => true;
globalThis._isMessageReaderUnpinned = () => false;
globalThis.scrollToBottom = () => calls.push('scrollToBottom');
globalThis.localStorage = {
  setItem: () => calls.push('setLocalStorageItem'),
  getItem: () => null,
  removeItem: () => calls.push('removeLocalStorageItem'),
};

const projection = createStreamTranscriptProjection();
const recovery = createStreamSessionRecovery({
  sessionId: activeSid,
  streamId,
  state: S,
  request: async () => scenario.apiPayload || { session: null },
  terminalState: { streamFinalized: false },
  turn: {
    assistantText: () => false,
    isActiveSession: () => scenario.isActiveSession !== false,
  },
  lifecycle: {
    cancelPersist: () => calls.push('cancelPersist'),
    cancelSnapshot: () => calls.push('cancelSnapshot'),
    clearStreamEndRecovery: () => calls.push('clearStreamEndRecovery'),
    clearOwnerInflight: () => calls.push('clearOwnerInflight'),
    closeSource: () => calls.push('closeSource'),
    clearApproval: () => calls.push('clearApproval'),
    clearClarify: () => calls.push('clearClarify'),
    setActivePaneIdle: () => calls.push('setActivePaneIdle'),
  },
  renderer: {
    cancelPendingRender: () => calls.push('cancelPendingRender'),
    cleanupReduceMotion: () => calls.push('cleanupReduceMotion'),
    clearAnchorProseIncrementalNode: () => calls.push('clearAnchorProse'),
    endParser: () => calls.push('endParser'),
  },
  anchor: {
    apply: () => calls.push('applyToAnchor'),
    attachProjectedScene: () => calls.push('attachProjectedScene'),
    flushReasoning: () => calls.push('flushReasoning'),
    scheduleCleanup: () => calls.push('scheduleAnchorCleanup'),
  },
  transcript: {
    carryForward: projection.carryForwardEphemeralTurnFields,
    ensureSingleTerminalStreamErrorMarker: projection.ensureSingleTerminalStreamErrorMarker,
    filterRecoveryControls: projection.filterRecoveryControlMessages,
    isTerminalStreamErrorMarker: projection.isTerminalStreamErrorMarkerMessage,
    messageIdentityKey: projection.messageIdentityKey,
    replaceMarkerOnly: projection.replaceMarkerOnlyAssistantWithStreamError,
  },
  tools: { mergeSettledWithLive: () => [] },
});

if (scenario.action === 'restore') {
  const status = await recovery.restoreSettledSession({}, {
    status: true,
    preserveVisibleOnShorterTerminalSnapshot: true,
  });
  const messages = Array.isArray(S.messages) ? S.messages : [];
  console.log(JSON.stringify({
    status,
    messages: messages.map(({ role, content }) => ({ role, content })),
    terminalMarkerCount: messages.filter(projection.isTerminalStreamErrorMarkerMessage).length,
    calls,
  }));
} else if (scenario.action === 'connection_lost') {
  recovery.handleConnectionLost({});
  const messages = Array.isArray(S.messages) ? S.messages : [];
  console.log(JSON.stringify({
    messages: messages.map(({ role, content }) => ({ role, content })),
    terminalMarkerCount: messages.filter(projection.isTerminalStreamErrorMarkerMessage).length,
    calls,
  }));
} else {
  throw new Error(`unknown action: ${scenario.action}`);
}
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    driver = tmp_path_factory.mktemp("issue5224_driver") / "driver.mjs"
    driver.write_text(_DRIVER, encoding="utf-8")
    return str(driver)


def _run_scenario(driver_path: str, scenario: dict) -> dict:
    result = subprocess.run(
        [NODE, driver_path, json.dumps(scenario), str(STREAM_TRANSCRIPT_JS), str(SESSION_RECOVERY_JS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return json.loads(result.stdout.strip())


def _restore(driver_path, current_messages, settled_messages):
    return _run_scenario(driver_path, {
        "action": "restore",
        "state": {
            "session": {"session_id": "session-5224", "message_count": len(current_messages)},
            "messages": current_messages,
            "activeStreamId": "stream-5224",
        },
        "apiPayload": {
            "session": {
                "session_id": "session-5224",
                "active_stream_id": None,
                "pending_user_message": None,
                "messages": settled_messages,
            },
        },
    })


def _pairs(outcome):
    return [(message["role"], message["content"]) for message in outcome["messages"]]


TERMINAL_MARKER = "**Connection interrupted:** The browser lost the live SSE connection before the response finished."


def test_terminal_error_restore_preserves_visible_transcript_when_server_snapshot_shorter(driver_path):
    outcome = _restore(driver_path, [
        {"role": "user", "content": "Question about data?", "_ts": "u1"},
        {"role": "assistant", "content": "Visible assistant answer segment one", "_ts": "a1"},
        {"role": "assistant", "content": "Visible assistant answer segment two", "_ts": "a2"},
        {"role": "assistant", "content": TERMINAL_MARKER, "_ts": "err"},
    ], [
        {"role": "user", "content": "Question about data?", "_ts": "u1"},
        {"role": "assistant", "content": "Visible assistant answer segment one", "_ts": "a1"},
    ])

    assert outcome["status"] == "restored"
    assert outcome["terminalMarkerCount"] == 1
    assert ("assistant", TERMINAL_MARKER) in _pairs(outcome)
    assert any("Visible assistant answer segment two" in content for _, content in _pairs(outcome))


def test_terminal_error_restore_replaces_when_snapshots_are_fuller(driver_path):
    settled = [
        {"role": "user", "content": "Question about data?", "_ts": "u1"},
        {"role": "assistant", "content": "Settled first segment", "_ts": "a1"},
        {"role": "assistant", "content": "Settled second segment", "_ts": "a2"},
        {"role": "assistant", "content": "Settled third segment", "_ts": "a3"},
    ]
    outcome = _restore(driver_path, [
        {"role": "assistant", "content": "Old interrupted shell", "_ts": "old"},
    ], settled)

    assert outcome["status"] == "restored"
    assert _pairs(outcome) == [(message["role"], message["content"]) for message in settled]


def test_terminal_error_restore_replaces_shorter_authoritative_snapshot_when_prefix_does_not_match(driver_path):
    outcome = _restore(driver_path, [
        {"role": "user", "content": "Question about data?", "_ts": "u1"},
        {"role": "assistant", "content": "Visible assistant fragment one", "_ts": "a1"},
        {"role": "assistant", "content": "Visible assistant fragment two", "_ts": "a2"},
        {"role": "assistant", "content": TERMINAL_MARKER, "_ts": "err"},
    ], [
        {"role": "user", "content": "Question about data?", "_ts": "u1"},
        {"role": "assistant", "content": "Settled final answer", "_ts": "final"},
    ])

    assert _pairs(outcome) == [
        ("user", "Question about data?"),
        ("assistant", "Settled final answer"),
    ]
    assert outcome["terminalMarkerCount"] == 0


def test_terminal_error_restore_does_not_preserve_from_historical_marker_on_older_turn(driver_path):
    historical = [
        {"role": "user", "content": "Earlier question", "_ts": "u0"},
        {"role": "assistant", "content": "Earlier answer", "_ts": "a0"},
        {"role": "assistant", "content": TERMINAL_MARKER, "_ts": "err0"},
        {"role": "user", "content": "Current question", "_ts": "u1"},
        {"role": "assistant", "content": "Current fragment one", "_ts": "a1"},
    ]
    outcome = _restore(driver_path, historical + [
        {"role": "assistant", "content": "Current fragment two", "_ts": "a2"},
    ], historical)

    assert _pairs(outcome) == [(message["role"], message["content"]) for message in historical]
    assert outcome["terminalMarkerCount"] == 1


def test_terminal_error_marker_is_single_instance_and_not_duplicated(driver_path):
    outcome = _run_scenario(driver_path, {
        "action": "connection_lost",
        "state": {
            "session": {"session_id": "session-5224"},
            "messages": [
                {"role": "user", "content": "Question", "_ts": "u1"},
                {"role": "assistant", "content": "Answer", "_ts": "a1"},
                {"role": "assistant", "content": TERMINAL_MARKER, "_ts": "err"},
                {"role": "assistant", "content": TERMINAL_MARKER, "_ts": "err2"},
            ],
            "activeStreamId": "stream-5224",
        },
    })

    assert outcome["terminalMarkerCount"] == 1
    assert outcome["messages"][-1]["content"].startswith(TERMINAL_MARKER)
    assert "clearStreamEndRecovery" in outcome["calls"]
