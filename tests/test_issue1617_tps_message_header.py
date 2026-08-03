"""Regression coverage for issue #1617: TPS belongs on message headers.

Product decision:
- show live TPS in the assistant message header while streaming when real TPS is available;
- persist/show the final TPS at the end of the turn;
- do not show placeholder or estimated TPS when unavailable.
"""
from tests.frontend_asset_contract import family_source

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
CONFIG_PY = (REPO / "api" / "config" / "settings.py").read_text(
    encoding="utf-8"
)
STREAMING_PY = "\n".join(
    (REPO / "api" / "runs" / name).read_text(encoding="utf-8")
    for name in ("local.py", "local_usage.py", "local_events.py")
)
BOOT_JS = family_source("boot")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
MESSAGES_JS = family_source("messages")
PANELS_JS = family_source("panels")
UI_JS = family_source("ui")
CSS = family_source("style")


def test_tps_renders_in_message_header_not_global_titlebar():
    assert "msg-tps-inline" in UI_JS, "assistant message headers need a TPS chip hook"
    assert "msg-tps-inline" in CSS, "TPS header chip needs an explicit CSS hook"
    assert "_assistantRoleHtml(tsTitle='', tpsText='')" in UI_JS, (
        "assistant role/header rendering should accept the per-message TPS text"
    )
    assert "_formatTurnTps" in UI_JS, "TPS formatting should be centralized"
    assert "_turnTps" in UI_JS, "settled assistant messages should render final TPS from message metadata"
    assert "tpsStat" not in MESSAGES_JS, "live TPS must not target the removed/global titlebar chip"


def test_live_metering_updates_only_real_tps_and_never_placeholders():
    listener_start = MESSAGES_JS.find("source.addEventListener('metering'")
    assert listener_start != -1, "messages.js should listen for metering SSE events"
    listener_end = MESSAGES_JS.find("source.addEventListener('apperror'", listener_start)
    assert listener_end != -1, "apperror listener should follow metering listener"
    listener = MESSAGES_JS[listener_start:listener_end]
    assert "_setLiveAssistantTps" in listener, "live metering should update the live assistant header"
    assert "tps_available" in listener and "estimated" in listener, (
        "live TPS display must check availability and reject estimated readings"
    )
    assert "0.0 t/s" not in listener, "unavailable TPS should render nothing, not a 0.0 placeholder"
    assert "'—'" not in listener and '"—"' not in listener, "unavailable TPS should render nothing, not a dash"
    assert "high" not in listener.lower() and "low" not in listener.lower(), (
        "message-header TPS should not carry global HIGH/LOW titlebar semantics"
    )


def test_live_metering_usage_is_provisional_until_done():
    listener_start = MESSAGES_JS.find("source.addEventListener('metering'")
    assert listener_start != -1, "messages.js should listen for metering SSE events"
    listener_end = MESSAGES_JS.find("source.addEventListener('apperror'", listener_start)
    assert listener_end != -1, "apperror listener should follow metering listener"
    listener = MESSAGES_JS[listener_start:listener_end]

    assert "_mergeUsageForCtxIndicator(d.usage,S.lastUsage||{})" in listener, (
        "live usage should merge through the context indicator usage guard"
    )
    assert ": {...(S.lastUsage||{}),...d.usage}" in listener, (
        "live usage should update the transient usage cache for the indicator"
    )
    assert "_syncCtxIndicator(S.lastUsage)" in listener, (
        "live usage should refresh the context indicator"
    )
    assert "S.session.input_tokens=d.usage.input_tokens" not in listener
    assert "S.session.last_prompt_tokens=d.usage.last_prompt_tokens" not in listener


def test_live_prompt_estimate_reanchors_to_fresh_exact_prompt_tokens():
    assert "self._prompt_exact_tokens = 0" in STREAMING_PY, (
        "live prompt estimates need a separate exact-token anchor"
    )
    assert 'real_prompt_tokens = int(usage.get("last_prompt_tokens") or 0)' in STREAMING_PY
    assert "real_prompt_tokens != self._prompt_exact_tokens" in STREAMING_PY
    assert "self._prompt_estimate_tokens = real_prompt_tokens" in STREAMING_PY


