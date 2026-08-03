"""Regression tests for #852 — thinking card must not mirror the main response.

The `_streamDisplay()` function in messages.js had an early return
`if(reasoningText) return raw` that bypassed think-block stripping when
the reasoning SSE event had populated `reasoningText`. Providers that emit
reasoning via BOTH `on_reasoning` AND `<think>` tags in the token stream
then showed identical content in the thinking card and the main response.
"""
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STREAM_CONTENT_JS = (ROOT / "static" / "modules" / "messages" / "stream-content.js").read_text(encoding="utf-8")
CORE_JS = (ROOT / "static" / "modules" / "messages" / "core.js").read_text(encoding="utf-8")


def _inline_extractor_body(js):
    start = js.index("function _extractInlineThinkingFromContent(")
    end = js.index("if(typeof window", start)
    return js[start:end]


class TestStreamDisplayStripsThinkBlocksAlways:

    def test_early_return_on_reasoning_text_is_gone(self):
        """Regression guard: the bypass that caused the thinking card to
        mirror the main response must stay removed."""
        m = re.search(r'function _streamDisplay\(\)\{.*?\n  \}', STREAM_CONTENT_JS, re.DOTALL)
        assert m, "_streamDisplay not found"
        fn = m.group(0)
        assert "if(reasoningText) return raw" not in fn, (
            "The early-return `if(reasoningText) return raw;` must remain "
            "removed (#852) — it caused the thinking card to mirror the main "
            "response when providers emit <think> tags AND reasoning SSE events."
        )

    def test_think_pair_stripping_still_runs(self):
        """The shared inline extractor must still strip think blocks."""
        m = re.search(r'function _parseStreamState\(\)\{.*?\n  \}', STREAM_CONTENT_JS, re.DOTALL)
        assert m
        fn = m.group(0)
        assert "_extractInlineThinkingFromContent" in fn
        helper = _inline_extractor_body(CORE_JS)
        assert "_thinkPairs" in helper
        assert "text.startsWith(candidate.open,index)" in helper

    def test_still_handles_incomplete_think_tag_partial_prefix(self):
        """Existing behaviour preserved: partial `<thi`, `<think` prefixes
        must still be suppressed so users don't see them mid-stream."""
        m = re.search(r'function _parseStreamState\(\)\{.*?\n  \}', STREAM_CONTENT_JS, re.DOTALL)
        assert m
        fn = m.group(0)
        assert "_extractInlineThinkingFromContent" in fn
        helper = _inline_extractor_body(CORE_JS)
        assert "candidate.open.startsWith(rest)" in helper
