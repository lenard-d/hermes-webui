"""Regression tests for v0.50.253 Opus pre-release follow-ups.

Three small follow-ups landed alongside the main batch:

1. /branch endpoint rejects non-string session_id with a 400 (instead of
   crashing with a generic 500 from get_session() raising TypeError).
2. /branch endpoint rejects negative keep_count (Python slicing semantics
   would otherwise produce "all but last N" rather than a forward prefix).
3. PR #1342 leaked 9 unused `wiki_*` i18n keys from a different branch.
   These were stripped — assert they don't come back.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parents[1]


# ── 1 + 2: /branch endpoint validation ────────────────────────────────────────


def _branch_validation_response(body, monkeypatch):
    """Invoke the extracted branch-route owner with an observable response."""
    import api.routes as routes
    from api.http.routes import session_mutations

    response = {}
    context = dict(routes.__dict__)
    context["bad"] = lambda _handler, message, status=400: response.update(
        error=message, status=status
    ) or True

    if "keep_count" in body:
        # A negative keep count is validated after resolving the source.  Keep
        # that lookup side-effect free so this regression test reaches the
        # validation boundary without touching persisted session state.
        monkeypatch.setattr(
            session_mutations.foreign_session_access,
            "resolve_branch_source",
            lambda _session_id: SimpleNamespace(refusal=None, session=object()),
        )

    result = session_mutations.handle_post(
        object(), urlparse("/api/session/branch"), body, None, context
    )
    assert result is True
    return response


def test_branch_endpoint_rejects_non_string_session_id(monkeypatch):
    """A malformed ID produces a clear 400 instead of a lookup-time 500."""
    assert _branch_validation_response({"session_id": 123}, monkeypatch) == {
        "error": "session_id must be a string",
        "status": 400,
    }


def test_branch_endpoint_rejects_negative_keep_count(monkeypatch):
    """A negative count cannot reach Python's surprising negative slice path."""
    assert _branch_validation_response(
        {"session_id": "source", "keep_count": -1}, monkeypatch
    ) == {"error": "keep_count must be non-negative", "status": 400}


# ── 3: orphan wiki_* i18n keys must not return ────────────────────────────────


def test_no_orphan_wiki_i18n_keys():
    """PR #1342 leaked 9 unused `wiki_*` keys (wiki_panel_title, wiki_status_label,
    wiki_entry_count, wiki_last_modified, wiki_not_available, wiki_enabled,
    wiki_disabled, wiki_toggle_failed, wiki_panel_desc) into static/i18n.js
    from a different branch. Zero references existed outside i18n.js. They
    were stripped by Opus pre-release follow-up. This test pins that they
    don't return."""
    i18n_src = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
    # If wiki_* keys are added in the future, they MUST have at least one
    # reference outside i18n.js. Until then, this test fails loudly.
    forbidden_keys = [
        "wiki_panel_title",
        "wiki_panel_desc",
        "wiki_status_label",
        "wiki_entry_count",
        "wiki_last_modified",
        "wiki_not_available",
        "wiki_enabled",
        "wiki_disabled",
        "wiki_toggle_failed",
    ]
    for key in forbidden_keys:
        assert key not in i18n_src, (
            f"{key!r} is back in static/i18n.js but no consumer uses it. "
            "If you're adding wiki UI, also wire it up in the JS / panel HTML / "
            "Python so the key is actually used. See v0.50.253 Opus pre-release "
            "review."
        )
