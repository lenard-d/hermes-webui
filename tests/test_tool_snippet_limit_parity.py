"""Regression test: JS tool-result snippet limit matches the Python backend limit."""
from tests.frontend_asset_contract import family_source


def test_tool_snippet_limit_parity():
    from api.streaming import _tool_result_snippet

    result = _tool_result_snippet("x" * 4001)
    js = family_source("ui")
    assert len(result) == 4000
    assert ".slice(0,4000)" in js
