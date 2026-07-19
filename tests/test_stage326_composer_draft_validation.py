"""Behavioral contracts for composer-draft validation and persistence."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

from api.config import SESSION_DIR
from tests._pytest_port import BASE


def _request(path: str, *, body: dict | None = None) -> tuple[dict, int]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read()), response.status
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read()), exc.code


@pytest.fixture
def draft_session():
    created, status = _request("/api/session/new", body={})
    assert status == 200
    sid = created["session"]["session_id"]
    try:
        yield sid
    finally:
        _request("/api/session/delete", body={"session_id": sid})


def _save_draft(sid: str, **fields) -> tuple[dict, int]:
    return _request("/api/session/draft", body={"session_id": sid, **fields})


def _load_session(sid: str) -> dict:
    query = urllib.parse.urlencode({"session_id": sid})
    payload, status = _request(f"/api/session?{query}")
    assert status == 200
    return payload["session"]


def test_draft_text_is_clamped_to_50kb(draft_session):
    payload, status = _save_draft(draft_session, text="x" * 60_000)

    assert status == 200
    assert len(payload["draft"]["text"]) == 50_000
    assert len(_load_session(draft_session)["composer_draft"]["text"]) == 50_000


def test_draft_files_are_clamped_to_50_entries(draft_session):
    payload, status = _save_draft(draft_session, files=[{"name": str(i)} for i in range(60)])

    assert status == 200
    assert len(payload["draft"]["files"]) == 50
    assert len(_load_session(draft_session)["composer_draft"]["files"]) == 50


def test_draft_rejects_unsupported_field_shapes_before_persisting(draft_session):
    payload, status = _save_draft(draft_session, text={"bad": True}, files="not-a-list")

    assert status == 200
    assert payload["draft"] == {"text": "", "files": []}
    assert _load_session(draft_session)["composer_draft"] == {"text": "", "files": []}


def test_draft_save_does_not_touch_session_updated_at(draft_session):
    before = _load_session(draft_session)["updated_at"]

    payload, status = _save_draft(draft_session, text="working copy")

    assert status == 200
    assert payload["draft"]["text"] == "working copy"
    assert _load_session(draft_session)["updated_at"] == before


def test_draft_save_skips_unchanged_payload_before_persist(draft_session):
    first, status = _save_draft(draft_session, text="same", files=[])
    assert status == 200
    assert "unchanged" not in first
    sidecar = SESSION_DIR / f"{draft_session}.json"
    before = sidecar.stat()

    second, status = _save_draft(draft_session, text="same", files=[])
    after = sidecar.stat()

    assert status == 200
    assert second["unchanged"] is True
    assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size)
