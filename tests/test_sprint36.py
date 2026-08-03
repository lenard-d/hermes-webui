"""
Sprint 36 Tests: cancelStream cleanup no longer depends on SSE event (PR #309 / issue #299).

The old cancelStream() set "Cancelling..." status and then relied on the SSE cancel
event to clear it. If the SSE connection was already closed, the event never arrived
and "Cancelling..." lingered indefinitely.

The fix: cancelStream() now clears status, busy state, and activeStreamId directly after
the cancel API request completes — regardless of whether the SSE cancel event fires.
The SSE handler still runs if it arrives (all operations idempotent).

Covers:
  1. cancelStream() clears activeStreamId unconditionally after the fetch
  2. cancelStream() calls setBusy(false) unconditionally
  3. cancelStream() calls setStatus('') / setComposerStatus('') unconditionally
  4. cancelStream() clears composer status text unconditionally
  5. The catch block no longer calls setStatus(cancel_failed) — cleanup runs even on error
  6. The SSE cancel handler is still present (idempotent path)
  7. cancel_failed i18n key is still defined in all locales (key exists, just not used in
     the catch-path anymore — kept for potential future use)
"""
from tests.frontend_asset_contract import family_source

import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def _locale_count(src: str) -> int:
    pattern = re.compile(
        r"^\s{2}(?:'(?P<quoted>[A-Za-z0-9-]+)'|(?P<plain>[A-Za-z0-9-]+))\s*:\s*\{",
        re.MULTILINE,
    )
    return sum(1 for _ in pattern.finditer(src))


# ── 1–4. cancelStream() cleanup is unconditional ─────────────────────────────

class TestCancelStreamCleanup:
    """cancelStream() must clear all busy state regardless of SSE connection state."""

    def _get_cancel_block(self):
        """Extract the cancelStream function body from boot.js."""
        src = family_source("boot")
        # Signature-tolerant: cancelStream now takes a `reason` param (#5345), so
        # match the declaration regardless of its parameter list.
        m = re.search(r"async function cancelStream\s*\(", src)
        assert m is not None, "cancelStream not found in boot.js"
        idx = m.start()
        # Find the closing brace — scan for the matching }
        depth = 0
        end = idx
        for i, ch in enumerate(src[idx:]):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end = idx + i + 1
                    break
        return src[idx:end]

    def test_clears_active_stream_id(self):
        """cancelStream() must null out S.activeStreamId after the request."""
        block = self._get_cancel_block()
        assert "S.activeStreamId=null" in block or "S.activeStreamId = null" in block, (
            "cancelStream() does not clear S.activeStreamId — "
            "subsequent calls could re-cancel an already-finished stream"
        )

    def test_calls_set_busy_false(self):
        """cancelStream() must call setBusy(false) directly."""
        block = self._get_cancel_block()
        assert "setBusy(false)" in block, (
            "cancelStream() does not call setBusy(false) — "
            "spinner may linger if SSE connection is already closed"
        )

    def test_calls_set_status_empty(self):
        """cancelStream() must call setStatus('') to clear 'Cancelling...' text."""
        block = self._get_cancel_block()
        assert "setStatus('')" in block or 'setStatus("")' in block, (
            "cancelStream() does not clear status text — "
            "'Cancelling...' can linger if SSE cancel event never arrives"
        )

    def test_clears_composer_status(self):
        """cancelStream() must clear the composer status text unconditionally."""
        block = self._get_cancel_block()
        assert "setComposerStatus" in block or "setStatus" in block, (
            "cancelStream() does not clear composer/status text — "
            "'Cancelling…' or stale status can linger if SSE cancel event never arrives"
        )

    def test_cleanup_not_inside_try_block(self):
        """Cleanup must happen outside the try block so it runs even if fetch fails."""
        block = self._get_cancel_block()
        # The S.activeStreamId=null and setBusy(false) must appear after the try/catch
        # Verify they are NOT only inside the try block by checking position relative to catch
        catch_idx = block.find("}catch(")
        cleanup_idx = block.find("S.activeStreamId=null")
        if cleanup_idx == -1:
            cleanup_idx = block.find("S.activeStreamId = null")
        assert cleanup_idx > catch_idx, (
            "S.activeStreamId cleanup appears to be inside the try block — "
            "it won't run if the fetch throws"
        )


