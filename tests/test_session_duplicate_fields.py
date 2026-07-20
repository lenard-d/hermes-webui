"""Behavioral field-propagation tests for session duplicate and branch owners."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest


def _source_session(tmp_path):
    messages = [
        {"role": "user", "content": {"text": ["hello"]}},
        {"role": "assistant", "content": {"text": ["world"]}},
    ]
    return SimpleNamespace(
        session_id="source-session",
        title="Source",
        workspace=str(tmp_path),
        model="provider/model",
        model_provider="provider",
        messages=messages,
        tool_calls=[{"name": "search", "args": {"queries": ["one"]}}],
        pinned=True,
        archived=True,
        project_id="project-1",
        profile="profile-1",
        input_tokens=101,
        output_tokens=37,
        estimated_cost=0.42,
        cache_read_tokens=19,
        cache_write_tokens=11,
        personality="focused",
        enabled_toolsets=["web", "terminal"],
        context_length=32000,
        threshold_tokens=24000,
        truncation_watermark=123.5,
        truncation_boundary={"message_count": 2, "keys": ["first"]},
        context_messages=copy.deepcopy(messages),
        gateway_routing={"provider": "provider", "fallbacks": ["backup"]},
        gateway_routing_history=[{"provider": "provider", "attempt": 1}],
        llm_title_generated=True,
        manual_title=True,
        composer_draft={"text": "draft", "attachments": ["a.txt"]},
        context_engine="legacy",
        context_engine_state={"cursor": [1, 2]},
        compression_anchor_visible_idx=1,
        compression_anchor_message_key="anchor-key",
        compression_anchor_summary="summary",
        compression_anchor_details={"details": ["one"]},
        compression_anchor_mode="auto",
        compression_anchor_engine="legacy",
        worktree_path=str(tmp_path / "worktree"),
        worktree_branch="feature/source",
        worktree_repo_root=str(tmp_path),
        last_prompt_tokens=2048,
        active_stream_id="stream-source",
        pending_user_message="pending",
        pending_attachments=[{"name": "pending.txt"}],
        pending_started_at=999.0,
        parent_session_id="source-parent",
        session_source="webui",
        _branch_source_readonly=True,
    )


def _recording_context(route_facade):
    published = []
    timeline = []
    responses = []

    def publish_materialized(session, *, persist):
        published.append((session, persist))
        timeline.append(("materialize", persist))

    def publish_event(reason, **details):
        timeline.append(("event", reason, details))

    def json_response(_handler, payload, status=200, **_kwargs):
        responses.append((payload, status))
        timeline.append(("response", status))
        return True

    ctx = dict(vars(route_facade))
    ctx.update(
        _publish_materialized_session=publish_materialized,
        publish_session_list_changed=publish_event,
        j=json_response,
    )
    return ctx, published, timeline, responses


@pytest.fixture(scope="module")
def duplicate_case(tmp_path_factory):
    import api.routes as route_facade
    from api.http.routes import session_creation_mutations

    source = _source_session(tmp_path_factory.mktemp("duplicate-source"))

    class SessionForDuplicate(route_facade.Session):
        @classmethod
        def load(cls, session_id):
            return source if session_id == source.session_id else None

    ctx, published, timeline, responses = _recording_context(route_facade)
    ctx.update(
        Session=SessionForDuplicate,
        _session_is_subagent_view_only=lambda _session_id: False,
    )

    result = session_creation_mutations.handle_post(
        object(),
        SimpleNamespace(path="/api/session/duplicate"),
        {"session_id": source.session_id},
        None,
        ctx,
    )

    assert result is True
    assert len(published) == 1
    return SimpleNamespace(
        source=source,
        session=published[0][0],
        persist=published[0][1],
        timeline=timeline,
        responses=responses,
    )


@pytest.fixture(scope="module")
def branch_case(tmp_path_factory):
    import api.routes as route_facade
    from api.http.routes import session_mutations

    source = _source_session(tmp_path_factory.mktemp("branch-source"))
    ctx, published, timeline, responses = _recording_context(route_facade)
    ctx.update(
        _load_branch_source_or_refuse=lambda _handler, _session_id: source,
        _session_requires_cli_metadata_lookup=lambda _session: False,
        _is_messaging_session_record=lambda _record: False,
        _lookup_cli_session_metadata=lambda _session_id: {},
        get_cli_session_messages=lambda _session_id: [],
    )

    result = session_mutations.handle_post(
        object(),
        SimpleNamespace(path="/api/session/branch"),
        {"session_id": source.session_id},
        None,
        ctx,
    )

    assert result is True
    assert len(published) == 1
    return SimpleNamespace(
        source=source,
        session=published[0][0],
        persist=published[0][1],
        timeline=timeline,
        responses=responses,
    )


def _assert_copied(case, field):
    assert getattr(case.session, field) == getattr(case.source, field)


def _assert_deepcopied(case, field):
    _assert_copied(case, field)
    assert getattr(case.session, field) is not getattr(case.source, field)


# ── Duplicate: critical fields MUST be copied ────────────────────────────────


def test_duplicate_copies_truncation_watermark(duplicate_case):
    _assert_copied(duplicate_case, "truncation_watermark")


def test_duplicate_copies_truncation_boundary(duplicate_case):
    _assert_copied(duplicate_case, "truncation_boundary")


def test_duplicate_copies_context_messages(duplicate_case):
    _assert_deepcopied(duplicate_case, "context_messages")
    assert (
        duplicate_case.session.context_messages[0]["content"]
        is not duplicate_case.source.context_messages[0]["content"]
    )


def test_duplicate_copies_gateway_routing(duplicate_case):
    _assert_deepcopied(duplicate_case, "gateway_routing")
    assert (
        duplicate_case.session.gateway_routing["fallbacks"]
        is not duplicate_case.source.gateway_routing["fallbacks"]
    )


def test_duplicate_copies_gateway_routing_history(duplicate_case):
    _assert_deepcopied(duplicate_case, "gateway_routing_history")
    assert (
        duplicate_case.session.gateway_routing_history[0]
        is not duplicate_case.source.gateway_routing_history[0]
    )


def test_duplicate_copies_cache_tokens(duplicate_case):
    _assert_copied(duplicate_case, "cache_read_tokens")
    _assert_copied(duplicate_case, "cache_write_tokens")


def test_duplicate_copies_enabled_toolsets(duplicate_case):
    _assert_copied(duplicate_case, "enabled_toolsets")


def test_duplicate_copies_llm_title_generated(duplicate_case):
    _assert_copied(duplicate_case, "llm_title_generated")


def test_duplicate_copies_composer_draft(duplicate_case):
    _assert_deepcopied(duplicate_case, "composer_draft")
    assert (
        duplicate_case.session.composer_draft["attachments"]
        is not duplicate_case.source.composer_draft["attachments"]
    )


def test_duplicate_copies_context_engine(duplicate_case):
    _assert_copied(duplicate_case, "context_engine")
    _assert_deepcopied(duplicate_case, "context_engine_state")
    assert (
        duplicate_case.session.context_engine_state["cursor"]
        is not duplicate_case.source.context_engine_state["cursor"]
    )


def test_duplicate_copies_model_provider(duplicate_case):
    _assert_copied(duplicate_case, "model_provider")


def test_duplicate_copies_personality(duplicate_case):
    _assert_copied(duplicate_case, "personality")


def test_duplicate_copies_project_id(duplicate_case):
    _assert_copied(duplicate_case, "project_id")


def test_duplicate_copies_profile(duplicate_case):
    _assert_copied(duplicate_case, "profile")


def test_duplicate_copies_context_length(duplicate_case):
    _assert_copied(duplicate_case, "context_length")


def test_duplicate_copies_threshold_tokens(duplicate_case):
    _assert_copied(duplicate_case, "threshold_tokens")


def test_duplicate_copies_workspace(duplicate_case):
    _assert_copied(duplicate_case, "workspace")


def test_duplicate_copies_model(duplicate_case):
    _assert_copied(duplicate_case, "model")


def test_duplicate_copies_usage_counters(duplicate_case):
    _assert_copied(duplicate_case, "input_tokens")
    _assert_copied(duplicate_case, "output_tokens")
    _assert_copied(duplicate_case, "estimated_cost")


# ── Duplicate: mutable fields MUST use deepcopy ──────────────────────────────


def test_duplicate_deepcopies_messages(duplicate_case):
    _assert_deepcopied(duplicate_case, "messages")
    assert duplicate_case.session.messages[0] is not duplicate_case.source.messages[0]
    assert (
        duplicate_case.session.messages[0]["content"]["text"]
        is not duplicate_case.source.messages[0]["content"]["text"]
    )


def test_duplicate_deepcopies_tool_calls(duplicate_case):
    _assert_deepcopied(duplicate_case, "tool_calls")
    assert duplicate_case.session.tool_calls[0] is not duplicate_case.source.tool_calls[0]
    assert (
        duplicate_case.session.tool_calls[0]["args"]["queries"]
        is not duplicate_case.source.tool_calls[0]["args"]["queries"]
    )


# ── Duplicate: intentionally NOT copied ──────────────────────────────────────


def test_duplicate_resets_pinned(duplicate_case):
    assert duplicate_case.source.pinned is True
    assert duplicate_case.session.pinned is False


def test_duplicate_resets_archived(duplicate_case):
    assert duplicate_case.source.archived is True
    assert duplicate_case.session.archived is False


def test_duplicate_does_not_copy_compression_anchor(duplicate_case):
    duplicate = duplicate_case.session
    assert duplicate.compression_anchor_visible_idx is None
    assert duplicate.compression_anchor_message_key is None
    assert duplicate.compression_anchor_summary is None
    assert duplicate.compression_anchor_details == {}
    assert duplicate.compression_anchor_mode is None
    assert duplicate.compression_anchor_engine is None


def test_duplicate_does_not_copy_worktree(duplicate_case):
    duplicate = duplicate_case.session
    assert duplicate.worktree_path is None
    assert duplicate.worktree_branch is None
    assert duplicate.worktree_repo_root is None


def test_duplicate_does_not_copy_last_prompt_tokens(duplicate_case):
    assert duplicate_case.session.last_prompt_tokens is None


def test_duplicate_does_not_copy_ephemeral(duplicate_case):
    duplicate = duplicate_case.session
    assert duplicate.active_stream_id is None
    assert duplicate.pending_user_message is None
    assert duplicate.pending_attachments == []
    assert duplicate.pending_started_at is None


def test_duplicate_has_fresh_identity_without_parent(duplicate_case):
    assert duplicate_case.session.session_id != duplicate_case.source.session_id
    assert duplicate_case.session.parent_session_id is None
    assert duplicate_case.session.session_source is None


def test_duplicate_materializes_durably_before_publication(duplicate_case):
    assert duplicate_case.persist is True
    assert [entry[0] for entry in duplicate_case.timeline] == [
        "materialize",
        "event",
        "response",
    ]
    assert duplicate_case.timeline[1][1] == "session_duplicate"


# ── Branch: critical fields MUST be copied ───────────────────────────────────


def test_branch_copies_model_provider(branch_case):
    _assert_copied(branch_case, "model_provider")


def test_branch_copies_project_id(branch_case):
    _assert_copied(branch_case, "project_id")


def test_branch_copies_personality(branch_case):
    _assert_copied(branch_case, "personality")


def test_branch_copies_enabled_toolsets(branch_case):
    _assert_copied(branch_case, "enabled_toolsets")


def test_branch_copies_context_messages(branch_case):
    _assert_deepcopied(branch_case, "context_messages")
    assert (
        branch_case.session.context_messages[0]["content"]
        is not branch_case.source.context_messages[0]["content"]
    )


def test_branch_copies_gateway_routing(branch_case):
    _assert_deepcopied(branch_case, "gateway_routing")
    assert (
        branch_case.session.gateway_routing["fallbacks"]
        is not branch_case.source.gateway_routing["fallbacks"]
    )


def test_branch_copies_context_length(branch_case):
    _assert_copied(branch_case, "context_length")


def test_branch_copies_threshold_tokens(branch_case):
    _assert_copied(branch_case, "threshold_tokens")


def test_branch_copies_context_engine(branch_case):
    _assert_copied(branch_case, "context_engine")
    _assert_deepcopied(branch_case, "context_engine_state")
    assert (
        branch_case.session.context_engine_state["cursor"]
        is not branch_case.source.context_engine_state["cursor"]
    )


# ── Branch: intentionally NOT copied ─────────────────────────────────────────


def test_branch_does_not_copy_compression_anchor(branch_case):
    branch = branch_case.session
    assert branch.compression_anchor_visible_idx is None
    assert branch.compression_anchor_message_key is None
    assert branch.compression_anchor_summary is None
    assert branch.compression_anchor_details == {}


def test_branch_does_not_copy_truncation_watermark(branch_case):
    assert branch_case.session.truncation_watermark is None


def test_branch_does_not_copy_truncation_boundary(branch_case):
    assert branch_case.session.truncation_boundary is None


def test_branch_does_not_copy_usage_counters(branch_case):
    branch = branch_case.session
    assert branch.input_tokens == 0
    assert branch.output_tokens == 0
    assert branch.estimated_cost is None


def test_branch_does_not_copy_tool_calls(branch_case):
    assert branch_case.session.tool_calls == []


def test_branch_does_not_copy_gateway_routing_history(branch_case):
    assert branch_case.session.gateway_routing_history == []


def test_branch_sets_session_source(branch_case):
    assert branch_case.session.session_source == "fork"
    assert branch_case.session.parent_session_id == branch_case.source.session_id


def test_branch_does_not_copy_ephemeral(branch_case):
    branch = branch_case.session
    assert branch.active_stream_id is None
    assert branch.pending_user_message is None
    assert branch.pending_attachments == []
    assert branch.pending_started_at is None


def test_branch_materializes_nonempty_fork_before_publication(branch_case):
    assert branch_case.persist is True
    assert [entry[0] for entry in branch_case.timeline] == [
        "materialize",
        "event",
        "response",
    ]
    assert branch_case.timeline[1][1] == "session_branch"
