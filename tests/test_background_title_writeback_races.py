"""Regression coverage for authoritative background-title writeback."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest


class _Session(SimpleNamespace):
    def __init__(self, sid: str, title: str, messages: list[dict], durable: dict[str, object]):
        super().__init__(
            session_id=sid,
            title=title,
            messages=messages,
            llm_title_generated=title not in {"Untitled", "New Chat"},
            manual_title=False,
        )
        self._durable = durable
        self.saved = []

    def save(self, **kwargs):
        self.saved.append(kwargs)
        self._durable[self.session_id] = self


def _install_title_worker_seams(monkeypatch, *, initial_title: str):
    import api.profiles as profiles
    import api.streaming as streaming

    sid = "title-race"
    durable: dict[str, object] = {}
    initial = _Session(
        sid,
        initial_title,
        [{"role": "user", "content": "old transcript"}],
        durable,
    )
    durable[sid] = initial
    cached = {sid: initial}
    edit_held = False

    @contextmanager
    def profile_env(*_args, **_kwargs):
        yield

    @contextmanager
    def authoritative_edit(
        edit_sid,
        *,
        session=None,
        touch_updated_at=True,
        **_kwargs,
    ):
        nonlocal edit_held
        assert edit_sid == sid
        current = durable.get(edit_sid, session)
        if current is None:
            raise KeyError(edit_sid)
        edit_held = True
        try:
            yield current
        except Exception:
            raise
        else:
            save_kwargs = {} if touch_updated_at else {"touch_updated_at": False}
            current.save(**save_kwargs)
        finally:
            edit_held = False

    monkeypatch.setattr(streaming, "get_session", lambda _sid: cached[_sid])
    monkeypatch.setattr(streaming, "SESSIONS", cached)
    monkeypatch.setattr(streaming, "LOCK", threading.Lock())
    monkeypatch.setattr(streaming, "_get_session_agent_lock", lambda _sid: threading.Lock())
    monkeypatch.setattr(streaming, "edit_session", authoritative_edit)
    monkeypatch.setattr(streaming, "_aux_title_configured", lambda: True)
    monkeypatch.setattr(profiles, "profile_env_for_background_worker", profile_env)

    return streaming, sid, initial, durable, cached, lambda: edit_held


def _run_worker(streaming, worker: str, sid: str, current_title: str, events: list[tuple]):
    put = lambda name, payload: events.append((name, payload))
    if worker == "update":
        streaming._run_background_title_update(
            sid,
            "user",
            "assistant",
            current_title,
            put,
        )
    else:
        streaming._run_background_title_refresh(
            sid,
            "user",
            "assistant",
            current_title,
            put,
        )


@pytest.mark.parametrize(
    ("worker", "initial_title"),
    [("update", "Untitled"), ("refresh", "Existing generated title")],
)
def test_background_title_writeback_does_not_resurrect_deleted_session(
    monkeypatch,
    worker,
    initial_title,
):
    streaming, sid, _initial, durable, cached, edit_is_held = _install_title_worker_seams(
        monkeypatch,
        initial_title=initial_title,
    )

    def generate(*_args, **_kwargs):
        assert edit_is_held() is False, "slow title generation must stay outside the session lock"
        durable.pop(sid)
        cached.pop(sid)
        return "Generated after delete", "llm_aux", "raw"

    monkeypatch.setattr(streaming, "_generate_llm_session_title_via_aux", generate)
    events = []

    _run_worker(streaming, worker, sid, initial_title, events)

    assert sid not in durable
    assert not any(name == "title" for name, _payload in events)


@pytest.mark.parametrize(
    ("worker", "initial_title"),
    [("update", "Untitled"), ("refresh", "Existing generated title")],
)
def test_background_title_writeback_preserves_newer_authoritative_session(
    monkeypatch,
    worker,
    initial_title,
):
    streaming, sid, initial, durable, _cached, edit_is_held = _install_title_worker_seams(
        monkeypatch,
        initial_title=initial_title,
    )
    newer = _Session(
        sid,
        "Newer authoritative title",
        [
            {"role": "user", "content": "old transcript"},
            {"role": "assistant", "content": "newer transcript"},
        ],
        durable,
    )

    def generate(*_args, **_kwargs):
        assert edit_is_held() is False, "slow title generation must stay outside the session lock"
        durable[sid] = newer
        return "Stale generated title", "llm_aux", "raw"

    monkeypatch.setattr(streaming, "_generate_llm_session_title_via_aux", generate)
    events = []

    _run_worker(streaming, worker, sid, initial_title, events)

    assert durable[sid] is newer
    assert newer.title == "Newer authoritative title"
    assert newer.messages[-1]["content"] == "newer transcript"
    assert initial.title == initial_title
    assert not any(name == "title" for name, _payload in events)
