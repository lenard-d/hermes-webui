import json


def test_import_cli_session_preserves_parent_session_id():
    from api.models import import_cli_session, SESSION_DIR, Session

    parent_id = 'parent_lineage_001'
    child_id = 'child_lineage_001'

    # Ensure clean fixture state for direct model-level import.
    for sid in (parent_id, child_id):
        try:
            (SESSION_DIR / f'{sid}.json').unlink(missing_ok=True)
        except Exception:
            pass

    session = import_cli_session(
        child_id,
        'Child Session',
        [{'role': 'user', 'content': 'hello', 'timestamp': 1.0}],
        model='test-model',
        parent_session_id=parent_id,
        created_at=1.0,
        updated_at=2.0,
    )

    assert session.parent_session_id == parent_id

    payload = json.loads((SESSION_DIR / f'{child_id}.json').read_text(encoding='utf-8'))
    assert payload['parent_session_id'] == parent_id

    loaded = Session.load(child_id)
    assert loaded.parent_session_id == parent_id
    assert loaded.compact()['parent_session_id'] == parent_id


def test_import_cli_session_persists_source_metadata_in_the_initial_write():
    from api.models import import_cli_session, SESSION_DIR, Session

    sid = "cli_source_metadata_001"
    (SESSION_DIR / f"{sid}.json").unlink(missing_ok=True)
    messages = [{"role": "user", "content": "keep", "timestamp": 1.0}]

    session = import_cli_session(
        sid,
        "Imported",
        messages,
        model="test-model",
        source_metadata={
            "is_cli_session": False,
            "source_tag": "cron",
            "raw_source": "cron",
            "session_source": "cron",
            "source_label": "Cron",
            "platform": "local",
            "session_id": "attacker-controlled",
            "messages": [{"role": "user", "content": "replace"}],
        },
    )

    loaded = Session.load(sid)
    metadata = Session.load_metadata_only(sid)
    assert session.source_tag == "cron"
    assert loaded is not None
    assert loaded.session_id == sid
    assert loaded.messages == messages
    assert loaded.is_cli_session is False
    assert loaded.source_tag == "cron"
    assert loaded.raw_source == "cron"
    assert loaded.session_source == "cron"
    assert loaded.source_label == "Cron"
    assert loaded.platform == "local"
    assert metadata is not None
    assert metadata.source_tag == "cron"
    assert metadata.platform == "local"
