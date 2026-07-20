"""Opus pre-release follow-up for stage-267:

#1454/#1474 split(':')[-1] trailing-empty guard — when a malformed configured model id
has a trailing colon (e.g. `@custom:foo:bar:`), the new normalization would collapse
two distinct ids to the empty string. Defensive `parts[-1] or s` falls back to the
original input so distinct ids stay distinct in the configured-model badge filter.

Mirrors:
  api/config.py        _norm_model_id
  static/ui.js         _normalizeConfiguredModelKey
"""
from api.config.catalog_normalization import normalize_catalog_model_id
from tests.frontend_asset_contract import family_source

UI_JS = family_source("ui")


def _exec_norm():
    """Return the canonical backend catalog normalizer."""
    return normalize_catalog_model_id


def test_norm_model_id_trailing_colon_keeps_original():
    """Malformed @provider: ids with trailing colon must not collapse to empty."""
    norm = _exec_norm()
    # Trailing colon — last split segment is empty, must fall back to original
    out = norm("@custom:foo:bar:")
    assert out, f"trailing-colon collapsed to empty: {out!r}"


def test_norm_model_id_clean_multi_segment_strips_correctly():
    """Clean @custom:vendor:model strips @custom: prefix, preserving hierarchy."""
    norm = _exec_norm()
    assert norm("@custom:jingdong:GLM-5") == "jingdong:glm.5"


def test_norm_model_id_trailing_slash_keeps_original():
    """Same guard on the / branch — trailing slash must not collapse to empty."""
    norm = _exec_norm()
    out = norm("custom/jingdong/")
    assert out, f"trailing-slash collapsed to empty: {out!r}"


def test_norm_model_id_simple_inputs_unchanged():
    """Sanity: simple inputs round-trip as before."""
    norm = _exec_norm()
    assert norm("gpt-4") == "gpt.4"
    assert norm("provider/model-name") == "model.name"
    assert norm("") == ""
    assert norm(None) == ""


def test_ui_js_mirror_has_trailing_empty_guard():
    """Frontend _normalizeConfiguredModelKey must mirror the backend guard."""
    # The colon branch now uses indexOf(':',1)+slice to strip only @provider: prefix
    assert "indexOf(':',1)" in UI_JS, "ui.js no longer uses indexOf-slice pattern for colon branch"
    snippet = UI_JS[UI_JS.find("function _normalizeConfiguredModelKey"):UI_JS.find("function _normalizeConfiguredModelKey") + 1800]
    assert "cand||s" in snippet, "ui.js missing trailing-empty guard `||s` fallback on colon branch"
    # The slash branch now uses replace(/^[^/]+\//, '') instead of split('/').pop()
    # to preserve multi-slash vendor hierarchy (#3360).  Verify the new pattern
    # and its trailing-empty guard (the `||s` suffix).
    assert "replace(/^[^/]+\\/" in snippet, (
        "ui.js slash branch should use replace(/^[^/]+\\//) pattern (#3360)"
    )
    assert "'')||s" in snippet, (
        "ui.js slash branch should have ||s trailing-empty guard (#3360)"
    )