# ── 5. Error path behavior ────────────────────────────────────────────────────

class TestCancelStreamErrorPath:
    """The catch block should not prevent cleanup from running."""

    def test_catch_block_does_not_call_set_status_cancel_failed(self):
        """The catch block must not call setStatus(cancel_failed) on its own.

        Previously: catch(e){setStatus(t('cancel_failed')+e.message)}
        After fix: catch swallows the error; cleanup runs in the outer scope.
        The status is cleared by setStatus('') unconditionally.
        """
        src = family_source("boot")
        # Signature-tolerant match (cancelStream now takes a `reason` param, #5345).
        m = re.search(r"async function cancelStream\s*\(", src)
        assert m is not None, "cancelStream not found in boot.js"
        idx = m.start()
        # Widen the window: the provenance log + comments added for #5345 sit
        # before the try/catch, so 400 chars no longer reaches the catch block.
        block = src[idx:idx + 1200]
        # The old pattern was setStatus inside catch; new pattern has it outside
        # Look for the catch block specifically
        catch_idx = block.find("}catch(")
        if catch_idx == -1:
            catch_idx = block.find("} catch (")
        assert catch_idx != -1, "No catch block found in cancelStream"
        # Get just the catch body
        brace_open = block.find("{", catch_idx)
        brace_close = block.find("}", brace_open)
        catch_body = block[brace_open:brace_close + 1]
        assert "cancel_failed" not in catch_body, (
            "catch block still calls setStatus(cancel_failed) — "
            "this means a failed cancel shows an error instead of cleaning up silently"
        )


# ── 6. SSE cancel terminal-owner behavior ───────────────────────────────────

@pytest.mark.skipif(NODE is None, reason="node is required to execute message runtime tests")
def test_sse_cancel_terminal_owner_idles_the_pane():
    """A cancel SSE frame finalizes its owner and invokes the pane-idle transition."""
    script = r"""
globalThis.document={
  addEventListener(){},
  getElementById(){return null;},
  baseURI:'http://localhost/',
};
globalThis.window=globalThis;
globalThis.window.addEventListener=()=>{};
globalThis.location={href:'http://localhost/'};
globalThis.renderSessionList=()=>{};
globalThis.clearLiveToolCards=()=>{};
globalThis.removeThinking=()=>{};
globalThis.renderMessages=()=>{};
globalThis._setSessionViewedCount=()=>{};
globalThis._isMessagePaneNearBottom=()=>true;
globalThis._isMessageReaderUnpinned=()=>false;

const {createStreamTerminalEventOwner}=await import('./static/modules/messages/terminal-events.js');

class FakeSource {
  constructor(){this.listeners=new Map();this.readyState=1;this.closed=false;}
  addEventListener(name,handler){this.listeners.set(name,handler);}
  close(){this.closed=true;this.readyState=2;}
  emit(name,payload){
    const handler=this.listeners.get(name);
    if(!handler) throw new Error(`missing ${name} handler`);
    handler({data:JSON.stringify(payload)});
  }
}

const source=new FakeSource();
const calls=[];
const terminalState={streamFinalized:false,terminalStateReached:false};
const state={
  session:{session_id:'sid-cancel',message_count:0},
  messages:[],
  activeStreamId:'stream-cancel',
};
globalThis.S=state;
const owner=createStreamTerminalEventOwner({
  sessionId:'sid-cancel',
  streamId:'stream-cancel',
  state,
  terminalState,
  turn:{assistantText:()=>''},
  lifecycle:{
    clearStreamEndRecovery:()=>calls.push('clearRecovery'),
    bailOutOfStaleTerminal:()=>false,
    cancelPersist:()=>calls.push('cancelPersist'),
    cancelSnapshot:()=>calls.push('cancelSnapshot'),
    clearOwnerInflight:()=>calls.push('clearInflight'),
    clearApproval:()=>calls.push('clearApproval'),
    clearClarify:()=>calls.push('clearClarify'),
    setActivePaneIdle:()=>calls.push('idlePane'),
  },
  renderer:{
    clearAnchorProseIncrementalNode:()=>calls.push('clearAnchorProse'),
    cancelPendingRender:()=>calls.push('cancelRender'),
    cleanupReduceMotion:()=>calls.push('cleanupMotion'),
    endParser:()=>calls.push('endParser'),
  },
  anchor:{
    attachProjectedScene:()=>calls.push('attachProjectedScene'),
    flushReasoning:()=>calls.push('flushReasoning'),
    apply:()=>calls.push('applyCancel'),
    scheduleCleanup:()=>calls.push('scheduleCleanup'),
  },
  transcript:{
    carryForward:(_previous,next)=>next,
    filterRecoveryControls:messages=>messages,
  },
});
owner.attach(source);
source.emit('cancel',{session:{session_id:'sid-cancel',message_count:0,messages:[]}});
await new Promise(resolve=>setTimeout(resolve,0));

if(!source.closed) throw new Error('cancel did not close the owner source');
if(!terminalState.streamFinalized||!terminalState.terminalStateReached){
  throw new Error('cancel did not finalize terminal state');
}
if(state.activeStreamId!==null) throw new Error('cancel did not clear active stream id');
if(calls.at(-1)!=='idlePane') throw new Error(`cancel did not idle pane: ${calls.join(',')}`);
"""
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


