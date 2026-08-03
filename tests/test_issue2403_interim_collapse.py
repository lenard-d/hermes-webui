"""Static-analysis tests for #2403 — collapse old interim progress notes.

When more than INTERIM_COLLAPSE_THRESHOLD interim_assistant events arrive in
one turn, earlier rendered blocks are hidden behind a toggle so the viewport
stays focused on the latest progress note.

These tests pin the structural invariants without a live browser, using the
same static-analysis pattern as test_issue2713_streaming_segment_flush.py.
"""
import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent


def read(rel):
    return (REPO / rel).read_text(encoding="utf-8")


# These contracts have distinct owners after the messages family split. Keep
# each assertion on the module that owns the behavior rather than rebuilding a
# non-executable family-wide source string.
CONTENT_EVENTS_JS = read("static/modules/messages/content-events.js")
CORE_JS = read("static/modules/messages/core.js")
INTERIM_CSS = read("static/style_parts/011-logs-rtl-outline.css")


def _extract_interim_handler(src):
    """Return the full interim_assistant SSE handler body."""
    start_pattern = "source.addEventListener('interim_assistant'"
    start = src.index(start_pattern)
    end_marker = "\n    });"
    pos = start
    while True:
        idx = src.index(end_marker, pos + 1)
        if idx > start + len(start_pattern) + 20:
            return src[start : idx + len(end_marker)]
        pos = idx


def _visible_interim_flush_pos(fn):
    attr_pos = fn.index("setAttribute('data-interim','1')")
    candidates = [
        "renderer.flushPendingSegment({force:true,skipAnchorProcessProse:true})",
        "renderer.flushPendingSegment({force:true})",
    ]
    positions = [fn.find(candidate, attr_pos) for candidate in candidates]
    positions = [pos for pos in positions if pos != -1]
    assert positions, "visible interim flush call not found after data-interim marker"
    return min(positions)


class TestInterimCollapseHandlerStructure:
    """The interim_assistant handler must contain the collapse threshold and logic."""

    def test_collapse_threshold_constant_present(self):
        fn = _extract_interim_handler(CONTENT_EVENTS_JS)
        assert "collapseThreshold" in fn, (
            "interim_assistant handler must define a collapse threshold "
            "to avoid scattered magic numbers"
        )

    def test_threshold_is_three(self):
        fn = _extract_interim_handler(CONTENT_EVENTS_JS)
        # Constant must be assigned to 3
        assert re.search(r"const\s+collapseThreshold\s*=\s*3\b", fn), (
            "collapseThreshold must be set to 3"
        )

    def test_turn_owned_interim_snippet_count_comparison(self):
        fn = _extract_interim_handler(CONTENT_EVENTS_JS)
        assert "turn.interimSnippetCount()" in fn, (
            "collapse guard must query the turn-owned interim snippet count"
        )
        assert "collapseThreshold" in fn, (
            "collapse guard must reference collapseThreshold, not a magic number"
        )

    def test_interim_data_attribute_set(self):
        fn = _extract_interim_handler(CONTENT_EVENTS_JS)
        assert "data-interim" in fn, (
            "interim_assistant handler must mark each segment with data-interim "
            "so collapse logic can query them"
        )

    def test_interim_collapsed_class_applied(self):
        fn = _extract_interim_handler(CONTENT_EVENTS_JS)
        assert "interim-collapsed" in fn, (
            "collapse logic must apply the interim-collapsed CSS class to hide old blocks"
        )

    def test_collapse_toggle_element_created(self):
        fn = _extract_interim_handler(CONTENT_EVENTS_JS)
        assert "interim-collapse-toggle" in fn, (
            "collapse logic must create an .interim-collapse-toggle element"
        )

    def test_toggle_text_references_count(self):
        fn = _extract_interim_handler(CONTENT_EVENTS_JS)
        # Toggle label must be dynamic: "Show N earlier update(s)"
        assert "earlier update" in fn, (
            "collapse toggle text must reference 'earlier update' so the count is visible"
        )

    def test_attribute_set_before_flush(self):
        fn = _extract_interim_handler(CONTENT_EVENTS_JS)
        attr_pos = fn.index("setAttribute('data-interim','1')")
        flush_pos = _visible_interim_flush_pos(fn)
        assert attr_pos < flush_pos, (
            "data-interim attribute must be set before _flushPendingSegmentRender "
            "so the segment is marked before it is sealed"
        )

    def test_collapse_after_flush_before_reset(self):
        fn = _extract_interim_handler(CONTENT_EVENTS_JS)
        flush_pos = _visible_interim_flush_pos(fn)
        collapse_pos = fn.index("collapseThreshold")
        reset_pos = fn.index("renderer.resetAssistantSegment()", collapse_pos)
        assert flush_pos < collapse_pos < reset_pos, (
            "collapse logic must run after flush but before renderer.resetAssistantSegment"
        )


