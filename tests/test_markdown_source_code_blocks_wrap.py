from pathlib import Path
from tests.frontend_asset_contract import family_source


ROOT = Path(__file__).resolve().parents[1]
UI_JS = family_source("ui")
CSS = family_source("style")


def test_markdown_source_code_blocks_get_dedicated_pre_class_in_both_renderers():
    expected = "const preClass=/^(md|markdown|mdx)$/.test(lang)?' class=\"md-source-block\"':'';"

    assert UI_JS.count(expected) >= 2, "Expected both fenced-code renderers to tag markdown source blocks"


def test_markdown_source_code_blocks_have_desktop_wrap_css():
    assert ".msg-body pre.md-source-block,.preview-md pre.md-source-block{white-space:pre-wrap;overflow-x:hidden;overflow-wrap:anywhere;}" in CSS
    assert ".msg-body pre.md-source-block code,.preview-md pre.md-source-block code{display:block;white-space:inherit;overflow-wrap:anywhere;word-break:break-word;}" in CSS
    assert ".msg-body pre.md-source-block code .token,.preview-md pre.md-source-block code .token{white-space:inherit;overflow-wrap:anywhere;word-break:inherit;}" in CSS
