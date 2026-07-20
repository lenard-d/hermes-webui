"""Regression test: JS tool-result snippet limit matches the Python backend limit."""
from tests.frontend_asset_contract import family_source
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_tool_snippet_limit_parity():
    py = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
    js = family_source("ui")
    assert "_TOOL_RESULT_SNIPPET_MAX = 4000" in py
    assert ".slice(0,4000)" in js
