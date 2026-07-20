"""Opus pre-release follow-up tests for v0.50.268.

Pin the three SHOULD-FIX items applied during stage-268 review:

- SF-1 (#1450): child-count UI uses i18n `session_meta_children` key, not hardcoded English.
- SF-2 (#1462): duplicate carries personality / enabled_toolsets / context_length / threshold_tokens.
- SF-3 (#1462): duplicate handles legacy null title via `(session.title or 'Untitled')` fallback.
"""
import re
from types import SimpleNamespace

import pytest

from tests.frontend_asset_contract import family_source

SESSIONS_JS = family_source("sessions")
I18N_JS = family_source("i18n")


# --- SF-1 (#1450): child-count UI uses i18n key ---

def test_sf1_child_count_uses_i18n_in_sessions_js():
    """The child-count badge and meta line must call t('session_meta_children', ...).

    Pre-fix, the strings were hardcoded as `${childCount} child${childCount===1?'':'ren'}`
    which rendered English in all 9 locales.
    """
    # Two callsites
    assert "t('session_meta_children', childCount)" in SESSIONS_JS, (
        "session_meta_children i18n key not used in sessions.js — child-count UI "
        "would render English in non-English locales"
    )
    # Negative: hardcoded form must be gone
    assert "${childCount} child${childCount===1?'':'ren'}" not in SESSIONS_JS, (
        "hardcoded English child-count string still present — removes locale support"
    )


def test_sf1_session_meta_children_present_in_all_locales():
    """Every locale block in i18n.js that has session_meta_messages must also
    have session_meta_children — they're the analogous sidebar meta strings."""
    msg_count = len(re.findall(r"session_meta_messages:", I18N_JS))
    child_count = len(re.findall(r"session_meta_children:", I18N_JS))
    assert msg_count == child_count, (
        f"session_meta_messages appears {msg_count} times but "
        f"session_meta_children appears {child_count} — must be in every locale"
    )
    # Sanity: 10 known locales (en, it, ja, ru, es, de, zh, zh-Hant, plus the legacy zh-tw/zh-hk aliases)
    assert child_count >= 10, f"expected >=10 locales with session_meta_children, got {child_count}"


# --- SF-2 (#1462): duplicate carries per-session settings ---

@pytest.fixture(scope="module")
def duplicated_legacy_session(tmp_path_factory):
    """Duplicate a legacy null-title record through the actual route owner."""
    import api.routes as route_facade
    from api.http.routes import session_creation_mutations

    source = route_facade.Session(
        session_id="stage268-source",
        title=None,
        workspace=str(tmp_path_factory.mktemp("stage268-duplicate")),
        model="provider/model",
        model_provider="provider",
        messages=[{"role": "user", "content": "retain settings"}],
        personality="focused",
        enabled_toolsets=["web", "terminal"],
        context_length=32768,
        threshold_tokens=24576,
    )
    materialized = []
    responses = []
    errors = []

    class SessionForDuplicate(route_facade.Session):
        @classmethod
        def load(cls, session_id):
            return source if session_id == source.session_id else None

    def publish_materialized(session, *, persist):
        materialized.append((session, persist))

    def json_response(_handler, payload, status=200, **_kwargs):
        responses.append((payload, status))
        return True

    def bad_response(_handler, message, status=400):
        errors.append((message, status))
        return True

    ctx = dict(vars(route_facade))
    ctx.update(
        Session=SessionForDuplicate,
        _publish_materialized_session=publish_materialized,
        _session_is_subagent_view_only=lambda _session_id: False,
        publish_session_list_changed=lambda *_args, **_kwargs: None,
        j=json_response,
        bad=bad_response,
    )

    result = session_creation_mutations.handle_post(
        object(),
        SimpleNamespace(path="/api/session/duplicate"),
        {"session_id": source.session_id},
        None,
        ctx,
    )

    assert result is True
    assert errors == []
    assert len(materialized) == 1
    duplicate, persist = materialized[0]
    assert persist is True
    assert responses == [
        ({"session": duplicate.compact() | {"messages": duplicate.messages}}, 200)
    ]
    return duplicate


def test_sf2_duplicate_carries_personality(duplicated_legacy_session):
    """The duplicate must propagate `personality` from source to copy."""
    assert duplicated_legacy_session.personality == "focused"


def test_sf2_duplicate_carries_enabled_toolsets(duplicated_legacy_session):
    """The duplicate must propagate `enabled_toolsets` (per-session toolset overrides)."""
    assert duplicated_legacy_session.enabled_toolsets == ["web", "terminal"]


def test_sf2_duplicate_carries_context_settings(duplicated_legacy_session):
    """The duplicate must propagate context_length + threshold_tokens."""
    assert duplicated_legacy_session.context_length == 32768
    assert duplicated_legacy_session.threshold_tokens == 24576


# --- SF-3 (#1462): None-title fallback ---

def test_sf3_duplicate_handles_none_title(duplicated_legacy_session):
    """The duplicate handler must guard `session.title or 'Untitled'` to avoid
    `TypeError: unsupported operand type(s) for +: 'NoneType' and 'str'`
    on legacy sessions with title=null."""
    assert duplicated_legacy_session.title == "Untitled (copy)"
