"""Regression coverage for #5139 gateway approval offline notice."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import urllib.error
from collections import OrderedDict
from pathlib import Path
from unittest.mock import MagicMock, patch

from api.sessions import cache as session_cache
from api.sessions import records as session_records
from api.config import STREAMS, STREAMS_LOCK, invalidate_gateway_caps
from api.gateway_chat import _run_gateway_chat_streaming

REPO = Path(__file__).resolve().parents[1]
GATEWAY_CHAT = (REPO / "api" / "runs" / "gateway.py").read_text(encoding="utf-8")
MESSAGES_STREAM_MODULE = REPO / "static" / "modules" / "messages" / "stream.js"


def _gateway_session(tmp_path, monkeypatch, *, session_id: str, stream_id: str):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    sessions = OrderedDict()
    monkeypatch.setattr(session_records, "SESSION_DIR", session_dir)
    monkeypatch.setattr(session_records, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(session_records, "SESSIONS", sessions)
    monkeypatch.setattr(session_cache, "SESSIONS", sessions)
    session = session_records.Session(
        session_id=session_id,
        active_stream_id=stream_id,
        workspace="/tmp",
        model="test",
        profile=None,
        context_messages=[],
        messages=[],
    )
    session._approval_notice_emitted = False
    session.save()
    session_cache.cache_full_session(session_id, session)
    return session


def _run_gateway_warning_case(unavailable_reason: str, tmp_path, monkeypatch) -> list:
    events = []
    q = MagicMock()
    q.put_nowait = lambda item: events.append(item)

    stream_id = f"sid-{unavailable_reason}"
    with STREAMS_LOCK:
        STREAMS[stream_id] = q

    session = _gateway_session(
        tmp_path,
        monkeypatch,
        session_id="sess-warning",
        stream_id=stream_id,
    )

    def fake_urlopen(req, *, timeout=None):
        assert req.full_url == "http://127.0.0.1:8642/v1/chat/completions"
        assert req.get_method() == "POST"
        payload = req.data.decode("utf-8")
        assert json.loads(payload)["messages"][-1]["content"] == "hi"
        resp = MagicMock()
        resp.__iter__ = lambda s: iter(
            [
                b'data: {"choices":[{"delta":{"content":"Done"}}]}\n',
                b"\n",
                b"data: [DONE]\n",
            ]
        )
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        return resp

    try:
        with patch.dict("os.environ", {"HERMES_WEBUI_CHAT_BACKEND": "gateway"}):
            with patch("api.runs.gateway.gateway_supports_approval", return_value=False), \
                 patch("api.runs.gateway.gateway_approval_unavailable_reason", return_value=unavailable_reason), \
                 patch("urllib.request.urlopen", side_effect=fake_urlopen), \
                 patch("api.runs.gateway.get_session", return_value=session), \
                 patch("api.runs.gateway.merge_session_messages_append_only", return_value=[]):
                _run_gateway_chat_streaming(
                    session_id="sess-warning",
                    msg_text="hi",
                    model="test-model",
                    workspace="/tmp",
                    stream_id=stream_id,
                )
    finally:
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)

    return events


def test_gateway_chat_emits_offline_warning_for_unreachable_probe(tmp_path, monkeypatch):
    events = _run_gateway_warning_case("unreachable", tmp_path, monkeypatch)
    warnings = [item for item in events if isinstance(item, tuple) and item[0] == "warning"]
    assert warnings
    assert warnings[0][1]["type"] == "approval_gateway_offline"
    assert warnings[0][1]["message"] == "Gateway connection failed. Check that the connected Hermes gateway is running and reachable."
    assert any(isinstance(item, tuple) and item[0] == "done" for item in events)
    assert not any(
        isinstance(item, tuple) and item[0] == "warning" and item[1].get("type") == "approval_gateway_unsupported"
        for item in events
    )


def test_gateway_chat_keeps_unsupported_warning_for_reachable_older_gateway(tmp_path, monkeypatch):
    events = _run_gateway_warning_case("unsupported", tmp_path, monkeypatch)
    warnings = [item for item in events if isinstance(item, tuple) and item[0] == "warning"]
    assert warnings
    assert warnings[0][1]["type"] == "approval_gateway_unsupported"
    assert warnings[0][1]["message"] == "Approvals require a newer gateway. Upgrade the connected Hermes gateway to enable this."
    assert any(isinstance(item, tuple) and item[0] == "done" for item in events)
    assert not any(
        isinstance(item, tuple) and item[0] == "warning" and item[1].get("type") == "approval_gateway_offline"
        for item in events
    )


def test_gateway_chat_keeps_unsupported_warning_for_404_capabilities_probe(tmp_path, monkeypatch):
    events = []
    q = MagicMock()
    q.put_nowait = lambda item: events.append(item)

    stream_id = "sid-http-404"
    with STREAMS_LOCK:
        STREAMS[stream_id] = q

    session = _gateway_session(
        tmp_path,
        monkeypatch,
        session_id="sess-404",
        stream_id=stream_id,
    )

    def fake_urlopen(req, *, timeout=None):
        if req.full_url == "http://127.0.0.1:8642/v1/capabilities":
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", hdrs=None, fp=io.BytesIO(b""))
        assert req.full_url == "http://127.0.0.1:8642/v1/chat/completions"
        resp = MagicMock()
        resp.__iter__ = lambda s: iter(
            [
                b'data: {"choices":[{"delta":{"content":"Done"}}]}\n',
                b"\n",
                b"data: [DONE]\n",
            ]
        )
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        return resp

    invalidate_gateway_caps()

    try:
        with patch.dict("os.environ", {"HERMES_WEBUI_CHAT_BACKEND": "gateway"}):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
                 patch("api.runs.gateway.get_session", return_value=session), \
                 patch("api.runs.gateway.merge_session_messages_append_only", return_value=[]):
                _run_gateway_chat_streaming(
                    session_id="sess-404",
                    msg_text="hi",
                    model="test-model",
                    workspace="/tmp",
                    stream_id=stream_id,
                )
    finally:
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)
        invalidate_gateway_caps()

    warnings = [item for item in events if isinstance(item, tuple) and item[0] == "warning"]
    assert warnings
    assert warnings[0][1]["type"] == "approval_gateway_unsupported"
    assert warnings[0][1]["message"] == "Approvals require a newer gateway. Upgrade the connected Hermes gateway to enable this."
    assert any(isinstance(item, tuple) and item[0] == "done" for item in events)
    assert not any(
        isinstance(item, tuple) and item[0] == "warning" and item[1].get("type") == "approval_gateway_offline"
        for item in events
    )


def test_gateway_chat_keeps_unsupported_warning_for_timeout_capabilities_probe(tmp_path, monkeypatch):
    events = []
    q = MagicMock()
    q.put_nowait = lambda item: events.append(item)

    stream_id = "sid-timeout"
    with STREAMS_LOCK:
        STREAMS[stream_id] = q

    session = _gateway_session(
        tmp_path,
        monkeypatch,
        session_id="sess-timeout",
        stream_id=stream_id,
    )

    def fake_urlopen(req, *, timeout=None):
        if req.full_url == "http://127.0.0.1:8642/v1/capabilities":
            raise TimeoutError("timed out")
        assert req.full_url == "http://127.0.0.1:8642/v1/chat/completions"
        resp = MagicMock()
        resp.__iter__ = lambda s: iter(
            [
                b'data: {"choices":[{"delta":{"content":"Done"}}]}\n',
                b"\n",
                b"data: [DONE]\n",
            ]
        )
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        return resp

    invalidate_gateway_caps()

    try:
        with patch.dict("os.environ", {"HERMES_WEBUI_CHAT_BACKEND": "gateway"}):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
                 patch("api.runs.gateway.get_session", return_value=session), \
                 patch("api.runs.gateway.merge_session_messages_append_only", return_value=[]):
                _run_gateway_chat_streaming(
                    session_id="sess-timeout",
                    msg_text="hi",
                    model="test-model",
                    workspace="/tmp",
                    stream_id=stream_id,
                )
    finally:
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)
        invalidate_gateway_caps()

    warnings = [item for item in events if isinstance(item, tuple) and item[0] == "warning"]
    assert warnings
    assert warnings[0][1]["type"] == "approval_gateway_unsupported"
    assert warnings[0][1]["message"] == "Approvals require a newer gateway. Upgrade the connected Hermes gateway to enable this."
    assert any(isinstance(item, tuple) and item[0] == "done" for item in events)
    assert not any(
        isinstance(item, tuple) and item[0] == "warning" and item[1].get("type") == "approval_gateway_offline"
        for item in events
    )


def test_messages_js_handles_offline_warning_without_touching_unsupported_branch():
    node = shutil.which("node")
    if node is None:
        return

    runner = f"""
