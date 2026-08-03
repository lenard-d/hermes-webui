"""Regression coverage for PWA-backed browser notifications (#3196)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
CONTENT_EVENTS_JS = (ROOT / "static" / "modules" / "messages" / "content-events.js").read_text(encoding="utf-8")
TERMINAL_EVENTS_JS = (ROOT / "static" / "modules" / "messages" / "terminal-events.js").read_text(encoding="utf-8")
STREAM_LIFECYCLE_JS = (ROOT / "static" / "modules" / "messages" / "stream-lifecycle.js").read_text(encoding="utf-8")
STREAM_TRANSPORT_JS = (ROOT / "static" / "modules" / "messages" / "stream-transport.js").read_text(encoding="utf-8")
SW_JS = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "modules" / "panels" / "settings-system.js").read_text(encoding="utf-8")
I18N_SOURCES = tuple((ROOT / "static" / "i18n_parts").glob("locale-*.js"))
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
NOTIFICATIONS_URL = (ROOT / "static" / "modules" / "messages" / "notifications.js").as_uri()

requires_node = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(script: str) -> dict:
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _notification_prelude(*, permission: str = "granted") -> str:
    return f"""
const shown=[];
const direct=[];
function Notification(title, options) {{ direct.push({{title,options}}); }}
Notification.permission={permission!r};
globalThis.document={{hidden:false,baseURI:'http://test.local/',addEventListener(){{}},getElementById(){{return null;}}}};
globalThis.window=globalThis;
globalThis.addEventListener=()=>{{}};
globalThis.location={{href:'http://test.local/current',origin:'http://test.local'}};
Object.defineProperty(globalThis,'navigator',{{configurable:true,value:{{serviceWorker:{{
  getRegistration:async()=>({{active:true,showNotification:async(title,options)=>shown.push({{title,options}})}})
}}}}}});
globalThis.S={{session:{{session_id:'active-session'}}}};
globalThis._notificationsEnabled=true;
globalThis.Notification=Notification;
globalThis.assistantDisplayName=()=> 'Hermes';
globalThis._sessionUrlForSid=(sid)=>`/chat/${{sid}}`;
globalThis.showToast=()=>{{}};
globalThis.t=(key)=>key;
"""


@requires_node
def test_browser_notifications_use_service_worker_when_available():
    result = _run_node(
        _notification_prelude()
        + f"""
const {{sendBrowserNotification}}=await import({NOTIFICATIONS_URL!r});
await sendBrowserNotification('Response complete','Task finished',{{force:true}});
await new Promise(resolve=>setTimeout(resolve,0));
console.log(JSON.stringify({{shown,direct}}));
"""
    )

    assert [event["title"] for event in result["shown"]] == ["Response complete"]
    assert result["direct"] == []


@requires_node
def test_notification_payload_uses_completion_session_when_provided():
    result = _run_node(
        _notification_prelude()
        + f"""
const {{sendBrowserNotification}}=await import({NOTIFICATIONS_URL!r});
await sendBrowserNotification('Response complete','Task finished',{{force:true,sid:'completed-session'}});
await new Promise(resolve=>setTimeout(resolve,0));
console.log(JSON.stringify(shown[0].options));
"""
    )

    assert result["tag"] == "hermes-completed-session"
    assert result["data"] == {"url": "http://test.local/chat/completed-session"}
    assert "sendBrowserNotification('Approval required',data.description||'Tool approval needed',{sid:activeSid})" in CONTENT_EVENTS_JS
    assert "sendBrowserNotification('Clarification needed',data.question||'Tool clarification needed',{sid:activeSid})" in CONTENT_EVENTS_JS


@requires_node
def test_completion_notification_preview_uses_settled_message_not_live_prefix():
    """Background completion preview must not slice the live-stream accumulator."""
    result = _run_node(
        _notification_prelude()
        + f"""
