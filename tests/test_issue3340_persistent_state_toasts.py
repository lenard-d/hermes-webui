from tests.frontend_asset_contract import family_source

import logging
from pathlib import Path
from types import SimpleNamespace

from api.runs.local_success import publish_persistent_state_changes
from api.runs.runtime_resolution import (
    _persistent_state_changes,
    _persistent_state_snapshot,
)

ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = family_source("messages")
LIVE_TOOLS_JS = (
    ROOT / "static" / "modules" / "messages" / "live-tools.js"
).read_text(encoding="utf-8")
LOCAL_CONVERSATION_PY = (ROOT / "api" / "runs" / "local_conversation.py").read_text(encoding="utf-8")
LOCAL_STREAMING_PY = (ROOT / "api" / "runs" / "local.py").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def _tool_complete_listener_block() -> str:
    start = LIVE_TOOLS_JS.index("source.addEventListener('tool_complete'")
    end = LIVE_TOOLS_JS.index("return source;", start)
    return LIVE_TOOLS_JS[start:end]


def test_tool_complete_notifies_on_persistent_state_writes():
    assert "function _maybeNotifyPersistentStateSaved(tool)" in MESSAGES_JS
    block = _tool_complete_listener_block()

    assert "notifyPersistentStateSaved(toolCall);" in block
    notify_idx = block.index("notifyPersistentStateSaved(toolCall);")
    assert block.index("toolCall.is_error=!!payload.is_error;") < notify_idx
    assert block.index("if(!ownsVisibleStream()) return;") < notify_idx
    assert notify_idx < block.index("refreshOpenPreview();")


def test_persistent_state_toast_classifier_is_write_only_and_deduped():
    helper_start = MESSAGES_JS.index("function _persistentToastHasWriteIntent")
    helper_end = MESSAGES_JS.index("function _persistentToastSkillName", helper_start)
    helper = MESSAGES_JS[helper_start:helper_end]

    assert "read|list|view|search|lookup|get|fetch|load|usage|toggle|delete|remove" in helper
    assert "save|saved|write|wrote|written|update|updated|create|created|store|stored|persist|persisted|remember|remembered" in helper
    assert "_persistentStateToastSeen.has(dedupeKey)" in MESSAGES_JS
    assert "_persistentStateToastSeen.add(dedupeKey)" in MESSAGES_JS
    assert "_showPersistentStateToast(isSkill?'skill':'memory'" in MESSAGES_JS
    assert "if(isSkill&&!skillName)return;" in MESSAGES_JS


def test_persistent_state_toasts_use_existing_user_visible_labels():
    notify_start = MESSAGES_JS.index("function _maybeNotifyPersistentStateSaved")
    notify_end = MESSAGES_JS.index("function _selectedTextReplyT", notify_start)
    notify = MESSAGES_JS[notify_start:notify_end]

    assert "t('memory_saved')" in notify
    assert "t('skill_created')" in notify
    assert "t('skill_updated')" in notify
    assert "showToast(itemName?`${base}: ${itemName}`:base,4200,'success')" in notify
    assert "showToast(t('memory_saved'),3600,'success')" in notify


def test_backend_emits_state_saved_sse_from_file_snapshots(tmp_path):
    before = _persistent_state_snapshot(str(tmp_path))
    memory_file = tmp_path / "memories" / "MEMORY.md"
    skill_file = tmp_path / "skills" / "demo" / "SKILL.md"
    memory_file.parent.mkdir(parents=True)
    skill_file.parent.mkdir(parents=True)
    memory_file.write_text("remember this", encoding="utf-8")
    skill_file.write_text("# Demo", encoding="utf-8")

    changes = _persistent_state_changes(
        before,
        _persistent_state_snapshot(str(tmp_path)),
    )

    assert changes == {
        "memory_saved": True,
        "skills": [{"name": "demo", "path": "demo/SKILL.md", "action": "created"}],
    }
    events = []
    publish_persistent_state_changes(
        session=SimpleNamespace(session_id="toast-session"),
        session_id="toast-session",
        profile_home=str(tmp_path),
        before=before,
        publish=lambda event, payload: events.append((event, payload)),
        logger=logging.getLogger(__name__),
    )

    assert events == [
        ("state_saved", {"session_id": "toast-session", "kind": "memory", "action": "saved"}),
        (
            "state_saved",
            {
                "session_id": "toast-session",
                "kind": "skill",
                "action": "created",
                "name": "demo",
            },
        ),
    ]
    # Snapshot ownership moved into LocalConversation during the local-run
    # split; local.py must still pass that immutable pre-turn state through to
    # the success writeback owner.
    assert "persistent_state_before=_persistent_state_snapshot(profile_home)" in LOCAL_CONVERSATION_PY
    assert "publish_persistent_state_changes(" in LOCAL_STREAMING_PY
    assert "before=_persistent_state_before" in LOCAL_STREAMING_PY


def test_frontend_handles_state_saved_sse_and_reuses_dedupe():
    start = MESSAGES_JS.index("source.addEventListener('state_saved'")
    end = MESSAGES_JS.index("source.addEventListener('title'", start)
    block = MESSAGES_JS[start:end]

    assert "showPersistentStateToast(payload.kind,payload.name||''" in block
    assert "String(payload.action||'').toLowerCase()==='created'" in block
    assert "if(!belongsToOwner(payload)) return;" in block
    assert "'state_saved'" in MESSAGES_JS


def test_issue_3340_changelog_entry_present():
    assert "#3340" in CHANGELOG
    assert "saved memory" in CHANGELOG
    assert "created/updated a skill" in CHANGELOG