globalThis.window=globalThis;
globalThis.document={{
  baseURI:'http://localhost/',hidden:false,visibilityState:'visible',wasDiscarded:false,
  addEventListener(){{}},removeEventListener(){{}},getElementById(){{return null;}},
  querySelector(){{return null;}},querySelectorAll(){{return [];}}
}};
globalThis.location={{href:'http://localhost/'}};
globalThis.addEventListener=()=>{{}};
globalThis.removeEventListener=()=>{{}};
globalThis.S={{session:{{session_id:'sid-1'}},messages:[],activeStreamId:'stream-1',todos:[]}};
globalThis.INFLIGHT={{}};
globalThis.api=async()=>({{}});

let toasts=[];
let statuses=[];
let translations=[];
globalThis.showToast=(...args)=>toasts.push(args);
globalThis.setComposerStatus=(value)=>statuses.push(value);
globalThis.t=(key)=>{{translations.push(key);return 'Approvals not supported';}};

class FakeEventSource {{
  static instances=[];
  static OPEN=1;
  static CONNECTING=0;
  static CLOSED=2;
  constructor(url){{
    this.url=url;
    this.listeners=Object.create(null);
    this.readyState=FakeEventSource.OPEN;
    FakeEventSource.instances.push(this);
  }}
  addEventListener(name,listener){{(this.listeners[name]??=[]).push(listener);}}
  emit(name,data){{
    for(const listener of this.listeners[name]??[]){{
      listener({{data:JSON.stringify(data),lastEventId:''}});
    }}
  }}
  close(){{this.readyState=FakeEventSource.CLOSED;}}
}}
globalThis.EventSource=FakeEventSource;

