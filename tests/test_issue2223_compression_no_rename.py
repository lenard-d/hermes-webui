"""
Tests for #2223: compression session rotation must not destroy session history.

The previous implementation renamed old_sid.json → new_sid.json during context
compression, destroying the only persistent copy of the uncompressed history
before the new session had been saved.  If the summariser also failed, the user
was left with zero recoverable messages.

The fix preserves old_sid.json and creates new_sid.json as a fresh file, setting
parent_session_id to link the lineage.
"""
import json

import pytest


@pytest.fixture
def isolated_session_dir(tmp_path, monkeypatch):
    """Isolate both disk and the shared in-process session cache."""
    from api.sessions import records

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    cached_sessions = records.SESSIONS.copy()
    monkeypatch.setattr(records, "SESSION_DIR", session_dir)
    monkeypatch.setattr(records, "SESSION_INDEX_FILE", session_dir / "_index.json")
    records.SESSIONS.clear()
    try:
        yield session_dir
    finally:
        records.SESSIONS.clear()
        records.SESSIONS.update(cached_sessions)


class TestNoRenameDuringCompression:
    """Compression rotation preserves an archived parent and a live child."""

    def test_rotation_preserves_parent_snapshot_and_relinks_fork_continuation(
        self, isolated_session_dir, monkeypatch
    ):
        """A rotation leaves old history intact and makes the child point to it.

        In particular, a forked source must not retain its older fork parent as
        the active continuation parent.  The immediate pre-compression snapshot
        is the authoritative lineage hop.
        """
        from api.sessions.records import Session
        from api.runs import local_compression
        from api.runs.local_compression import LocalCompressionOwner

        monkeypatch.setattr(local_compression, "alias_session_agent_lock", lambda *_args: None)
        monkeypatch.setattr(LocalCompressionOwner, "_migrate_agent_cache", lambda *_args: None)

        session = Session(
            session_id="old_sid",
            title="Forked Long Chat",
            parent_session_id="fork_parent",
            messages=[{"role": "user", "content": "before"}],
        )
        session.save()
        session.messages.append({"role": "assistant", "content": "after"})

        owner = LocalCompressionOwner(
            original_session_id="old_sid",
            profile_name=None,
            agent=type("Agent", (), {"session_id": "new_sid"})(),
            session_lock=object(),
            logger=local_compression.logging.getLogger(__name__),
        )
        owner.rotate_if_needed(session)
        session.save()

        old_payload = json.loads((isolated_session_dir / "old_sid.json").read_text(encoding="utf-8"))
        new_payload = json.loads((isolated_session_dir / "new_sid.json").read_text(encoding="utf-8"))
        assert old_payload["pre_compression_snapshot"] is True
        assert old_payload["parent_session_id"] == "fork_parent"
        assert len(old_payload["messages"]) == 2
        assert new_payload["parent_session_id"] == "old_sid"
        assert len(new_payload["messages"]) == 2
        index = json.loads((isolated_session_dir / "_index.json").read_text(encoding="utf-8"))
        index_by_id = {entry["session_id"]: entry for entry in index}
        assert index_by_id["old_sid"]["pre_compression_snapshot"] is True
        assert index_by_id["new_sid"]["parent_session_id"] == "old_sid"
        assert session.session_id == "new_sid"
        assert session.parent_session_id == "old_sid"
        assert not session.pre_compression_snapshot


class TestMergePreservesHistory:
    """_merge_display_messages_after_agent_result must preserve all previous
    display messages when compression returns only a marker."""

    @pytest.fixture
    def merge(self):
        from api.runs.transcript import _merge_display_messages_after_agent_result

        return _merge_display_messages_after_agent_result

    def test_marker_only_preserves_all_previous(self, merge):
        """When result is just a compression-failure marker, previous display survives."""
        previous_display = [
            {"role": "user", "content": f"msg{i}"} for i in range(100)
        ] + [
            {"role": "assistant", "content": f"reply{i}"} for i in range(100)
        ]
        previous_context = list(previous_display)
        marker = {
            "role": "user",
            "content": (
                "Summary generation was unavailable. 200 message(s) were removed "
                "to free context space but could not be summarized."
            ),
        }
        result = [marker, {"role": "user", "content": "continue"}, {"role": "assistant", "content": "ok"}]

        merged = merge(previous_display, previous_context, result, "continue")

        # All 200 original messages must survive.
        assert len(merged) >= 200
        for i in range(100):
            assert merged[i]["content"] == f"msg{i}"

    def test_empty_result_preserves_all_previous(self, merge):
        """If result_messages is empty, previous display is returned unchanged."""
        previous_display = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        previous_context = list(previous_display)

        merged = merge(previous_display, previous_context, [], "test")

        assert merged == previous_display

    def test_none_result_preserves_all_previous(self, merge):
        """If result_messages is None, previous display is returned unchanged."""
        previous_display = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        previous_context = list(previous_display)

        merged = merge(previous_display, previous_context, None, "test")

        assert merged == previous_display
