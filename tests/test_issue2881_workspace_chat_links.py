"""Regression coverage for workspace:// chat links opening workspace preview (#2881)."""

from __future__ import annotations

from tests.frontend_asset_contract import family_asset_paths, family_source
import json

import shutil
import subprocess
from pathlib import Path

import pytest

def _family_path_arg(family: str) -> str:
    return json.dumps([str(path) for path in family_asset_paths(family)])

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS = family_source("ui")
MESSAGES_JS = family_source("messages")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_DRIVER_SRC = r"""
const fs = require('fs');
const src = JSON.parse(process.argv[2]).map(p=>fs.readFileSync(p, 'utf8')).join('');
global.window = {};
global.document = { createElement: () => ({ innerHTML: '', textContent: '' }) };
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const _IMAGE_EXTS=/\.(png|jpg|jpeg|gif|webp|bmp|ico|avif)$/i;
const _SVG_EXTS=/\.svg$/i;
const _AUDIO_EXTS=/\.(mp3|ogg|wav|m4a|aac|flac|wma|opus|webm)$/i;
const _VIDEO_EXTS=/\.(mp4|webm|mkv|mov|avi|ogv|m4v)$/i;

function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}
eval(extractFunc('_matchBacktickFenceLine'));
eval(extractFunc('_isBacktickFenceClose'));
eval(extractFunc('renderMd'));

let buf = '';
process.stdin.on('data', c => { buf += c; });
process.stdin.on('end', () => { process.stdout.write(renderMd(buf)); });
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("issue2881_renderer") / "driver.js"
    path.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(path)


def _render(driver_path: str, markdown: str) -> str:
    result = subprocess.run(
        [NODE, driver_path, _family_path_arg("ui")],
        input=markdown,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout


def test_render_md_rewrites_workspace_links_to_internal_anchor(driver_path):
    html = _render(driver_path, "[Open plan](workspace://notes/plan.md)")

    assert 'href="#workspace=notes%2Fplan.md"' in html
    assert "workspace://notes/plan.md" not in html
    assert ">Open plan</a>" in html


def test_render_md_does_not_autolink_raw_workspace_urls(driver_path):
    html = _render(driver_path, "Open workspace://notes/plan.md manually")

    assert '<a href="#workspace=' not in html
    assert "workspace://notes/plan.md" in html


def test_workspace_link_click_delegate_opens_workspace_preview():
    assert 'a[href^="#workspace="]' in UI_JS
    assert "decodeURIComponent" in UI_JS
    assert "openArtifactPath(rel)" in UI_JS
    workspace = family_source("workspace")
    assert "async function openArtifactPath(path)" in workspace
    assert "/api/list?session_id=" in workspace
    assert "file_open_failed" in workspace


def test_streaming_markdown_rewrites_workspace_links_before_sanitizing():
    assert "function _smdLinkHref" in MESSAGES_JS
    assert "workspace:\\/\\/" in MESSAGES_JS
    assert "'#workspace='" in MESSAGES_JS
    assert "_smdLinkHref(v)" in MESSAGES_JS
    assert "_smdLinkHref(value)" in MESSAGES_JS