class TestInterimCollapseCSS:
    """CSS must define both .interim-collapsed and .interim-collapse-toggle."""

    def test_interim_collapsed_rule_present(self):
        css = INTERIM_CSS
        assert ".interim-collapsed" in css, (
            "style.css must define .interim-collapsed to hide collapsed blocks"
        )

    def test_interim_collapsed_uses_display_none(self):
        css = INTERIM_CSS
        m = re.search(r"\.interim-collapsed\s*\{[^}]*\}", css)
        assert m, ".interim-collapsed rule not found in style.css"
        rule = m.group(0)
        assert "display" in rule and "none" in rule, (
            ".interim-collapsed must set display:none"
        )

    def test_collapse_toggle_rule_present(self):
        css = INTERIM_CSS
        assert ".interim-collapse-toggle" in css, (
            "style.css must define .interim-collapse-toggle"
        )

    def test_collapse_toggle_has_cursor_pointer(self):
        css = INTERIM_CSS
        # Extract the first .interim-collapse-toggle rule block
        m = re.search(r"\.interim-collapse-toggle\s*\{[^}]*\}", css)
        assert m, ".interim-collapse-toggle rule not found"
        rule = m.group(0)
        assert "cursor" in rule and "pointer" in rule, (
            ".interim-collapse-toggle must set cursor:pointer"
        )

    def test_collapse_toggle_hover_rule_present(self):
        css = INTERIM_CSS
        assert ".interim-collapse-toggle:hover" in css, (
            "style.css must define a :hover rule for .interim-collapse-toggle"
        )


class TestInterimCollapseSurvivesLiveTurnRestore:
    """Regression guard (#3574 deep-review, Codex catch): the collapse toggle
    must use a DELEGATED document-level click handler, NOT a per-element
    addEventListener. The live turn's DOM is snapshotted/restored via
    outerHTML/innerHTML on session switch (snapshotLiveTurnHtmlForSession /
    restoreLiveTurnHtmlForSession), which strips element-attached listeners.
    A per-element listener would leave a restored toggle inert and collapsed
    interim notes permanently unreachable for the rest of the turn.
    """

    def test_delegated_document_click_handler_present(self):
        src = CORE_JS
        assert "function _interimCollapseDelegatedClick" in src, (
            "interim-collapse toggle must be handled by the delegated "
            "_interimCollapseDelegatedClick handler so it survives a live-turn "
            "DOM restore (innerHTML rebuild strips per-element listeners)."
        )
        assert "document.addEventListener('click', _interimCollapseDelegatedClick)" in src, (
            "the delegated interim-collapse handler must be registered at the "
            "document level (not on the toggle element)."
        )

    def test_delegated_handler_resolves_toggle_via_closest(self):
        src = CORE_JS
        handler_start = src.index("function _interimCollapseDelegatedClick")
        handler = src[handler_start:handler_start + 900]
        assert ".closest('.interim-collapse-toggle')" in handler, (
            "delegated handler must resolve the clicked toggle via "
            "closest('.interim-collapse-toggle') so clicks on the toggle (or "
            "its children) route correctly."
        )

    def test_toggle_creation_does_not_attach_per_element_listener(self):
        """The toggle-creation block must NOT bind a click listener directly on
        the element — that is the bug the delegated handler replaces."""
        handler = _extract_interim_handler(CONTENT_EVENTS_JS)
        assert "toggle.addEventListener('click'" not in handler, (
            "toggle must not use a per-element click listener (lost on live-turn "
            "DOM restore); use the delegated document handler instead."
        )

    def test_toggle_carries_threshold_data_attribute(self):
        """State must live in the DOM (data-threshold) so the stateless delegated
        handler works on both freshly-created and restored toggles."""
        assert "toggle.dataset.threshold" in CONTENT_EVENTS_JS, (
            "toggle must carry data-threshold so the delegated handler can "
            "recompute the collapse set without closure state after a restore."
        )
