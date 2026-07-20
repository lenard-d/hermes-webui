import copy
import json
import queue
import threading
from collections import OrderedDict
from contextlib import contextmanager
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

from tests._pytest_port import BASE


def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as r:
            data = r.read()
            content_type = r.headers.get("Content-Type", "")
            if "application/json" in content_type:
                return json.loads(data), r.status, dict(r.headers)
            return data.decode("utf-8"), r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        data = e.read()
        content_type = e.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return json.loads(data), e.code, dict(e.headers)
        return data.decode("utf-8"), e.code, dict(e.headers)


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def _make_session_with_messages():
    created, _ = post("/api/session/new", {})
    sid = created["session"]["session_id"]
    from api.models import Session

    # Current master keeps a freshly /api/session/new session memory-only until
    # its first message is persisted, so Session.load(sid) can return None here.
    # Construct + persist the session directly so the share path has a real file.
    session = Session.load(sid) or Session(session_id=sid)
    session.title = "Shared Test"
    session.messages = [
        {"role": "system", "content": "internal system instructions should stay private"},
        {"role": "user", "content": "Please summarize this."},
        {
            "role": "assistant",
            "content": "Here is a concise summary.",
            "provider_details": "HTTP 401: expired upstream token",
            "provider_details_label": "Provider details",
        },
        {"role": "tool", "content": "raw tool output should not be public"},
    ]
    session.workspace = "/very/private/workspace"
    session.profile = None
    session.save()
    return sid


def test_share_create_returns_public_url_and_persists_session_fields():
    sid = _make_session_with_messages()
    try:
        payload, status = post("/api/share/create", {"session_id": sid})
        assert status == 200
        assert payload["ok"] is True
        share = payload["share"]
        assert share["token"]
        assert share["url"].startswith("/share/")
        assert payload["session"]["share_token"] == share["token"]
        assert payload["session"]["share_created_at"]
    finally:
        post("/api/session/delete", {"session_id": sid})


def test_public_share_payload_is_sanitized_and_read_only():
    sid = _make_session_with_messages()
    try:
        created, _ = post("/api/share/create", {"session_id": sid})
        token = created["share"]["token"]
        payload, status, headers = get(f"/api/share/{token}")
        assert status == 200
        assert headers.get("X-Robots-Tag") == "noindex, nofollow"
        share = payload["share"]
        assert share["title"] == "Shared Test"
        assert "workspace" not in share
        assert "profile" not in share
        assert "source_session_id" not in share
        assert "token" not in share
        assert "revoked_at" not in share
        assert share["message_count"] == 2
        assert [m["role"] for m in share["messages"]] == ["user", "assistant"]
        assert all("system" != m["role"] for m in share["messages"])
        assert all("tool" != m["role"] for m in share["messages"])
        assert "provider_details" not in share["messages"][1]
        assert "provider_details_label" not in share["messages"][1]
    finally:
        post("/api/session/delete", {"session_id": sid})


def test_share_revoke_makes_link_unavailable():
    sid = _make_session_with_messages()
    try:
        created, _ = post("/api/share/create", {"session_id": sid})
        token = created["share"]["token"]
        revoked, status = post("/api/share/revoke", {"session_id": sid})
        assert status == 200
        assert revoked["ok"] is True
        missing, status, _ = get(f"/api/share/{token}")
        assert status == 404
        assert missing["error"] == "Shared conversation not found"
    finally:
        post("/api/session/delete", {"session_id": sid})


def test_share_revoke_endpoint_hides_share_token_from_session():
    sid = _make_session_with_messages()
    try:
        post("/api/share/create", {"session_id": sid})
        payload, status = post("/api/share/revoke", {"session_id": sid})
        assert status == 200
        assert payload["session"]["share_token"] is None
        assert payload["session"]["share_created_at"] is None
    finally:
        post("/api/session/delete", {"session_id": sid})


def test_share_page_serves_public_html():
    body, status, _ = get("/share/example-token")
    assert status == 200
    assert "Hermes Shared Conversation" in body
    assert "static/share.js" in body


