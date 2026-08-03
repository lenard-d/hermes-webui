from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TRANSCRIPT_LOADING_JS = (ROOT / "static" / "modules" / "sessions" / "transcript-loading.js").read_text(encoding="utf-8")
OLDER_MESSAGE_PAGINATION_JS = (ROOT / "static" / "modules" / "sessions" / "older-message-pagination.js").read_text(encoding="utf-8")


def test_sessions_js_resyncs_tool_calls_after_history_window_replacement():
    """History paging replaces S.messages with a larger window.

    Legacy sessions keep tool card data in session.tool_calls, so that side data
    must be refreshed alongside the message window. Otherwise renderMessages()
    can keep stale anchors and show unloaded/thinking placeholders while the
    user scrolls through history.
    """
    assert "function _syncToolCallsForLoadedMessages(messages, sessionToolCalls)" in TRANSCRIPT_LOADING_JS
    assert "_syncToolCallsForLoadedMessages(msgs, data.session.tool_calls);" in TRANSCRIPT_LOADING_JS
    assert "S.messages = nextMessages;\n    _syncToolCallsForLoadedMessages(nextMessages, responseSession.tool_calls);" in OLDER_MESSAGE_PAGINATION_JS
    assert "S.messages = _msgsToAssign;\n    transcriptWindowState.messagesTruncated = false;\n    transcriptWindowState.oldestIdx = 0;\n    _syncToolCallsForLoadedMessages(msgs, data.session.tool_calls);" in OLDER_MESSAGE_PAGINATION_JS


def test_sessions_js_clears_session_tool_calls_when_messages_have_own_metadata():
    assert "const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;" in TRANSCRIPT_LOADING_JS
    assert "const hasTu=Array.isArray(m.content)&&m.content.some(p=>p&&p.type==='tool_use');" in TRANSCRIPT_LOADING_JS
    assert "windowOffset" not in TRANSCRIPT_LOADING_JS
    assert "copy.assistant_msg_idx=idx-offset;" not in TRANSCRIPT_LOADING_JS
    assert "S.toolCalls=sessionToolCalls.map(tc=>({...tc,done:true}));" in TRANSCRIPT_LOADING_JS
    assert "S.toolCalls=[];" in TRANSCRIPT_LOADING_JS