def test_done_payload_persists_final_tps_when_exact_usage_available():
    """The settled event and assistant message share the exact TPS value."""
    from api.runs.local_success import LocalSuccessProjection

    class FakeMeter:
        def get_ttft_ms(self, _stream_id):
            return None

        def get_stats(self, _stream_id):
            return {}

    session = SimpleNamespace(
        session_id="issue-1617",
        title="TPS test",
        messages=[
            {"role": "user", "content": "hello", "timestamp": 1},
            {"role": "assistant", "content": "world", "timestamp": 2},
        ],
        input_tokens=0,
        output_tokens=0,
        estimated_cost=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        tool_calls=[],
        gateway_routing=None,
        gateway_routing_history=[],
        last_prompt_tokens=0,
        pending_user_message=None,
        pending_attachments=[],
        pending_started_at=None,
        pending_user_source=None,
        post_compression_context_tokens_estimate=None,
    )
    agent = SimpleNamespace(
        session_prompt_tokens=100,
        session_completion_tokens=20,
        session_estimated_cost_usd=0.01,
        session_cache_read_tokens=0,
        session_cache_write_tokens=0,
        model="",
        context_compressor=SimpleNamespace(
            context_length=128_000,
            threshold_tokens=100_000,
            last_prompt_tokens=100,
        ),
    )
    events = []

    with patch("api.runs.local_success.time.time", return_value=105), \
         patch("api.runs.local_success.meter", return_value=FakeMeter()):
        projection = LocalSuccessProjection.apply(
            session,
            agent=agent,
            result={},
            route_model="",
            resolved_model="",
            resolved_provider=None,
            resolved_base_url=None,
            resolved_api_key=None,
            config={},
            previous_messages=[],
            reasoning_segments={},
            live_tool_calls={},
            attachments=[],
            message_text="hello",
            turn_started_at=100,
            stream_id="stream-1617",
        )
        projection.publish_terminal(
            session,
            session_id=session.session_id,
            stream_id="stream-1617",
            agent=agent,
            publish=lambda event, payload: events.append((event, payload)),
            payload_builder=lambda _session, **_kwargs: {"session_id": session.session_id},
            tool_limit_reached=False,
            maybe_schedule_title_refresh=lambda *_args: None,
            writeback_timings=[],
            writeback_started=100,
            logger=SimpleNamespace(),
        )

    done_payload = next(payload for event, payload in events if event == "done")
    assert done_payload["usage"]["tps"] == 4.0
    assert session.messages[-1]["_turnTps"] == 4.0


def test_backend_marks_streaming_metering_availability_explicitly():
    assert "tps_available" in STREAMING_PY, "metering SSE payloads must explicitly say whether TPS is displayable"
    assert "estimated" in STREAMING_PY, "metering SSE payloads must explicitly distinguish estimated readings"
    assert "record_token(stream_id, len(STREAM_PARTIAL_TEXT[stream_id]))" not in STREAMING_PY, (
        "live TPS must not be derived from streamed character count / byte-size estimates"
    )


def test_tps_display_setting_is_default_off_and_persisted():
    assert '"show_tps": False' in CONFIG_PY, "TPS display should be disabled by default"
    assert '"show_tps"' in CONFIG_PY and "_SETTINGS_BOOL_KEYS" in CONFIG_PY, (
        "TPS display should be a persisted boolean WebUI setting"
    )
    assert "settingsShowTps" in INDEX_HTML, "Preferences needs a user-facing TPS display toggle"
    assert "payload.show_tps=showTpsCb.checked" in PANELS_JS, (
        "Preferences autosave should persist the TPS display toggle through /api/settings"
    )
    assert "showTpsCb.checked=!!settings.show_tps" in PANELS_JS, (
        "Settings panel should hydrate the TPS toggle from persisted settings"
    )
    assert "window._showTps=!!s.show_tps" in BOOT_JS, (
        "Boot should hydrate show_tps into a runtime flag"
    )
    assert "window._showTps=false" in BOOT_JS, (
        "Boot fallback should keep TPS hidden when settings cannot load"
    )


def test_tps_display_hot_applies_when_preferences_autosave():
    fn_start = PANELS_JS.find("async function _autosavePreferencesSettings")
    assert fn_start != -1, "preferences autosave function should exist"
    fn_end = PANELS_JS.find("function _retryPreferencesAutosave", fn_start)
    assert fn_end != -1, "retry function should follow preferences autosave"
    fn = PANELS_JS[fn_start:fn_end]
    assert "payload&&payload.show_tps!==undefined" in fn, (
        "TPS preference autosave must detect the show_tps field specifically"
    )
    assert "window._showTps=!!(saved&&saved.show_tps)" in fn, (
        "TPS preference autosave should update the runtime flag from the saved response"
    )
    assert "clearMessageRenderCache" in fn and "renderMessages" in fn, (
        "TPS preference autosave should re-render the open transcript without refresh"
    )


def test_tps_header_rendering_respects_display_setting():
    assert "function isTpsDisplayEnabled()" in UI_JS, "TPS visibility should be centralized"
    assert "return window._showTps===true" in UI_JS, "TPS should only render when explicitly enabled"
    assert "const tps=(isTpsDisplayEnabled()&&tpsText)" in UI_JS, (
        "settled assistant headers must suppress TPS when the setting is off"
    )
    assert "isTpsDisplayEnabled()?_formatTurnTps(value):''" in UI_JS, (
        "live TPS updates must remove/suppress the chip when the setting is off"
    )
    assert "isTpsDisplayEnabled()?_formatTurnTps(m._turnTps):''" in UI_JS, (
        "reloaded assistant messages must not render persisted TPS while disabled"
    )
