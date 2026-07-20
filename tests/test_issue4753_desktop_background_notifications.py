"""Behavioral coverage for desktop-backgrounded notification delivery (#4753)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
NOTIFICATIONS_URL = (ROOT / "static" / "modules" / "messages" / "notifications.js").as_uri()
CORE_URL = (ROOT / "static" / "modules" / "messages" / "core.js").as_uri()
LIFECYCLE_URL = (ROOT / "static" / "modules" / "messages" / "stream-lifecycle.js").as_uri()

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_notification_case(*, document_hidden: bool, desktop_backgrounded: bool = False, options=None):
    payload = {
        "documentHidden": document_hidden,
        "desktopBackgrounded": desktop_backgrounded,
        "options": options or {},
    }
    script = "const params = " + json.dumps(payload) + ";\n" + f"""
(async()=>{{
const shown = [];
const direct = [];

function Notification(title, options) {{
  direct.push({{ title, options }});
  shown.push({{ title, body: options && options.body, options }});
}}
Notification.permission = 'granted';

globalThis.document = {{
  hidden: params.documentHidden,
  baseURI: 'http://test.local/',
  addEventListener(){{}},
  getElementById(){{ return null; }},
}};
globalThis.window = globalThis;
globalThis.addEventListener = () => {{}};
globalThis.location = {{href:'http://test.local/',origin:'http://test.local'}};
globalThis._notificationsEnabled = true;
globalThis.S = {{session:null}};
globalThis.navigator = {{serviceWorker:null}};
globalThis.Notification = Notification;
globalThis.assistantDisplayName = () => 'Hermes';
globalThis._notificationOptions = (body, options) => ({{ body, tag: options && options.sid ? options.sid : '' }});
globalThis._showPwaNotification = (title, body, options) => {{
    shown.push({{ title, body, options }});
    return Promise.resolve();
}};
const {{sendBrowserNotification}} = await import({NOTIFICATIONS_URL!r});
globalThis.__hermesSetBackgrounded(params.desktopBackgrounded);
await sendBrowserNotification('Response complete','Task finished',params.options);

console.log(JSON.stringify({{
  shown,
  direct,
  documentHidden: document.hidden,
  setterType: typeof globalThis.__hermesSetBackgrounded,
}}));
}})().catch(error=>{{console.error(error);process.exitCode=1;}});
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"node failed: {result.stderr}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_visible_desktop_backgrounded_tab_notifies_without_page_visibility_hidden():
    result = _run_notification_case(document_hidden=False, desktop_backgrounded=True)

    assert result["setterType"] == "function"
    assert result["documentHidden"] is False
    assert len(result["shown"]) == 1
    assert result["shown"][0]["title"] == "Response complete"


def test_visible_foreground_tab_stays_silent_without_force():
    result = _run_notification_case(document_hidden=False, desktop_backgrounded=False)

    assert result["shown"] == []
    assert result["direct"] == []


def test_real_hidden_tab_still_notifies_without_desktop_background_flag():
    result = _run_notification_case(document_hidden=True, desktop_backgrounded=False)

    assert len(result["shown"]) == 1
    assert result["shown"][0]["body"] == "Task finished"


def test_force_hidden_still_notifies_visible_documents():
    result = _run_notification_case(
        document_hidden=False,
        desktop_backgrounded=False,
        options={"forceHidden": True},
    )

    assert len(result["shown"]) == 1


def test_late_done_still_notifies_after_desktop_backgrounded_tab_returns_foreground():
    script = f"""
(async()=>{{
globalThis.document = {{
    hidden: false,
    baseURI: 'http://test.local/',
    addEventListener(){{}},
    getElementById(){{ return null; }},
}};
globalThis.window = globalThis;
globalThis.addEventListener = () => {{}};
globalThis.location = {{href:'http://test.local/'}};
const core = await import({CORE_URL!r});
const lifecycle = await import({LIFECYCLE_URL!r});
lifecycle._STREAM_WAS_HIDDEN['session-1']={{streamId:'stream-1',wasHidden:false}};
lifecycle._STREAM_NOTIFICATION_BACKGROUND['session-1']={{streamId:'stream-1',wasBackgrounded:false}};
globalThis.__hermesSetBackgrounded(true);
globalThis.__hermesSetBackgrounded(false);
const first = lifecycle._shouldForceCompletionNotification('session-1','stream-1');
const second = lifecycle._shouldForceCompletionNotification('session-1','stream-1');
console.log(JSON.stringify({{ first, second }}));
}})().catch(error=>{{console.error(error);process.exitCode=1;}});
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"node failed: {result.stderr}")
    payload = json.loads(result.stdout.strip().splitlines()[-1])

    assert payload["first"] is True
    assert payload["second"] is False
