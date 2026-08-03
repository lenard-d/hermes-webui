"""Critical-path guards for restoring the first visible conversation."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOOT_JS = (ROOT / "static" / "modules" / "boot" / "index.js").read_text(
    encoding="utf-8"
)


def test_saved_session_restore_does_not_wait_for_workspace_or_onboarding():
    side_tasks = BOOT_JS.index("const _workspaceListReady=loadWorkspaceList();")
    restore = BOOT_JS.index("await loadSession(saved, {preserveActiveInput:true});")
    critical_path = BOOT_JS[side_tasks:restore]

    assert "await _workspaceListReady" not in critical_path
    assert "await _onboardingReady" not in critical_path


def test_noncritical_boot_tasks_handle_failures_in_background():
    side_tasks = BOOT_JS.index("const _workspaceListReady=loadWorkspaceList();")
    session_list = BOOT_JS.index("await renderSessionList();", side_tasks)
    block = BOOT_JS[side_tasks:session_list]

    assert "Promise.allSettled" in block
    assert "console.warn('[boot] background task failed'" in block
