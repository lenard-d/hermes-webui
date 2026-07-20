pytest_plugins = ("tests.test_renderer_js_behaviour",)

import pytest

from tests.frontend_asset_contract import family_source
from tests import test_renderer_js_behaviour as renderer_behaviour
from tests.test_renderer_js_behaviour import _render


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    """Run the shared renderer harness against the complete split UI family."""
    tmp_dir = tmp_path_factory.mktemp("inline_code_renderer")
    driver = tmp_dir / "driver.js"
    ui_source = tmp_dir / "ui-family.js"
    driver.write_text(renderer_behaviour._DRIVER_SRC, encoding="utf-8")
    ui_source.write_text(family_source("ui"), encoding="utf-8")
    original = renderer_behaviour.UI_JS_PATH
    renderer_behaviour.UI_JS_PATH = ui_source
    try:
        yield str(driver)
    finally:
        renderer_behaviour.UI_JS_PATH = original


def test_inline_code_inside_link_label_renders_as_code(driver_path):
    out = _render(driver_path, "[`8c64957`](https://github.com/x/y)")
    assert '<a href="https://github.com/x/y"' in out
    assert "<code>8c64957</code>" in out
    assert "&lt;code&gt;" not in out


def test_list_item_link_label_keeps_inline_code(driver_path):
    out = _render(driver_path, "- [`8c64957`](https://github.com/x/y)")
    assert "<li>" in out
    assert "<code>8c64957</code>" in out
    assert "&lt;code&gt;" not in out


def test_unknown_raw_html_inside_link_label_is_escaped_once(driver_path):
    out = _render(driver_path, "[<script>alert(1)</script>](https://github.com/x/y)")
    assert "<script>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out
    assert "&amp;lt;script" not in out
