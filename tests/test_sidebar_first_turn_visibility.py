"""Regressions for first-turn sessions appearing in the sidebar immediately."""
from tests.frontend_asset_contract import family_source

import pathlib

REPO = pathlib.Path(__file__).parent.parent


def read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


class TestSidebarFirstTurnVisibility:
    def test_messages_send_optimistically_upserts_active_sidebar_row(self):
        src = family_source("messages")
        assert "upsertActiveSessionForLocalTurn" in src, (
            "send() must optimistically upsert the active session into the sidebar "
            "as soon as the local user message is pushed."
        )
        push_idx = src.index("S.messages.push(userMsg);")
        helper_idx = src.index("upsertActiveSessionForLocalTurn", push_idx)
        start_idx = src.index("const startData=await _chatAdmission.promise", push_idx)
        assert helper_idx < start_idx, (
            "The sidebar row must be rendered before /api/chat/start returns so "
            "tool calls are reachable while the first agent turn is still running."
        )
        pre_start = src[helper_idx:start_idx]
        assert "renderSessionList();" not in pre_start, (
            "Do not re-fetch /api/sessions before /api/chat/start saves pending state; "
            "that race can overwrite the optimistic first-turn row with an empty list."
        )

    def test_messages_send_renders_pending_assistant_placeholder_before_chat_start(self):
        messages = family_source("messages")
        ui = family_source("ui")
        send_start = messages.index("async function send()")
        push_idx = messages.index("appendThinking('',{pending:true})", send_start)
        start_idx = messages.index("const startData=await _chatAdmission.promise", send_start)
        assert push_idx < start_idx, (
            "send() should render an assistant-side pending placeholder before "
            "/api/chat/start or the first SSE event returns."
        )
        append_start = ui.index("function appendThinking(text='', options)")
        append_end = ui.index("function updateThinking", append_start)
        append_body = ui[append_start:append_end]
        assert "allowPendingPlaceholder" in append_body
        assert "options.pending===true" in append_body.replace(" ", ""), (
            "appendThinking() must allow the explicit pre-stream placeholder path "
            "without weakening the stale-stream guard for ordinary SSE updates."
        )
        assert "(!S.activeStreamId&&!allowPendingPlaceholder)" in append_body.replace(" ", "")

    def test_sessions_js_has_local_turn_upsert_helper(self):
        src = family_source("sessions")
        assert "function upsertActiveSessionForLocalTurn" in src
        start = src.index("function upsertActiveSessionForLocalTurn")
        end = src.index("function renderSessionListFromCache", start)
        body = src[start:end]
        assert "_allSessions.unshift" in body or "_allSessions.splice" in body, (
            "Helper must add a missing active session to the cached sidebar list."
        )
        assert "S.session.message_count" in body and "S.messages.length" in body, (
            "Helper must treat the locally pushed user message as a real sidebar message."
        )
        assert "is_streaming:true" in body.replace(" ", ""), (
            "Optimistic row should render as streaming until the backend reconciles."
        )

    def test_messages_comments_document_why_each_optimistic_upsert_stays_separate(self):
        src = family_source("messages")
        assert "First optimistic pass" in src and "persists pending state" in src
        assert "Second optimistic pass" in src and "provisional title" in src
        assert "Third optimistic pass" in src and "stream_id is now known" in src

    def test_chat_start_failure_clears_optimistic_streaming_state(self):
        messages = family_source("messages")
        catch_start = messages.index("}catch(e){", messages.index("const startData=await _chatAdmission.promise"))
        failure_start = messages.index("S.messages.push({role:'assistant',content:`**Error:** ${errMsg}`});", catch_start)
        catch_body = messages[failure_start:messages.index("return;", failure_start)]
        assert "setBusy(false)" in catch_body, "chat/start failure must leave the active pane idle"
        assert "clearOptimisticSessionStreaming(activeSid)" in catch_body, (
            "If /api/chat/start fails after the optimistic sidebar upsert, the cached row "
            "must drop its streaming spinner immediately instead of waiting for polling."
        )
        assert "void renderSessionList()" in catch_body, (
            "After clearing the optimistic spinner locally, fetch /api/sessions to reconcile "
            "with whatever the server persisted before failing."
        )

        sessions = family_source("sessions")
        assert "function clearOptimisticSessionStreaming" in sessions
        clear_start = sessions.index("function clearOptimisticSessionStreaming")
        clear_end = sessions.index("function renderSessionListFromCache", clear_start)
        clear_body = sessions[clear_start:clear_end]
        assert "is_streaming:false" in clear_body.replace(" ", "")
        assert "active_stream_id:null" in clear_body.replace(" ", "")
        assert "_sessionStreamingById.set(sid,false)" in clear_body.replace(" ", "")

    def test_backend_compact_counts_pending_first_turn_as_visible(self, tmp_path):
        from api.sessions.records import Session

        session = Session(
            session_id="pending-first-turn",
            workspace=tmp_path,
            pending_user_message="hello",
            pending_started_at=1234,
        )

        compact = session.compact()
        assert compact["has_pending_user_message"] is True
        assert compact["message_count"] == 1
        assert compact["last_message_at"] == 1234

    def test_backend_index_filter_keeps_pending_first_turn_sessions(self, monkeypatch, tmp_path):
        import json
        import api.sessions.records as records
        import api.sessions.sidebar as sidebar

        sid = "pending-index-turn"
        index_file = tmp_path / "_index.json"
        # Session records own the durable sidecar and compact projection;
        # sidebar owns filtering of the indexed listing. Patch both real owners
        # so this exercises the same handoff as /api/sessions, not store.py's
        # compatibility re-exports.
        monkeypatch.setattr(records, "SESSION_DIR", tmp_path)
        monkeypatch.setattr(records, "SESSION_INDEX_FILE", index_file)
        monkeypatch.setattr(records, "SESSIONS", {})
        monkeypatch.setattr(sidebar, "SESSION_DIR", tmp_path)
        monkeypatch.setattr(sidebar, "SESSION_INDEX_FILE", index_file)
        monkeypatch.setattr(sidebar, "SESSIONS", {})
        sidecar = records.Session(
            session_id=sid,
            workspace=tmp_path,
            pending_user_message="hello",
            pending_started_at=1234,
        )
        sidecar.save()
        row = sidecar.compact()
        row["message_count"] = 0
        index_file.write_text(json.dumps([row]), encoding="utf-8")
        monkeypatch.setattr(sidebar, "_stale_snapshot_metadata_refresh_ids", lambda _rows: set())

        result = sidebar.all_sessions(include_lineage_metadata=False)

        assert [item["session_id"] for item in result] == [sid]

    def test_session_refresh_preserves_optimistic_first_turn_rows_when_server_lags(self):
        src = family_source("sessions")
        assert "function _mergeOptimisticFirstTurnSessions" in src, (
            "renderSessionList() must merge locally optimistically inserted first-turn rows "
            "back into the fetched /api/sessions result. A session switch can re-fetch before "
            "the server has saved pending state, and replacing _allSessions would hide the "
            "new in-flight chat until the stream finishes."
        )
        apply_start = src.index("function _applySessionListPayload")
        apply_end = src.index("async function renderSessionList", apply_start)
        apply_body = src[apply_start:apply_end]
        assign_idx = apply_body.index("_allSessions =")
        assert "_mergeOptimisticFirstTurnSessions" in apply_body[:assign_idx + 160], (
            "The fetched session list should be merged with optimistic rows at the assignment "
            "site, before completion transitions or renderSessionListFromCache() run."
        )
