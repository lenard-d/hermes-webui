"""Regression test: JS tool-result snippet limit matches the Python backend limit."""
import json
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_tool_snippet_limit_parity():
    from api.runs.tool_events import _tool_result_snippet

    result = _tool_result_snippet("x" * 4001)
    node = shutil.which("node")
    if not node:
        return
    probe = """
globalThis.window = globalThis;
globalThis.window.addEventListener = () => {};
globalThis.window.removeEventListener = () => {};
globalThis.matchMedia = () => ({matches:false, addEventListener(){}, removeEventListener(){}});
globalThis.document = {addEventListener(){}, removeEventListener(){}, getElementById(){ return null; }};
const {_cliToolResultSnippet} = await import('./static/modules/ui/cli-tool-presentation.js');
process.stdout.write(JSON.stringify({length:_cliToolResultSnippet('x'.repeat(4001)).length}));
"""
    node_result = subprocess.run(
        [node, "--input-type=module", "-e", probe],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert node_result.returncode == 0, node_result.stderr
    js = json.loads(node_result.stdout)
    assert len(result) == 4000
    assert js["length"] == len(result)
