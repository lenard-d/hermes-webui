from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC = REPO_ROOT / "static"
PARTS_DIR = STATIC / "workspace_parts"
MANIFEST = PARTS_DIR / "manifest.json"


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _production_scripts() -> list[Path]:
    return [
        STATIC / "workspace.js",
        *(PARTS_DIR / name for name in _manifest()["parts"]),
    ]


def test_workspace_manifest_pins_the_direct_classic_script_order():
    manifest = _manifest()
    assert manifest == {
        "version": 1,
        "parts": [
            "001-navigation.js",
            "002-preview-editor.js",
            "003-upload.js",
        ],
    }
    assert sorted(path.name for path in PARTS_DIR.glob("*.js")) == manifest["parts"]


def test_workspace_scripts_are_individually_and_combined_parseable(tmp_path):
    scripts = _production_scripts()
    for script in scripts:
        result = subprocess.run(
            ["node", "--check", str(script)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{script.relative_to(REPO_ROOT)}: {result.stderr}"

    combined = tmp_path / "workspace-combined.js"
    combined.write_text(
        "".join(script.read_text(encoding="utf-8") for script in scripts),
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", "--check", str(combined)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_workspace_facade_declares_parts_without_eagerly_reading_them():
    facade = (STATIC / "workspace.js").read_text(encoding="utf-8")
    declared = re.findall(r"'[0-9]{3}-[^']+\.js'", facade)
    assert [item.strip("'") for item in declared] == _manifest()["parts"]
    assert "workspace.parts=workspace.parts||Object.create(null);" in facade
    assert "workspace.transport=Object.freeze({api,recordClientSSEError});" in facade
    for module_name in ("navigation", "previewEditor", "upload"):
        assert f"workspace.parts.{module_name}" not in facade


def test_workspace_family_loads_in_index_and_service_worker_order():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    worker = (STATIC / "sw.js").read_text(encoding="utf-8")
    scripts = _production_scripts()

    html_markers = [
        f'src="static/{script.relative_to(STATIC).as_posix()}?v=__WEBUI_VERSION__"'
        for script in scripts
    ]
    worker_markers = [
        f"'./static/{script.relative_to(STATIC).as_posix()}' + VQ"
        for script in scripts
    ]
    html_positions = [html.index(marker) for marker in html_markers]
    worker_positions = [worker.index(marker) for marker in worker_markers]
    assert html_positions == sorted(html_positions)
    assert worker_positions == sorted(worker_positions)


def test_workspace_classic_load_order_installs_owners_and_compatibility_globals():
    runner = r"""
const fs=require('fs');
const vm=require('vm');
const context=vm.createContext({
  console,
  setTimeout,
  clearTimeout,
  URL,
  URLSearchParams,
  Blob,
  TextEncoder,
});
context.window=context;

for(const path of process.argv.slice(1)){
  vm.runInContext(fs.readFileSync(path,'utf8'),context,{filename:path});
}

const workspace=context.HermesWorkspace;
if(!workspace) throw new Error('missing HermesWorkspace facade');
for(const name of ['navigation','previewEditor','upload']){
  if(!Object.isFrozen(workspace.parts[name])) throw new Error('missing frozen owner: '+name);
}
if(context.loadDir!==workspace.parts.navigation.loadDir) throw new Error('navigation global drifted');
if(context.openFile!==workspace.parts.previewEditor.openFile) throw new Error('preview global drifted');
if(context.triggerWorkspaceUpload!==workspace.parts.upload.triggerWorkspaceUpload) throw new Error('upload global drifted');
if(context.api!==workspace.transport.api) throw new Error('transport global drifted');
if(context._normalizeWorkspaceRelPath('../../etc/passwd')!=='') throw new Error('parent escape normalized permissively');
if(context._normalizeWorkspaceRelPath('./docs/../README.md')!=='README.md') throw new Error('relative path normalization drifted');
"""
    result = subprocess.run(
        ["node", "-e", runner, *(str(path) for path in _production_scripts())],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_workspace_owner_modules_keep_state_and_cleanup_closures_coherent():
    navigation = (PARTS_DIR / "001-navigation.js").read_text(encoding="utf-8")
    preview = (PARTS_DIR / "002-preview-editor.js").read_text(encoding="utf-8")
    upload = (PARTS_DIR / "003-upload.js").read_text(encoding="utf-8")

    assert "let _wsTreeGen = 0;" in navigation
    assert "treeGen!==_wsTreeGen" in navigation
    assert "_workspaceEscapeGrantForPath" in navigation
    assert "_clearWorkspaceEscapeGrant" in navigation
    assert "let _previewCurrentPath = '';" in preview
    assert "let _previewDirty = false;" in preview
    assert "function cancelEditMode()" in preview
    assert "_previewRawContentPath = _previewCurrentPath;" in preview
    assert "_clearWorkspaceEscapeGrant(grant.path);" in preview
    assert "_bindWorkspaceOsUploadDropTarget" in upload
    assert "_clearWorkspaceOsUploadDragOver" in upload