def test_share_create_supports_raw_messaging_session_without_webui_sidecar():
    from tests.test_gateway_sync import _ensure_state_db, _insert_gateway_session, _remove_test_sessions

    conn = _ensure_state_db()
    sid = "share_tg_external_001"
    try:
        _insert_gateway_session(
            conn,
            session_id=sid,
            source="telegram",
            title="Telegram Share",
        )
        payload, status = post("/api/share/create", {"session_id": sid})
        assert status == 200
        assert payload["ok"] is True
        token = payload["share"]["token"]
        assert token
        assert payload["session"]["share_token"] == token
        assert payload["session"]["session_source"] == "messaging"
        assert payload["session"]["raw_source"] == "telegram"
        assert [m["role"] for m in payload["session"]["messages"]] == ["user", "assistant"]

        shared, status, _ = get(f"/api/share/{token}")
        assert status == 200
        assert shared["share"]["title"] == "Telegram Share"
        assert [m["content"] for m in shared["share"]["messages"]] == [
            "Hello from Telegram",
            "Hi there!",
        ]

        revoked, status = post("/api/share/revoke", {"session_id": sid})
        assert status == 200
        assert revoked["session"]["share_token"] is None
        assert [m["role"] for m in revoked["session"]["messages"]] == ["user", "assistant"]
    finally:
        try:
            post("/api/session/delete", {"session_id": sid})
        except Exception:
            pass
        _remove_test_sessions(conn, sid)
        conn.close()


def test_share_create_uses_messaging_display_transcript_when_sidecar_has_no_messages():
    from api.models import Session
    from tests.test_gateway_sync import _ensure_state_db, _insert_gateway_session, _remove_test_sessions

    conn = _ensure_state_db()
    sid = "share_discord_imported_001"
    try:
        _insert_gateway_session(
            conn,
            session_id=sid,
            source="discord",
            title="Discord Share",
        )
        local = Session(
            session_id=sid,
            title="Discord Share",
            messages=[],
            model="openai/gpt-5",
            created_at=1.0,
            updated_at=2.0,
        )
        local.is_cli_session = True
        local.session_source = "messaging"
        local.raw_source = "discord"
        local.source_tag = "discord"
        local.source_label = "Discord"
        local.save(touch_updated_at=False)

        payload, status = post("/api/share/create", {"session_id": sid})
        assert status == 200
        token = payload["share"]["token"]
        assert token
        assert [m["content"] for m in payload["session"]["messages"]] == [
            "Hello from Telegram",
            "Hi there!",
        ]

        shared, status, _ = get(f"/api/share/{token}")
        assert status == 200
        assert [m["content"] for m in shared["share"]["messages"]] == [
            "Hello from Telegram",
            "Hi there!",
        ]
    finally:
        post("/api/session/delete", {"session_id": sid})
        _remove_test_sessions(conn, sid)
        conn.close()


@pytest.fixture
def isolated_share_route_session_store(tmp_path, monkeypatch):
    """Keep direct route race tests out of the shared HTTP server state."""
    from api import models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "sessions-index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    return session_dir


@pytest.mark.parametrize("operation", ["create", "revoke"])
def test_share_metadata_commit_preserves_concurrent_transcript_write(
    isolated_share_route_session_store,
    monkeypatch,
    operation,
):
    """Share metadata must not save the stale session resolved before share I/O."""
    from api import models, routes

    sid = f"share_{operation}_concurrent_transcript"
    session = models.Session(
        session_id=sid,
        title="Concurrent share",
        messages=[{"role": "user", "content": "original"}],
        share_token="existing-share-token" if operation == "revoke" else None,
        share_created_at=1.0 if operation == "revoke" else None,
    )
    session.save(skip_index=True)
    stale_snapshot = models.Session.load(sid)
    assert stale_snapshot is not None

    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(
        routes,
        "read_body",
        lambda _handler: {"session_id": sid},
    )
    monkeypatch.setattr(routes, "_publish_session_list_changed", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, **_kwargs: captured.update(
            payload=payload,
            status=status,
        )
        or True,
    )

    def commit_newer_transcript():
        newer = models.Session.load(sid)
        assert newer is not None
        newer.messages.append({"role": "assistant", "content": "concurrent newer reply"})
        newer.save(skip_index=True)
        models.cache_full_session(sid, newer)

    def resolve_after_concurrent_transcript(_sid, _handler):
        commit_newer_transcript()
        return stale_snapshot, stale_snapshot, {}

    monkeypatch.setattr(
        routes,
        "_resolve_share_session_pair",
        resolve_after_concurrent_transcript,
    )

    if operation == "create":
        def create_share(_snapshot):
            return {
                "share_token": "new-share-token",
                "share_title": "Concurrent share",
                "share_message_count": 1,
                "share_created_at": 2.0,
                "share_updated_at": 2.0,
            }

        monkeypatch.setattr(routes, "create_or_refresh_share", create_share)
    else:
        def revoke_existing_share(_session):
            return True

        monkeypatch.setattr(routes, "revoke_share", revoke_existing_share)

    assert routes.handle_post(
        object(),
        SimpleNamespace(path=f"/api/share/{operation}", query=""),
    ) is True
    assert captured["status"] == 200

    persisted = models.Session.load(sid)
    assert persisted is not None
    assert [message["content"] for message in persisted.messages] == [
        "original",
        "concurrent newer reply",
    ]
    if operation == "create":
        assert persisted.share_token == "new-share-token"
        assert persisted.share_created_at == 2.0
    else:
        assert persisted.share_token is None
        assert persisted.share_created_at is None


