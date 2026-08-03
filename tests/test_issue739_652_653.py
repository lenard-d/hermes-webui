"""
Tests for streaming error handling fixes:
  #739 — quota/credit exhaustion detected as distinct error type + persisted to session
  #652 — context compaction session_id rotation: stream_end uses original session_id
  #653 — bad tool call hang: same stream_end fix applies

All static tests (no live server required).
"""
import re
import pathlib

from api.runs.message_sanitization import _sanitize_messages_for_api
from api.runs.provider_errors import _classify_provider_error

FAILURE_OWNER = pathlib.Path(__file__).parent.parent / 'api' / 'runs' / 'local_failures.py'
SUCCESS_OWNER = pathlib.Path(__file__).parent.parent / 'api' / 'runs' / 'local_success.py'
TITLE_GENERATION = pathlib.Path(__file__).parent.parent / 'api' / 'runs' / 'title_generation' / 'lifecycle.py'
TERMINAL_EVENTS = pathlib.Path(__file__).parent.parent / 'static' / 'modules' / 'messages' / 'terminal-events.js'

failure_owner_src = FAILURE_OWNER.read_text(encoding='utf-8')
success_owner_src = SUCCESS_OWNER.read_text(encoding='utf-8')
title_generation_src = TITLE_GENERATION.read_text(encoding='utf-8')
terminal_events_src = TERMINAL_EVENTS.read_text(encoding='utf-8')


# ── #739: Quota exhaustion detection ─────────────────────────────────────────

class TestQuotaDetection:
    """Quota-exhausted errors must be classified separately from rate limits."""

    def test_quota_patterns_present_in_silent_failure_path(self):
        """All supported provider texts classify as quota exhaustion."""
        for message in (
            'insufficient credit',
            'credit balance depleted',
            'credits exhausted',
            'quota_exceeded',
            'exceeded your current quota',
        ):
            assert _classify_provider_error(message)['type'] == 'quota_exhausted'

    def test_quota_type_emitted_as_quota_exhausted(self):
        """The apperror type is 'quota_exhausted', not 'error' or 'rate_limit'."""
        assert _classify_provider_error('insufficient credit')['type'] == 'quota_exhausted'

    def test_quota_checked_before_rate_limit(self):
        """Quota check must appear before the rate-limit check in the exception path.
        OpenAI billing 429s overlap with rate-limit patterns."""
        assert _classify_provider_error('HTTP 429 more credits')['type'] == 'quota_exhausted'

    def test_rate_limit_excludes_quota(self):
        """Rate-limit detection must be guarded so quota errors don't also match."""
        assert _classify_provider_error('HTTP 429 rate limit')['type'] == 'rate_limit'

    def test_js_quota_label_present(self):
        """messages.js renders a 'quota_exhausted' apperror with a distinct label."""
        assert "quota_exhausted" in terminal_events_src
        assert "Out of credits" in terminal_events_src


# ── #739: Error persistence across reload ─────────────────────────────────────

class TestErrorPersistence:
    """Errors must be saved to the session so they survive page reload."""

    def test_silent_failure_appends_error_message(self):
        """Silent-failure path appends an _error-marked message before returning."""
        # The shared failure owner materializes every persisted terminal error.
        assert "error_message = {" in failure_owner_src
        assert '"_error": True' in failure_owner_src
        assert "session.messages.append(error_message)" in failure_owner_src

    def test_silent_failure_calls_save_before_return(self):
        """save() must be called after appending the error message."""
        # The same constructed error marker is appended before session save.
        block_start = failure_owner_src.index("error_message = {")
        block_end = failure_owner_src.index("def _record_process_wakeup_pause", block_start)
        block = failure_owner_src[block_start:block_end]
        error_marker_pos = block.index('"_error": True')
        append_pos = block.index("session.messages.append(error_message)")
        save_pos = block.index("session.save()", append_pos)

        assert error_marker_pos < append_pos < save_pos, (
            "save() must be called after appending the error message in the "
            "silent-failure path"
        )

    def test_exception_path_appends_error_message(self):
        """Exception path also persists the error to the session."""
        # Terminal completion and exception handling use the same persistence owner.
        terminal_start = failure_owner_src.index("def inspect_terminal_result")
        exception_start = failure_owner_src.index("def handle_exception", terminal_start)
        exception_end = failure_owner_src.index("def _retry_with_fresh_credentials", exception_start)
        assert "self._persist_error(" in failure_owner_src[terminal_start:exception_start]
        assert "self._persist_error(" in failure_owner_src[exception_start:exception_end]

    def test_sanitize_skips_error_messages(self):
        """_sanitize_messages_for_api must not send _error messages to the LLM."""
        messages = [
            {'role': 'assistant', 'content': 'provider failure', '_error': True},
            {'role': 'user', 'content': 'continue'},
        ]

        assert _sanitize_messages_for_api(messages) == [
            {'role': 'user', 'content': 'continue'},
        ]


# ── #652/#653: Context compaction stream_end fix ──────────────────────────────

class TestStreamEndSessionId:
    """stream_end must use the original session_id param, not s.session_id."""

    def test_non_bg_title_stream_end_uses_session_id_param(self):
        """When no background title is spawned, stream_end should use original session_id."""
        # The fixed code: put('stream_end', {'session_id': session_id})
        # Not: put('stream_end', {'session_id': s.session_id})
        # Verify the pattern appears in the non-background-title branch
        assert 'publish("stream_end", {"session_id": session_id})' in success_owner_src

    def test_background_title_thread_stream_end_uses_session_id_param(self):
        """Background title thread also emits stream_end with original session_id."""
        # In _run_background_title_update: put_event('stream_end', {'session_id': session_id})
        # The session_id param is passed from the caller with the original value
        assert "put_event('stream_end', {'session_id': session_id})" in title_generation_src

    def test_s_session_id_not_used_in_stream_end(self):
        """s.session_id (which may be rotated after compaction) must not appear in stream_end."""
        # Find all stream_end emissions and verify none use s.session_id
        for source in (success_owner_src, title_generation_src):
            matches = re.finditer(r"(?:publish|put_event)\(['\"]stream_end['\"],[^)]+\)", source)
            for match in matches:
                assert 's.session_id' not in match.group(), \
                    f"stream_end uses s.session_id (may be rotated): {match.group()}"

    def test_title_event_uses_original_session_id(self):
        """title event in background title thread uses original session_id, not s.session_id."""
        # Client guard: if((d.session_id||activeSid)!==activeSid) return;
        # So title must be emitted with the original id
        assert "put_event('title', {'session_id': session_id," in title_generation_src
