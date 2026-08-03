"""Regression coverage for manual session title regeneration controls (#3106)."""
from tests.frontend_asset_contract import family_source

from pathlib import Path
from unittest.mock import MagicMock

import api.streaming as streaming

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = family_source("sessions")
I18N_JS = family_source("i18n")
SESSION_MUTATIONS_PY = (
    ROOT / "api" / "http" / "routes" / "session_mutations.py"
).read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_session_action_menu_exposes_regenerate_title_control():
    assert "session_title_regenerate" in SESSIONS_JS
    assert "session_title_regenerate_desc" in SESSIONS_JS
    assert "ICONS.spark" in SESSIONS_JS
    assert "api('/api/session/title/regenerate'" in SESSIONS_JS
    assert "renderSessionListFromCache();" in SESSIONS_JS


def test_writable_imported_sessions_keep_regenerate_action_without_broadening_shared_gate():
    # The shared _isReadOnlySession() helper must stay scoped to read_only flags
    # so it does not silently disable rename/pin/archive/etc. for imported
    # sessions. Writable imported sessions should still expose regenerate.
    helper_idx = SESSIONS_JS.index("function _isReadOnlySession(session)")
    next_helper_idx = SESSIONS_JS.index("function _sessionSourceLabel", helper_idx)
    helper_block = SESSIONS_JS[helper_idx:next_helper_idx]
    assert "session.is_imported" not in helper_block, (
        "_isReadOnlySession must not include is_imported; writable imports need regenerate"
    )
    regen_idx = SESSIONS_JS.index("api('/api/session/title/regenerate'")
    guard_window = SESSIONS_JS[regen_idx - 600:regen_idx]
    assert "session.is_imported" not in guard_window


def test_regenerate_title_i18n_and_changelog_entries_exist():
    for key in [
        "session_title_regenerate",
        "session_title_regenerate_desc",
        "session_title_regenerating",
        "session_title_regenerated",
        "session_title_regenerate_failed",
    ]:
        assert key in I18N_JS
    assert "session action menu can regenerate conversation titles" in CHANGELOG
    assert "#3106" in CHANGELOG


def test_regenerate_endpoint_persists_generated_title_without_reordering_sidebar():
    endpoint_idx = SESSION_MUTATIONS_PY.index('"/api/session/title/regenerate"')
    next_endpoint_idx = SESSION_MUTATIONS_PY.index('"/api/personality/set"', endpoint_idx)
    block = SESSION_MUTATIONS_PY[endpoint_idx:next_endpoint_idx]
    assert "generate_session_title_for_session" in block
    assert "_persist_generated_session_title(" in block
    assert 'event_reason="session_title_regenerate"' in block
    assert "Read-only imported sessions cannot regenerate titles" in block


def test_regenerate_helper_persists_generated_title_and_publishes_sidebar_refresh(
    monkeypatch, tmp_path
):
    import api.sessions.store as models
    import api.routes as routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()

    session = models.Session(
        session_id="regenerate-title-contract",
        title="Untitled",
        messages=[{"role": "user", "content": "name this session"}],
        updated_at=123.0,
    )
    session.save(touch_updated_at=False)
    synced = []
    published = []
    monkeypatch.setattr(routes, "_sync_session_title_to_insights", synced.append)
    monkeypatch.setattr(
        routes,
        "_publish_session_list_changed",
        lambda reason, **scope: published.append((reason, scope)),
    )

    result = routes._persist_generated_session_title(
        session,
        "A durable generated title",
        event_reason="session_title_regenerate",
    )

    persisted = models.Session.load(session.session_id)
    assert result == "A durable generated title"
    assert persisted.title == "A durable generated title"
    assert persisted.llm_title_generated is True
    assert persisted.manual_title is False
    assert persisted.updated_at == 123.0
    assert len(synced) == 1
    assert synced[0].session_id == session.session_id
    assert synced[0].title == "A durable generated title"
    assert published == [(
        "session_title_regenerate",
        {"profile": session.profile, "session_id": session.session_id},
    )]


def test_regenerate_endpoint_syncs_title_to_state_db_when_enabled(monkeypatch):
    """The title-publication owner mirrors the generated title to insights."""
    from api.sessions import title_publication
    from api import state_sync

    session = MagicMock(
        session_id="regenerated-title",
        title="A durable generated title",
        messages=[{"role": "user", "content": "name this session"}],
        input_tokens=12,
        output_tokens=34,
        estimated_cost=0.56,
        model="test-model",
        profile="work",
        cache_read_tokens=7,
        cache_write_tokens=8,
    )
    mirrored = []
    monkeypatch.setattr(title_publication, "load_settings", lambda: {"sync_to_insights": True})
    monkeypatch.setattr(state_sync, "sync_session_usage", lambda **kwargs: mirrored.append(kwargs))

    title_publication._sync_session_title_to_insights(session)

    assert mirrored == [{
        "session_id": "regenerated-title",
        "input_tokens": 12,
        "output_tokens": 34,
        "estimated_cost": 0.56,
        "model": "test-model",
        "title": "A durable generated title",
        "message_count": 1,
        "profile": "work",
        "cache_read_tokens": 7,
        "cache_write_tokens": 8,
    }]


def test_streaming_helper_generates_title_from_persisted_transcript(monkeypatch):
    session = MagicMock()
    session.messages = [
        {"role": "user", "content": "Please fix the stale sidebar title controls"},
        {"role": "assistant", "content": "I will add a regenerate-title action."},
    ]

    class _ProfileEnv:
        def __enter__(self):
            return None
        def __exit__(self, exc_type, exc, tb):
            return False

    import api.profiles as profiles_api
    from api.runs.title_generation import lifecycle as title_generation
    monkeypatch.setattr(profiles_api, "profile_env_for_background_worker", lambda *args, **kwargs: _ProfileEnv())
    monkeypatch.setattr(
        title_generation,
        "_generate_llm_session_title_via_aux",
        lambda user, assistant, agent=None: ("Sidebar title controls", "llm", "raw"),
    )

    title, status, raw = streaming.generate_session_title_for_session(session)
    assert title == "Sidebar title controls"
    assert status == "llm"
    assert raw == "raw"


def test_streaming_helper_has_local_fallback_when_llm_title_is_empty(monkeypatch):
    session = MagicMock()
    session.messages = [
        {"role": "user", "content": "Can you triage this GitHub issue and PR review?"},
        {"role": "assistant", "content": "Sure."},
    ]

    class _ProfileEnv:
        def __enter__(self):
            return None
        def __exit__(self, exc_type, exc, tb):
            return False

    import api.profiles as profiles_api
    from api.runs.title_generation import lifecycle as title_generation
    monkeypatch.setattr(profiles_api, "profile_env_for_background_worker", lambda *args, **kwargs: _ProfileEnv())
    monkeypatch.setattr(title_generation, "_generate_llm_session_title_via_aux", lambda *args, **kwargs: (None, "llm_empty", ""))

    title, status, _raw = streaming.generate_session_title_for_session(session)
    assert title == "GitHub Issue Triage"
    assert status == "local_summary:llm_empty"