@pytest.mark.parametrize("operation", ["create", "revoke"])
def test_share_metadata_failure_is_reported_and_publication_fails_closed(
    monkeypatch,
    operation,
):
    """A partial share/session commit must not be reported as full success."""
    from api import models, routes

    sid = f"share_{operation}_metadata_failure"
    token = "new-share-token" if operation == "create" else "existing-share-token"
    snapshot = models.Session(
        session_id=sid,
        title="Share failure",
        messages=[{"role": "user", "content": "share me"}],
        share_token=None if operation == "create" else token,
        share_created_at=None if operation == "create" else 1.0,
    )
    captured = {}
    revoked_tokens = []

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": sid})
    monkeypatch.setattr(
        routes,
        "_resolve_share_session_pair",
        lambda _sid, _handler: (snapshot, snapshot, {}),
    )
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, **_kwargs: captured.update(
            payload=payload,
            status=status,
        )
        or True,
    )
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, message, status=400: captured.update(
            payload={"error": message},
            status=status,
        )
        or True,
    )

    @contextmanager
    def failing_edit_session(*_args, **_kwargs):
        yield snapshot
        raise OSError("session metadata disk full")

    def record_revoke(session):
        revoked_tokens.append(session.share_token)
        return True

    monkeypatch.setattr(routes, "edit_session", failing_edit_session)
    monkeypatch.setattr(routes, "revoke_share", record_revoke)
    if operation == "create":
        monkeypatch.setattr(
            routes,
            "create_or_refresh_share",
            lambda _snapshot: {
                "share_token": token,
                "share_title": "Share failure",
                "share_message_count": 1,
                "share_created_at": 2.0,
                "share_updated_at": 2.0,
            },
        )

    assert routes.handle_post(
        object(),
        SimpleNamespace(path=f"/api/share/{operation}", query=""),
    ) is True
    assert captured["status"] == 500
    assert "session metadata" in captured["payload"]["error"].lower()
    assert revoked_tokens == [token]


@pytest.mark.parametrize("operation", ["create", "revoke"])
def test_share_mutation_does_not_resurrect_session_deleted_after_resolution(
    isolated_share_route_session_store,
    monkeypatch,
    operation,
):
    """Only genuinely external sessions may seed a missing local sidecar."""
    from api import models, routes

    sid = f"share_{operation}_deleted_before_owner"
    session = models.Session(
        session_id=sid,
        title="Delete won",
        messages=[{"role": "user", "content": "do not resurrect"}],
        share_token="existing-share-token" if operation == "revoke" else None,
        share_created_at=1.0 if operation == "revoke" else None,
    )
    session.save(skip_index=True)
    stale_snapshot = models.Session.load(sid)
    assert stale_snapshot is not None
    share_io_calls = []
    captured = {}

    def resolve_then_delete(_sid, _handler):
        (isolated_share_route_session_store / f"{sid}.json").unlink()
        with models.LOCK:
            models.SESSIONS.pop(sid, None)
        return stale_snapshot, stale_snapshot, {}

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": sid})
    monkeypatch.setattr(routes, "_resolve_share_session_pair", resolve_then_delete)
    monkeypatch.setattr(
        routes,
        "create_or_refresh_share",
        lambda _snapshot: share_io_calls.append("create") or {
            "share_token": "new-share-token",
            "share_title": "Delete won",
            "share_message_count": 1,
            "share_created_at": 2.0,
            "share_updated_at": 2.0,
        },
    )
    monkeypatch.setattr(
        routes,
        "revoke_share",
        lambda _session: share_io_calls.append("revoke") or True,
    )
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, message, status=400: captured.update(
            payload={"error": message},
            status=status,
        )
        or True,
    )
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, **_kwargs: captured.update(
            payload=payload,
            status=status,
        )
        or True,
    )

    assert routes.handle_post(
        object(),
        SimpleNamespace(path=f"/api/share/{operation}", query=""),
    ) is True
    assert captured["status"] == 404
    assert share_io_calls == []
    assert not (isolated_share_route_session_store / f"{sid}.json").exists()