const {{attachLiveStream}}=await import({json.dumps(MESSAGES_STREAM_MODULE.as_uri())});
attachLiveStream('sid-1','stream-1');
await Promise.resolve();
const source=FakeEventSource.instances[0];
if(!source) throw new Error('stream owner did not create an EventSource');

function emitWarning(data){{
  toasts=[];
  statuses=[];
  translations=[];
  source.emit('warning',data);
  return {{toasts,statuses,translations}};
}}

console.log(JSON.stringify({{
  offline:emitWarning({{
    type:'approval_gateway_offline',
    message:'Gateway connection failed.',
  }}),
  unsupported:emitWarning({{
    type:'approval_gateway_unsupported',
    message:'Upgrade the gateway.',
  }}),
}}));
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", runner],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)

    assert observed["offline"] == {
        "toasts": [["Gateway connection failed.", 4000, "warning"]],
        "statuses": [],
        "translations": [],
    }
    assert observed["unsupported"] == {
        "toasts": [["Approvals not supported", 4000, "warning"]],
        "statuses": [],
        "translations": ["approval_gateway_unsupported_label"],
    }


def test_gateway_chat_source_mentions_offline_warning_type():
    assert "approval_type = \"approval_gateway_offline\"" in GATEWAY_CHAT
    assert "approval_type = \"approval_gateway_unsupported\"" in GATEWAY_CHAT
    assert "approval_message = \"Gateway connection failed. Check that the connected Hermes gateway is running and reachable.\"" in GATEWAY_CHAT
