"""Behavior coverage for #1710's workspace-tree rename tooltip.

Folders navigate when their name is double-clicked; only file rows may advertise
the rename action.  Exercise the native workspace-tree owner with a small DOM
harness so this regression stays tied to rendered row behavior rather than the
former concatenated ``ui`` source layout.
"""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


NODE = shutil.which("node")
TREE_MODULE = Path(__file__).resolve().parents[1] / "static" / "modules" / "ui" / "workspace-tree.js"


pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _render_tree_rows(entries: list[dict]) -> dict:
    """Render real workspace-tree rows and return their user-visible state."""
    payload = {"source": TREE_MODULE.read_text(encoding="utf-8"), "entries": entries}
    script = "const params = " + json.dumps(payload) + ";\n" + r"""
const vm = require('node:vm');

const element = () => ({
  attributes: {}, children: [], className: '', dataset: {}, innerHTML: '',
  onclick: null, ondblclick: null, oncontextmenu: null, ondragstart: null,
  ondragend: null, style: {}, textContent: '', title: '',
  classList: { add() {}, remove() {} },
  appendChild(child) { this.children.push(child); return child; },
  removeAttribute(name) { delete this.attributes[name]; },
  setAttribute(name, value) { this.attributes[name] = String(value); },
});

const document = { createElement: () => element() };
const loadDirCalls = [];
const sandbox = {
  console, Set, document,
  IMAGE_EXTS: new Set(), MD_EXTS: new Set(),
  S: { _dirCache: {}, _expandedDirs: new Set(), currentDir: '.', session: { session_id: 'test' } },
  api: async () => ({}),
  deleteWorkspaceDir() {}, deleteWorkspaceFile() {},
  fileExt: () => '', li: () => '', openFile() {},
  loadDir(path) { loadDirCalls.push(path); },
  showConfirmDialog: async () => {}, showToast() {},
  t: (key) => ({ double_click_rename: 'Double-click to rename' }[key] || key),
  _bindWorkspaceMoveDropTarget() {}, _bindWorkspaceOsUploadDropTarget() {},
  _clearWorkspaceMoveDragOver() {}, _clearWsDragData() {}, _setWsDragData() {},
  _showFileContextMenu() {}, _visibleWorkspaceEntries: (items) => items,
};
sandbox.globalThis = sandbox;

const executable = params.source
  .replace(/^import\s+[\s\S]*?;\s*$/gm, '')
  .replace(/^export\s*\{[\s\S]*?\};\s*$/gm, '');
vm.runInNewContext(executable + '\nglobalThis.renderTreeItemsUnderTest = _renderTreeItems;', sandbox, {
  filename: 'workspace-tree.js',
});

const container = element();
sandbox.renderTreeItemsUnderTest(container, params.entries, 0);
const rows = container.children.map((row) => {
  const name = row.children.find((child) => child.className === 'file-name');
  return { name: name.textContent, title: name.title, ondblclick: name.ondblclick };
});
for (const row of rows.filter((row) => row.name === 'folder')) {
  row.ondblclick({ stopPropagation() {} });
}
console.log(JSON.stringify({
  titles: Object.fromEntries(rows.map((row) => [row.name, row.title])),
  loadDirCalls,
}));
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(f"workspace-tree Node harness failed: {result.stderr}")
    return json.loads(result.stdout)


def test_only_file_rows_advertise_double_click_rename():
    rendered = _render_tree_rows([
        {"name": "folder", "path": "folder", "type": "dir"},
        {"name": "readme.md", "path": "readme.md", "type": "file"},
    ])

    assert rendered["titles"] == {
        "folder": "",
        "readme.md": "Double-click to rename",
    }


def test_folder_double_click_keeps_its_navigation_behavior():
    rendered = _render_tree_rows([
        {"name": "folder", "path": "folder", "type": "dir"},
    ])

    assert rendered["loadDirCalls"] == ["folder"]