def test_create_and_revoke_share_file_io_is_serialized_by_session_owner(
    isolated_share_route_session_store,
    monkeypatch,
):
    """A racing revoke must observe and revoke the create that committed first."""
    from api import config, models, routes

    sid = "share_create_revoke_owner_interleave"
    session = models.Session(
        session_id=sid,
        title="Serialized share",
        messages=[{"role": "user", "content": "share me safely"}],
    )
    session.save(skip_index=True)

    first_interleave = queue.Queue()
    create_file_entered = threading.Event()
    allow_create_file_return = threading.Event()
    live_share_tokens = {}
    thread_errors = []

    class TrackingLock:
        def __init__(self):
            self._lock = threading.Lock()

        def __enter__(self):
            if not self._lock.acquire(blocking=False):
                first_interleave.put("blocked_on_session_owner")
                self._lock.acquire()
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            self._lock.release()

    owner_lock = TrackingLock()
    monkeypatch.setattr(config, "_get_session_agent_lock", lambda _sid: owner_lock)

    def resolve_current(_sid, _handler):
        current = models.get_session(sid)
        current = routes.get_full_session(sid, session=current)
        snapshot = copy.copy(current)
        snapshot.messages = list(current.messages)
        return snapshot, current, {}

    def create_share(_snapshot):
        token = "interleaved-new-token"
        live_share_tokens[token] = True
        create_file_entered.set()
        assert allow_create_file_return.wait(timeout=5)
        return {
            "share_token": token,
            "share_title": "Serialized share",
            "share_message_count": 1,
            "share_created_at": 2.0,
            "share_updated_at": 2.0,
        }

    def revoke_current_share(current):
        first_interleave.put("revoke_share_io")
        token = str(current.share_token or "")
        if token:
            live_share_tokens[token] = False
        return bool(token)

    def capture_json(handler, payload, status=200, **_kwargs):
        handler.result = (payload, status)
        return True

    def capture_bad(handler, message, status=400):
        handler.result = ({"error": message}, status)
        return True

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: handler.body)
    monkeypatch.setattr(routes, "_resolve_share_session_pair", resolve_current)
    monkeypatch.setattr(routes, "create_or_refresh_share", create_share)
    monkeypatch.setattr(routes, "revoke_share", revoke_current_share)
    monkeypatch.setattr(routes, "_publish_session_list_changed", lambda *args, **kwargs: None)
    monkeypatch.setattr(routes, "j", capture_json)
    monkeypatch.setattr(routes, "bad", capture_bad)

    create_handler = SimpleNamespace(body={"session_id": sid}, result=None)
    revoke_handler = SimpleNamespace(body={"session_id": sid}, result=None)

    def call_route(handler, operation):
        try:
            routes.handle_post(
                handler,
                SimpleNamespace(path=f"/api/share/{operation}", query=""),
            )
        except BaseException as exc:
            thread_errors.append(exc)

    create_thread = threading.Thread(
        target=call_route,
        args=(create_handler, "create"),
    )
    revoke_thread = threading.Thread(
        target=call_route,
        args=(revoke_handler, "revoke"),
    )
    create_thread.start()
    assert create_file_entered.wait(timeout=5)
    revoke_thread.start()
    observed_interleave = first_interleave.get(timeout=5)
    allow_create_file_return.set()
    create_thread.join(timeout=5)
    revoke_thread.join(timeout=5)

    assert not create_thread.is_alive()
    assert not revoke_thread.is_alive()
    assert thread_errors == []
    assert observed_interleave == "blocked_on_session_owner"
    assert create_handler.result[1] == 200
    assert revoke_handler.result[1] == 200

    persisted = models.Session.load(sid)
    assert persisted is not None
    assert persisted.share_token is None
    assert persisted.share_created_at is None
    assert live_share_tokens == {"interleaved-new-token": False}
