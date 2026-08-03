"""Regression coverage for the browser-offline banner and auto-refresh loop."""

from __future__ import annotations

import pathlib


REPO_ROOT = pathlib.Path(__file__).parent.parent
OFFLINE_RECOVERY_JS = (REPO_ROOT / "static" / "modules" / "ui" / "offline-recovery.js").read_text(encoding="utf-8")
MESSAGES_CORE_JS = (REPO_ROOT / "static" / "modules" / "messages" / "core.js").read_text(encoding="utf-8")
STREAM_TRANSPORT_JS = (REPO_ROOT / "static" / "modules" / "messages" / "stream-transport.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
SHELL_NAVIGATION_CSS = (REPO_ROOT / "static" / "style_parts" / "003-shell-navigation.css").read_text(encoding="utf-8")
LOCALE_SOURCES = tuple(
    path.read_text(encoding="utf-8")
    for path in sorted((REPO_ROOT / "static" / "i18n_parts").glob("locale-*.js"))
)


def test_offline_banner_markup_styles_and_copy_exist():
    assert 'id="offlineBanner"' in INDEX_HTML
    assert 'role="status"' in INDEX_HTML
    assert 'aria-live="assertive"' in INDEX_HTML
    assert 'onclick="checkOfflineRecoveryNow()"' in INDEX_HTML
    assert ".offline-banner" in SHELL_NAVIGATION_CSS
    assert ".offline-banner.visible" in SHELL_NAVIGATION_CSS
    assert ".offline-action[disabled]" in SHELL_NAVIGATION_CSS
    for key in (
        "offline_title",
        "offline_browser_detail",
        "offline_network_detail",
        "offline_autorefresh",
        "offline_check_now",
        "offline_checking",
        "offline_stream_waiting",
    ):
        assert all(key in locale for locale in LOCALE_SOURCES)


def test_offline_monitor_patches_fetch_and_auto_reloads_after_health_probe():
    assert "const OFFLINE_RECHECK_MS=2500" in OFFLINE_RECOVERY_JS
    assert "const OFFLINE_HEALTH_TIMEOUT_MS=10000" in OFFLINE_RECOVERY_JS
    assert "const OFFLINE_FETCH_FAILURES_BEFORE_BANNER=2" in OFFLINE_RECOVERY_JS
    assert "window.fetch=async function(...args)" in OFFLINE_RECOVERY_JS
    assert "window.addEventListener('offline',()=>{void _showOfflineBannerIfProbeFails('browser',{requireConsecutiveFailures:false});});" in OFFLINE_RECOVERY_JS
    assert "window.addEventListener('online',()=>{if(_offlineVisible)checkOfflineRecoveryNow();})" in OFFLINE_RECOVERY_JS
    assert "setInterval(()=>{checkOfflineRecoveryNow();},OFFLINE_RECHECK_MS)" in OFFLINE_RECOVERY_JS
    assert "new URL('health',document.baseURI||location.href)" in OFFLINE_RECOVERY_JS
    assert "window.location.reload()" in OFFLINE_RECOVERY_JS


def test_offline_recovery_probe_is_serialized_and_stops_timer_before_reload():
    assert "let _offlineProbePromise=null" in OFFLINE_RECOVERY_JS
    assert "let _offlineHealthProbePromise=null" in OFFLINE_RECOVERY_JS
    assert "if(!_offlineVisible)return false;" in OFFLINE_RECOVERY_JS
    assert "if(!_offlineVisible&&!_offlineFetchPatched)return false;" not in OFFLINE_RECOVERY_JS
    assert "finally{_offlineProbePromise=null;}" in OFFLINE_RECOVERY_JS
    assert "finally{_offlineHealthProbePromise=null;}" in OFFLINE_RECOVERY_JS
    reload_idx = OFFLINE_RECOVERY_JS.find("window.location.reload()")
    assert reload_idx != -1
    assert OFFLINE_RECOVERY_JS.rfind("_stopOfflineProbeTimer();", 0, reload_idx) != -1


def test_fetch_typeerror_is_gated_by_health_probe_not_blind_banner():
    fetch_patch = OFFLINE_RECOVERY_JS.split("window.fetch=async function(...args){", 1)[1].split("function initOfflineMonitor", 1)[0]
    assert "function _isAbortError(e)" in OFFLINE_RECOVERY_JS
    assert "!_isAbortError(e)&&(e instanceof TypeError||!_browserReportsOnline())" in fetch_patch
    assert "void _showOfflineBannerIfProbeFails(_browserReportsOnline()?'network':'browser');" in fetch_patch
    assert "_offlineFetchProbeFailures<OFFLINE_FETCH_FAILURES_BEFORE_BANNER" in OFFLINE_RECOVERY_JS
    assert "if(!_browserReportsOnline())showOfflineBanner('browser');" not in fetch_patch


def test_sse_network_error_defers_to_offline_banner_instead_of_inline_error():
    assert "function _deferStreamErrorIfOffline()" in MESSAGES_CORE_JS
    assert "t('offline_stream_waiting')" in MESSAGES_CORE_JS
    assert "if(_deferStreamErrorIfOffline()) return;" in STREAM_TRANSPORT_JS
    error_handler = STREAM_TRANSPORT_JS.split("source.addEventListener('error',async e=>{", 1)[1].split("source.addEventListener('cancel'", 1)[0]
    assert error_handler.find("_deferStreamErrorIfOffline()") < error_handler.rfind("_handleStreamError(source)")


def test_sse_error_defers_while_page_hidden_until_tab_returns():
    assert "function _deferStreamErrorIfPageHidden(source)" in STREAM_TRANSPORT_JS
    assert "document.visibilityState==='hidden'" in STREAM_TRANSPORT_JS
    assert "document.wasDiscarded===true" in STREAM_TRANSPORT_JS
    assert "Connection paused. Reconnecting when this tab returns…" in STREAM_TRANSPORT_JS
    assert "document.addEventListener('visibilitychange',resume)" in STREAM_TRANSPORT_JS
    assert "window.addEventListener('pageshow',resume)" in STREAM_TRANSPORT_JS
    error_handler = STREAM_TRANSPORT_JS.split("source.addEventListener('error',async e=>{", 1)[1].split("source.addEventListener('cancel'", 1)[0]
    assert "if(_deferStreamErrorIfPageHidden(source)) return;" in error_handler
    assert error_handler.find("_deferStreamErrorIfPageHidden(source)") < error_handler.rfind("_handleStreamError(source)")


def test_deferred_hidden_stream_error_reattaches_or_restores_before_inline_error():
    recovery_block = STREAM_TRANSPORT_JS.split("function _reattachOrRestoreAfterDeferredStreamError(source){", 1)[1].split("function _deferStreamErrorIfPageHidden(source)", 1)[0]
    assert "api(`/api/chat/stream/status?stream_id=${encodeURIComponent(streamId)}`)" in recovery_block
    assert "if(st.active)" in recovery_block
    assert "_wireSource(new EventSource" in recovery_block
    assert "if(await _restoreSettledSession(source, {preserveVisibleOnShorterTerminalSnapshot:true})) return;" in recovery_block
    assert recovery_block.find("if(await _restoreSettledSession(source, {preserveVisibleOnShorterTerminalSnapshot:true})) return;") < recovery_block.rfind("_handleStreamError(source)")
