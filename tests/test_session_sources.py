"""Behavioral contract for imported-session source identity."""

from types import SimpleNamespace


def test_apply_cli_source_metadata_owns_the_allowlist_and_raw_source_fallback():
    from api.sessions.sources import apply_cli_source_metadata

    session = SimpleNamespace(title="keep", unexpected="original")
    metadata = {
        "source_tag": "telegram",
        "raw_source": "",
        "session_source": "messaging",
        "source_label": "Telegram",
        "user_id": "user-1",
        "chat_id": "chat-1",
        "chat_type": "dm",
        "thread_id": "thread-1",
        "session_key": "session-key",
        "platform": "telegram",
        "title": "must not overwrite",
        "unexpected": "must not apply",
    }

    apply_cli_source_metadata(session, metadata, is_cli_session=True)

    assert session.is_cli_session is True
    assert session.source_tag == "telegram"
    assert session.raw_source == "telegram"
    assert session.session_source == "messaging"
    assert session.source_label == "Telegram"
    assert session.user_id == "user-1"
    assert session.chat_id == "chat-1"
    assert session.chat_type == "dm"
    assert session.thread_id == "thread-1"
    assert session.session_key == "session-key"
    assert session.platform == "telegram"
    assert session.title == "keep"
    assert session.unexpected == "original"


def test_import_source_metadata_keeps_storage_fields_explicitly_allowlisted():
    from api.sessions.sources import import_source_metadata

    filtered = import_source_metadata(
        {
            "source_tag": "cli",
            "project_id": "project-1",
            "model_provider": "openrouter",
            "read_only": True,
            "title": "not source metadata",
        }
    )

    assert filtered == {
        "source_tag": "cli",
        "project_id": "project-1",
        "model_provider": "openrouter",
    }