globalThis._assistantTurnAnchorSettledFinalAnswer=()=> 'Settled final answer';
globalThis.msgContent=(message)=>message.content;
const {{_completionNotificationPreviewText}}=await import({NOTIFICATIONS_URL!r});
const preview=_completionNotificationPreviewText(
  {{role:'assistant',content:'live prefix that must not be notified'}},
  {{sessionId:'completed-session',liveDisplayText:'live prefix that must not be notified'}}
);
console.log(JSON.stringify({{preview}}));
"""
    )

    assert result["preview"] == "Settled final answer"
    assert "_completionNotificationPreviewText(lastAsst," in TERMINAL_EVENTS_JS
    assert "d.session.messages" in TERMINAL_EVENTS_JS


@requires_node
def test_completion_notification_fires_when_tab_was_hidden_during_stream():
    """#4416: a throttled background-tab SSE delivers `done` late (after the user
    returns, document.hidden=false), which silently dropped the completion
    notification. The done handler now passes forceHidden based on whether the
    tab was hidden at ANY point during the stream, and sendBrowserNotification
    bypasses ONLY the live visibility gate (not the user's enabled setting) on
    forceHidden — so a backgrounded stream notifies, a watched one stays silent."""
    result = _run_node(
        _notification_prelude()
        + f"""
const lifecycle=await import({(ROOT / 'static' / 'modules' / 'messages' / 'stream-lifecycle.js').as_uri()!r});
const {{sendBrowserNotification}}=await import({NOTIFICATIONS_URL!r});
lifecycle._STREAM_WAS_HIDDEN['session-1']={{streamId:'stream-1',wasHidden:true}};
const forceHidden=lifecycle._shouldForceCompletionNotification('session-1','stream-1');
await sendBrowserNotification('Response complete','Task finished',{{forceHidden}});
await new Promise(resolve=>setTimeout(resolve,0));
console.log(JSON.stringify({{forceHidden,shown,hiddenEntries:lifecycle._STREAM_WAS_HIDDEN}}));
"""
    )

    assert result["forceHidden"] is True
    assert [event["title"] for event in result["shown"]] == ["Response complete"]
    assert result["hiddenEntries"] == {}


@requires_node
def test_desktop_background_notification_signal_stays_out_of_stream_visibility():
    result = _run_node(
        _notification_prelude()
        + f"""
const lifecycle=await import({(ROOT / 'static' / 'modules' / 'messages' / 'stream-lifecycle.js').as_uri()!r});
lifecycle._STREAM_WAS_HIDDEN['session-1']={{streamId:'stream-1',wasHidden:false}};
lifecycle._STREAM_NOTIFICATION_BACKGROUND['session-1']={{streamId:'stream-1',wasBackgrounded:false}};
globalThis.__hermesSetBackgrounded(true);
globalThis.__hermesSetBackgrounded(false);
console.log(JSON.stringify({{
  hidden:lifecycle._STREAM_WAS_HIDDEN['session-1'].wasHidden,
  background:lifecycle._STREAM_NOTIFICATION_BACKGROUND['session-1'].wasBackgrounded,
  forced:lifecycle._shouldForceCompletionNotification('session-1','stream-1'),
}}));
"""
    )

    assert result == {"hidden": False, "background": True, "forced": True}
    assert "_desktopBackgroundedForNotifications" not in STREAM_LIFECYCLE_JS
    assert "_desktopBackgroundedForNotifications" not in STREAM_TRANSPORT_JS


def test_service_worker_handles_notification_clicks_without_hijacking_other_sessions():
    assert "notificationclick" in SW_JS
    assert "event.notification.close()" in SW_JS
    assert "clients.matchAll" in SW_JS
    assert "clients.openWindow" in SW_JS
    # Match the open tab on pathname, not the full href (query/hash differ).
    assert "samePath(client.url)" in SW_JS
    assert "new URL(clientUrl).pathname === targetPath" in SW_JS
    assert "targetClient.focus()" in SW_JS
    exact_idx = SW_JS.index("targetClient.focus()")
    open_idx = SW_JS.index("self.clients.openWindow(targetUrl)")
    navigate_idx = SW_JS.index("focusableClient.navigate(targetUrl)")
    assert exact_idx < open_idx < navigate_idx


def test_settings_expose_permission_and_test_controls():
    assert "notificationPermissionStatus" in INDEX_HTML
    assert 'id="notificationPermissionButtonWrap"' in INDEX_HTML
    assert 'id="notificationPermissionButton"' in INDEX_HTML
    assert "requestNotificationPermission()" in INDEX_HTML
    assert "sendBrowserNotification('Hermes test'" in INDEX_HTML
    assert "{force:true}" in INDEX_HTML
    assert "function updateNotificationPermissionStatus" in PANELS_JS
    assert "const btn=$('notificationPermissionButton');" in PANELS_JS
    assert "const btnWrap=$('notificationPermissionButtonWrap');" in PANELS_JS
    assert "btn.disabled=granted;" in PANELS_JS
    assert "btn.title=granted?'':label;" in PANELS_JS
    assert "if(btnWrap) btnWrap.title=label;" in PANELS_JS
    assert "notifications_permission_status" in PANELS_JS
    assert "btn.setAttribute('aria-label', label);" in PANELS_JS
    assert "btn.setAttribute('aria-disabled', granted?'true':'false');" in PANELS_JS
    assert "btn.setAttribute('aria-disabled','true');" in PANELS_JS


@requires_node
def test_granted_permission_branch_is_not_silent():
    result = _run_node(
        _notification_prelude()
        + f"""
const updates=[];
const toasts=[];
globalThis.updateNotificationPermissionStatus=()=>updates.push('updated');
globalThis.showToast=(...args)=>toasts.push(args);
const {{requestNotificationPermission}}=await import({NOTIFICATIONS_URL!r});
const permission=await requestNotificationPermission();
console.log(JSON.stringify({{permission,updates,toasts}}));
"""
    )

    assert result["permission"] == "granted"
    assert result["updates"] == ["updated"]
    assert result["toasts"] == [["notifications_enabled_toast", 3000]]


def test_notification_i18n_and_changelog_entries_exist():
    for key in [
        "notifications_enable_btn",
        "notifications_test_btn",
        "notifications_permission_status",
        "notifications_enabled_toast",
        "notifications_denied",
        "notifications_unsupported",
    ]:
        assert any(key in source.read_text(encoding="utf-8") for source in I18N_SOURCES)
    assert "PWA notifications now use the service worker" in CHANGELOG
    assert "#3196" in CHANGELOG
    entry = next(
        line for line in CHANGELOG.splitlines()
        if "Notification permission controls now reflect the real browser state" in line
    )
    assert entry.count("#4118") == 1