# ── 7. i18n key preserved ─────────────────────────────────────────────────────

def test_cancel_failed_i18n_key_exists_in_all_locales():
    """cancel_failed key must still exist in i18n.js for all locales."""
    src = family_source("i18n")
    # Should appear once per locale (en, es, de, ru, zh, zh-Hant)
    locale_count = _locale_count(src)
    count = src.count("cancel_failed:")
    assert count >= locale_count, (
        f"cancel_failed key only found {count} times in i18n.js — "
        f"expected at least {locale_count} (one per locale)"
    )


# ── 8. Server-persisted cancel marker doesn't leak into agent history ────────

def test_cancel_marker_flagged_as_error_to_skip_in_api_history():
    """The server-side cancel marker appended in cancel_stream() must carry
    _error: True so _sanitize_messages_for_api() strips it from the
    conversation_history sent to the agent on the next user message.

    Without this flag, the LLM sees "Task cancelled" as a prior assistant
    turn and may reference it in subsequent responses ("As I mentioned, I was
    cancelled...") — a behavioral regression introduced when this PR started
    persisting the marker to the session.
    """
    from types import SimpleNamespace

    from api.runs.terminal_outcomes import _persist_cancelled_turn

    session = SimpleNamespace(
        active_stream_id="stream-1",
        messages=[],
        pending_attachments=[],
        pending_started_at=None,
        pending_user_message=None,
        pending_user_source=None,
        profile="test",
    )

    _persist_cancelled_turn(session)

    assert session.messages[-1]["_error"] is True, (
        "cancel marker is missing _error: True — it will leak into the agent's "
        "conversation_history via _sanitize_messages_for_api() on the next turn. "
        "The API-message sanitizer must keep filtering persisted error markers."
    )


def test_sanitize_strips_error_flagged_assistant_messages():
    """_sanitize_messages_for_api() must drop messages with _error: True —
    this is the invariant the cancel marker's _error flag relies on."""
    from api.runs.message_sanitization import _sanitize_messages_for_api
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "assistant", "content": "*Task cancelled.*", "_error": True},
        {"role": "user", "content": "next"},
    ]
    sanitized = _sanitize_messages_for_api(messages)
    assert len(sanitized) == 3, (
        f"expected 3 messages (cancel marker stripped), got {len(sanitized)}: {sanitized}"
    )
    assert all("Task cancelled" not in (m.get("content") or "") for m in sanitized), (
        "_sanitize_messages_for_api must filter cancel markers from API history"
    )
