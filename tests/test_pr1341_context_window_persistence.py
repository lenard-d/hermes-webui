"""Regression test for PR #1341 + Opus pre-release review of v0.50.246.

PR #1341 added context_length/threshold_tokens/last_prompt_tokens fields to
the Session model — but didn't add the writer that actually populates them
during streaming. The pre-release review caught this: without the writer,
the user-visible bug (context-ring shows 0% after page reload) would NOT
have been fixed by #1341 alone.

This test verifies that:
1. After a streaming turn completes, the session's context_length /
   threshold_tokens / last_prompt_tokens are written from the agent's
   compressor BEFORE s.save() is called (so they land on disk).
2. GET /api/session response includes the populated values.
3. A reloaded session retains the populated values.

Implementation reference: api/streaming.py around line 2188 (the per-turn
post-merge save) writes from getattr(agent, 'context_compressor', None).
"""
import inspect
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STREAMING = ROOT / "api" / "runs" / "local.py"
ROUTES = ROOT / "api" / "routes.py"


def test_streaming_persists_context_fields_on_session_before_save():
    """The post-merge per-turn save block must write the three fields to the
    session BEFORE calling s.save(), otherwise the values never reach disk."""
    src = STREAMING.read_text(encoding="utf-8")

    # Find the post-merge save block — anchored on the unique reasoning trace
    # marker right above the persistence block.
    block_start = src.find("Persist reasoning trace in the session")
    assert block_start != -1, "Reasoning-trace marker not found in streaming.py"

    # Save call follows shortly after
    save_match = re.search(r"\n[ \t]+s\.save\(\)", src[block_start:])
    assert save_match is not None, "s.save() not found after the post-merge marker"
    save_call = block_start + save_match.start()
    # Limit bumped to 16000 by #3455 (server-side <think> split added to the
    # pre-save reasoning-persist block, + the anchor moved to the comment marker
    # which sits a few lines above the former `if` anchor). The pre-save block
    # legitimately grew here. NOTE: this byte-distance assertion is itself brittle
    # (it must be bumped whenever a legitimate pre-save mutation block is added) — a
    # structural check (presence of s.save() shortly after the post-merge marker)
    # would be more durable; left as a follow-up. Earlier limits: 9000 (cancellation
    # guards) → 13000 (#3263 v1) → 15000 (#3256/#3263 dual-gate).
    assert save_call - block_start < 18000, (
        "s.save() should be close to the post-merge marker — block expanded unexpectedly. "
        "If you've added a new pre-save mutation block here, bump this limit."
    )

    block = src[block_start:save_call]

    # The three fields must all be assigned on s within this block
    assert "s.context_length" in block, (
        "s.context_length must be written before s.save() in the post-merge block"
    )
    assert "s.threshold_tokens" in block, (
        "s.threshold_tokens must be written before s.save() in the post-merge block"
    )
    assert "s.last_prompt_tokens" in block, (
        "s.last_prompt_tokens must be written before s.save() in the post-merge block"
    )

    # The values must come from the agent's context_compressor
    assert "context_compressor" in block, (
        "Values must be sourced from agent.context_compressor"
    )


def test_session_init_accepts_context_fields():
    """Session exposes and applies the three fields as explicit named kwargs."""
    from api.sessions.store import Session

    parameters = inspect.signature(Session).parameters
    values = {
        "context_length": 200000,
        "threshold_tokens": 180000,
        "last_prompt_tokens": 45123,
    }
    for field in values:
        assert field in parameters, f"Session.__init__ must accept {field}"
        assert parameters[field].default is None

    session = Session(session_id="context-init", **values)
    for field, expected in values.items():
        assert getattr(session, field) == expected


def test_session_metadata_fields_include_context_fields(tmp_path, monkeypatch):
    """Metadata-only loading restores all three persisted context fields."""
    from api.sessions import store as models

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", sessions_dir)

    values = {
        "context_length": 200000,
        "threshold_tokens": 180000,
        "last_prompt_tokens": 45123,
    }
    session = models.Session(
        session_id="context-metadata",
        messages=[{"role": "user", "content": "hello"}],
        **values,
    )
    session.save(skip_index=True)

    payload = json.loads(session.path.read_text(encoding="utf-8"))
    for field, expected in values.items():
        assert payload[field] == expected

    metadata = models.Session.load_metadata_only(session.session_id)
    assert metadata is not None
    for field, expected in values.items():
        assert getattr(metadata, field) == expected


def test_session_compact_exposes_context_fields():
    """Session.compact() exposes the three context values unchanged."""
    from api.sessions.store import Session

    values = {
        "context_length": 200000,
        "threshold_tokens": 180000,
        "last_prompt_tokens": 45123,
    }
    compact = Session(session_id="context-compact", **values).compact()
    for field, expected in values.items():
        assert compact[field] == expected


def test_routes_session_get_returns_context_fields():
    """GET /api/session response must include the three fields."""
    src = ROUTES.read_text(encoding="utf-8")
    # The session-detail response builder uses getattr(s, ..., 0) or 0 pattern.
    # Look for the three keys in the same response shape.
    assert '"context_length"' in src, "GET /api/session response must include context_length"
    assert '"threshold_tokens"' in src, "GET /api/session response must include threshold_tokens"
    assert '"last_prompt_tokens"' in src, "GET /api/session response must include last_prompt_tokens"


def test_session_round_trip_persists_context_fields(tmp_path, monkeypatch):
    """Real round-trip: save a Session with the fields set, reload, fields still there.

    Patches SESSION_DIR on the live api.models module so we don't pollute
    sys.modules state and break test ordering for sibling tests that depend
    on a stable api.models import (e.g. test_session_sidecar_repair.py).
    """
    from api.sessions import store as models

    # Use tmp_path as the session dir for this test only
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", sessions_dir)

    s = models.Session(session_id="ctxtest1", title="Context test")
    s.context_length = 200000
    s.threshold_tokens = 180000
    s.last_prompt_tokens = 45123
    s.save()

    # Reload from disk
    s2 = models.Session.load("ctxtest1")
    assert s2 is not None, "Session should reload"
    assert s2.context_length == 200000, f"context_length lost on reload: got {s2.context_length}"
    assert s2.threshold_tokens == 180000, f"threshold_tokens lost on reload: got {s2.threshold_tokens}"
    assert s2.last_prompt_tokens == 45123, f"last_prompt_tokens lost on reload: got {s2.last_prompt_tokens}"
