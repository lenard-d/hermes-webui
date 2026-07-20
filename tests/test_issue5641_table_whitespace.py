import importlib.util
from pathlib import Path

import pytest

from tests.frontend_asset_contract import family_source


_HELPERS_PATH = Path(__file__).with_name("test_renderer_js_behaviour.py")
_SPEC = importlib.util.spec_from_file_location("issue5641_renderer_helpers", _HELPERS_PATH)
_HELPERS = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_HELPERS)

NODE = _HELPERS.NODE
_DRIVER_SRC = _HELPERS._DRIVER_SRC.replace(
    "const src = JSON.parse(process.argv[2])\n"
    "  .map(path => fs.readFileSync(path, 'utf8'))\n"
    "  .join('');",
    "const src = fs.readFileSync(process.argv[2], 'utf8');",
)


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    directory = tmp_path_factory.mktemp("issue5641_renderer_driver")
    driver = directory / "driver.js"
    source = directory / "ui-browser-order.js"
    driver.write_text(_DRIVER_SRC, encoding="utf-8")
    source.write_text(family_source("ui"), encoding="utf-8")
    return str(driver), str(source)


def _render(driver_paths, markdown: str) -> str:
    driver, source = driver_paths
    result = _HELPERS.subprocess.run(
        [NODE, driver, source],
        input=markdown,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return result.stdout


pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


@pytest.mark.parametrize(
    "src",
    [
        "| a | b | \n|---|---|\n| 1 | 2 |",
        "| a | b |\n|---|---| \n| 1 | 2 |",
        " | a | b |\n |---|---|\n | 1 | 2 |",
        "   | a | b |\n   |---|---|\n   | 1 | 2 |",
    ],
)
def test_table_rows_with_edge_whitespace_still_render_as_table(driver_path, src):
    out = _render(driver_path, src)
    assert "<table><thead>" in out
    assert "<td>1</td>" in out
    assert "<p>" not in out


def test_four_space_indented_table_like_block_stays_outside_table(driver_path):
    src = (
        "    | a | b |\n"
        "    |---|---|\n"
        "    | 1 | 2 |"
    )
    out = _render(driver_path, src)
    assert "<table" not in out
