from pathlib import Path
import shutil
import subprocess
import textwrap


ROOT = Path(__file__).resolve().parents[1]
COMPRESSION_UI = ROOT / "static" / "modules" / "ui" / "compression-ui.js"
RENDERER = ROOT / "static" / "modules" / "ui" / "renderer.js"
STREAM_TRANSCRIPT = ROOT / "static" / "modules" / "messages" / "stream-transcript.js"
TERMINAL_EVENTS = ROOT / "static" / "modules" / "messages" / "terminal-events.js"
SESSION_RECOVERY = ROOT / "static" / "modules" / "messages" / "session-recovery.js"


def test_preserved_task_list_marker_only_helper_is_strict():
    src = COMPRESSION_UI.read_text(encoding="utf-8")

    assert "function _isPreservedCompressionTaskListMarkerOnlyText" in src
    assert "_isPreservedCompressionTaskListMarkerText(text)" in src
    assert ".replace(/^\\s*\\[your active task list was preserved across context compression\\]" in src
    assert ".trim()" in src


def test_marker_only_assistant_message_renders_as_error_not_model_text():
    compression_ui = COMPRESSION_UI.read_text(encoding="utf-8")
    renderer = RENDERER.read_text(encoding="utf-8")

    assert "function _isMarkerOnlyAssistantCompressionMessage" in compression_ui
    assert "m.role!=='assistant'" in compression_ui
    assert "_isPreservedCompressionTaskListMarkerOnlyText(text)" in compression_ui
    assert "if(!isUser&&_isMarkerOnlyAssistantCompressionMessage(m))" in renderer
    assert "content='**Error:** No response received after context compression. Please retry.'" in renderer


def test_done_and_restore_replace_marker_only_assistant_with_error_toast():
    node = shutil.which("node")
    if node is None:
        return
    script = textwrap.dedent(
        f"""
        const {{ createStreamTranscriptProjection }} = await import({STREAM_TRANSCRIPT.as_uri()!r});
        const projection = createStreamTranscriptProjection({{
          markerOnlyText: text => /^\\[your active task list was preserved across context compression\\]\\s*$/i.test(text),
        }});
        const messages = [
          {{ role: 'user', content: 'continue' }},
          {{ role: 'assistant', content: '[your active task list was preserved across context compression]' }},
        ];
        const replaced = projection.replaceMarkerOnlyAssistantWithStreamError(messages);
        if (!replaced) throw new Error('marker-only terminal reply was not replaced');
        if (messages[1].content !== '**Error:** No response received after context compression. Please retry.') {{
          throw new Error('terminal replacement text was not rendered');
        }}
        if (!messages[1].provider_details.includes('internal preserved-task-list compression marker')) {{
          throw new Error('replacement did not retain diagnostic provenance');
        }}
        """
    )
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

    terminal_events = TERMINAL_EVENTS.read_text(encoding="utf-8")
    session_recovery = SESSION_RECOVERY.read_text(encoding="utf-8")
    assert "_markerOnlyAssistantError=_replaceMarkerOnlyAssistantWithStreamError(S.messages)" in terminal_events
    assert "_markerOnlyAssistantError=_replaceMarkerOnlyAssistantWithStreamError(S.messages)" in session_recovery
    assert "showToast('No response received after context compression. Please retry.',5000,'error')" in session_recovery
