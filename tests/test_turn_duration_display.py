"""Regression tests for per-turn response duration in WebUI.

The WebUI should expose how long an agent turn took, using backend timing so
reload/reconnect does not lose the measurement.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOCAL_SUCCESS_PY = (REPO / "api" / "runs" / "local_success.py").read_text(encoding="utf-8")
TERMINAL_EVENTS_JS = (REPO / "static" / "modules" / "messages" / "terminal-events.js").read_text(encoding="utf-8")
SEND_JS = (REPO / "static" / "modules" / "messages" / "send.js").read_text(encoding="utf-8")
TURN_ADMISSION_PY = (REPO / "api" / "runs" / "admission.py").read_text(encoding="utf-8")
ACTIVITY_TIMING_JS = (REPO / "static" / "modules" / "ui" / "activity-timing.js").read_text(encoding="utf-8")
ACTIVITY_AND_SCROLL_JS = (REPO / "static" / "modules" / "ui" / "activity-and-scroll.js").read_text(encoding="utf-8")
LIVE_RUN_STATUS_JS = (REPO / "static" / "modules" / "ui" / "live-run-status.js").read_text(encoding="utf-8")
SETTLED_TURN_FINALIZATION_JS = (REPO / "static" / "modules" / "ui" / "settled-turn-finalization.js").read_text(encoding="utf-8")
SETTLED_ACTIVITY_RENDERER_JS = (REPO / "static" / "modules" / "ui" / "settled-activity-renderer.js").read_text(encoding="utf-8")
ANCHOR_SCENE_PRESENTATION_JS = (REPO / "static" / "modules" / "ui" / "anchor-scene-presentation.js").read_text(encoding="utf-8")
WORKLOG_TOOL_GROUPS_JS = (REPO / "static" / "modules" / "ui" / "worklog-tool-groups.js").read_text(encoding="utf-8")
RENDERER_JS = (REPO / "static" / "modules" / "ui" / "renderer.js").read_text(encoding="utf-8")
I18N_HELPERS_JS = (REPO / "static" / "i18n_parts" / "helpers.js").read_text(encoding="utf-8")
I18N_EN_JS = (REPO / "static" / "i18n_parts" / "locale-en.js").read_text(encoding="utf-8")
I18N_ZH_JS = (REPO / "static" / "i18n_parts" / "locale-zh.js").read_text(encoding="utf-8")
I18N_ZH_HANT_JS = (REPO / "static" / "i18n_parts" / "locale-zh_hant.js").read_text(encoding="utf-8")
ACTIVITY_CSS = (REPO / "static" / "style_parts" / "005-menus-actions-activity.css").read_text(encoding="utf-8")
TRANSCRIPT_CSS = (REPO / "static" / "style_parts" / "008-sessions-transcript.css").read_text(encoding="utf-8")


def test_streaming_done_payload_includes_backend_turn_duration():
    assert "duration_seconds" in LOCAL_SUCCESS_PY, (
        "api/runs/local_success.py should include a backend-measured duration_seconds "
        "field in the done usage payload."
    )
    assert "time.time() - float(turn_started_at)" in LOCAL_SUCCESS_PY
    assert "duration = 0.0" in LOCAL_SUCCESS_PY
    assert "_turnDuration" in LOCAL_SUCCESS_PY, (
        "The measured duration should be persisted on the assistant message so "
        "it survives reload after the SSE stream settles."
    )


def test_done_handler_persists_duration_on_last_assistant_message():
    assert "d.usage.duration_seconds" in TERMINAL_EVENTS_JS, (
        "static/messages.js should read duration_seconds from the done usage payload."
    )
    assert "lastAsst._turnDuration" in TERMINAL_EVENTS_JS, (
        "The done handler should attach the duration to the last assistant message "
        "so renderMessages() can display it after the live stream settles."
    )


def test_ui_formats_and_renders_turn_duration_in_footer_and_activity_summary():
    assert "function _formatTurnDuration" in ACTIVITY_TIMING_JS, (
        "ui.js should centralize duration formatting for footer and compact activity display."
    )
    assert "msg-duration-inline" in SETTLED_TURN_FINALIZATION_JS and "Done in" in SETTLED_TURN_FINALIZATION_JS, (
        "Expanded/non-activity display should show a subtle footer chip like 'Done in 42s'."
    )
    assert "tool-call-group-duration" in WORKLOG_TOOL_GROUPS_JS, (
        "Compact tool activity summary should have a dedicated duration span at the end of the line."
    )
    assert "data-turn-duration" in ANCHOR_SCENE_PRESENTATION_JS, (
        "The spec Activity summary needs a stable data-turn-duration hook so settled duration can update its summary."
    )
    assert "turnDuration:includeTurnDuration?_turnDurationForAnchor(anchorRow):undefined" in SETTLED_ACTIVITY_RENDERER_JS, (
        "Settled compact activity should put turn duration on the first spec Activity row, "
        "not resurrect the legacy top Run Activity."
    )
    assert "compactWorklogForMessage" in SETTLED_TURN_FINALIZATION_JS, (
        "When folded Worklog detail is present, duration should live on the Worklog row "
        "instead of being duplicated in the assistant footer."
    )
    assert ".msg-duration-inline" in TRANSCRIPT_CSS and ".tool-call-group-duration" in ACTIVITY_CSS, (
        "Duration UI should have explicit CSS hooks for the footer chip and compact activity summary."
    )


def test_active_compact_activity_elapsed_timer_uses_persisted_start_time():
    assert "response_pending_started_at = session.pending_started_at" in TURN_ADMISSION_PY
    assert '"pending_started_at": response_pending_started_at' in TURN_ADMISSION_PY, (
        "turn admission should return the persisted pending_started_at timestamp "
        "so the live timer starts from backend/session truth."
    )
    assert "startData.pending_started_at" in SEND_JS, (
        "send() should copy chat-start pending_started_at into S.session before "
        "attaching the live stream."
    )
    assert "showLiveRunStatus(activeSid,{startedAt:_startedAt});" in SEND_JS, (
        "The first chat-start path should show the bottom live footer timer as soon "
        "as stream_id and pending_started_at are known; reconnect should not be the "
        "only path that restores it."
    )
    assert "function _processedElapsedLabel" in ACTIVITY_TIMING_JS and "t('processed_elapsed',text)" in ACTIVITY_TIMING_JS, (
        "Compact Worklog should present the running timer as the stable processed-time anchor."
    )
    assert "data-turn-started-at" in ACTIVITY_AND_SCROLL_JS and "data-active-turn-elapsed" in ACTIVITY_AND_SCROLL_JS, (
        "Live compact Activity groups need stable start-time and active-elapsed "
        "hooks for browser QA and reconnect/rerender safety."
    )
    assert "_activityProcessedElapsedLabel(group)" in WORKLOG_TOOL_GROUPS_JS, (
        "The in-progress Activity summary should own the live elapsed label "
        "instead of relying on the bottom live footer."
    )
    assert "setInterval" in ACTIVITY_AND_SCROLL_JS and "_clearActivityElapsedTimer" in ACTIVITY_AND_SCROLL_JS, (
        "The active elapsed label should tick while running and clear its interval "
        "on terminal/error/session-switch cleanup paths."
    )


def test_live_footer_timer_is_re_synced_after_message_rerender():
    assert "function _syncLiveRunStatusAfterRender()" in LIVE_RUN_STATUS_JS, (
        "renderMessages() needs a dedicated helper so the live footer timer "
        "can be restored after DOM rebuilds."
    )
    assert "_syncLiveRunStatusAfterRender();" in RENDERER_JS, (
        "renderMessages() should call the live-status sync helper after it "
        "rebuilds msgInner."
    )
    assert "showLiveRunStatus(sid,{startedAt,tokens:_liveRunStatusTokens});" in LIVE_RUN_STATUS_JS, (
        "If the timer node was torn down during a rerender, the helper should "
        "recreate it for the active session."
    )


def test_compact_worklog_hides_bottom_live_footer_timer():
    show = LIVE_RUN_STATUS_JS.split("function showLiveRunStatus", 1)[1].split("function _renderLiveRunStatusContent", 1)[0]
    sync = LIVE_RUN_STATUS_JS.split("function _syncLiveRunStatusAfterRender", 1)[1].split("function hideLiveRunStatus", 1)[0]

    assert "isCompactWorklogMode" in show
    assert "if(el){el.hidden=true;el.innerHTML='';}" in show
    assert "isCompactWorklogMode" in sync
    assert "if(el){el.hidden=true;el.innerHTML='';}" in sync


def test_processed_elapsed_anchor_is_i18n_driven():
    assert "function _i18nProcessedElapsed(prefix, duration)" in I18N_HELPERS_JS
    assert "function _i18nProcessedElapsedEn(duration)" in I18N_HELPERS_JS
    assert "return _i18nProcessedElapsed('Processed', duration);" in I18N_HELPERS_JS
    assert "function _i18nProcessedElapsedZh(duration)" in I18N_HELPERS_JS
    assert "return _i18nProcessedElapsed('已处理', duration);" in I18N_HELPERS_JS
    assert "function _i18nProcessedElapsedZhHant(duration)" in I18N_HELPERS_JS
    assert "return _i18nProcessedElapsed('已處理', duration);" in I18N_HELPERS_JS
    assert "processed_elapsed: helpers._i18nProcessedElapsedEn" in I18N_EN_JS
    assert "processed_elapsed: helpers._i18nProcessedElapsedZh" in I18N_ZH_JS
    assert "processed_elapsed: helpers._i18nProcessedElapsedZhHant" in I18N_ZH_HANT_JS
    assert "t('processed_elapsed','')" in WORKLOG_TOOL_GROUPS_JS
    assert "`已处理 ${" not in ACTIVITY_TIMING_JS
