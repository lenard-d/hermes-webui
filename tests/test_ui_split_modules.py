import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "static"
PARTS_DIR = STATIC_DIR / "ui_parts"


def _module_paths():
    return sorted(PARTS_DIR.glob("*.js"))


def _combined_source():
    paths = [STATIC_DIR / "ui.js", *_module_paths()]
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def test_ui_modules_have_an_explicit_stable_load_order():
    paths = _module_paths()
    prefixes = [int(path.name[:3]) for path in paths]

    assert len(paths) == 20
    assert prefixes == list(range(1, len(paths) + 1))
    assert all(re.fullmatch(r"\d{3}-[a-z0-9-]+\.js", path.name) for path in paths)


def test_ui_modules_are_individually_parseable_classic_scripts():
    for path in _module_paths():
        result = subprocess.run(
            ["node", "--check", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{path.name}: {result.stderr}"


def test_ui_modules_register_explicit_compatibility_apis():
    namespaces = []

    for path in _module_paths():
        source = path.read_text(encoding="utf-8")
        matches = re.findall(r"window\.HermesUI\.register\('([^']+)'", source)
        assert len(matches) == 1, f"{path.name} must register exactly one module API"
        namespaces.extend(matches)

    assert len(namespaces) == len(set(namespaces))


def test_ui_module_sizes_remain_reviewable():
    for path in _module_paths():
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        if path.name == "017-message-renderer.js":
            # renderMessages is kept whole to preserve its lifecycle invariants.
            assert line_count <= 1600
        else:
            assert line_count <= 1300, f"{path.name} has grown beyond a cohesive module"

    facade_lines = len((STATIC_DIR / "ui.js").read_text(encoding="utf-8").splitlines())
    assert facade_lines <= 500


def test_full_ui_load_order_is_syntax_valid(tmp_path):
    combined_path = tmp_path / "ui-combined.js"
    combined_path.write_text(_combined_source(), encoding="utf-8")

    result = subprocess.run(
        ["node", "--check", str(combined_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_adjacent_worklog_modules_register_in_browser_load_order():
    runner = r"""
const fs = require('fs');
const vm = require('vm');
const modules = Object.create(null);
const compat = Object.create(null);
const context = vm.createContext({console});
context.window = context;
context.HermesUI = {
  modules,
  compat,
  register(name, exports) {
    modules[name] = exports;
    Object.assign(compat, exports);
  },
};

for (const path of process.argv.slice(1)) {
  vm.runInContext(fs.readFileSync(path, 'utf8'), context, {filename: path});
}

if (typeof modules.transparentWorklog.ensureLiveWorklogContainer !== 'function') {
  throw new Error('transparentWorklog module did not register its worklog owner');
}
if (typeof modules.anchorScenes.ensureRunActivityGroup !== 'function') {
  throw new Error('anchorScenes module did not register its run-activity owner');
}
if (compat.ensureRunActivityGroup !== modules.anchorScenes.ensureRunActivityGroup) {
  throw new Error('run-activity compatibility export is not owned by anchorScenes');
}
"""
    result = subprocess.run(
        [
            "node",
            "-e",
            runner,
            str(PARTS_DIR / "013-transparent-worklog.js"),
            str(PARTS_DIR / "014-anchor-scenes.js"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_compatibility_surface_keeps_primary_entry_points():
    source = _combined_source()

    for declaration in (
        "function renderModelDropdown(",
        "function renderMd(",
        "function renderMessages(",
        "function buildToolCard(",
        "function renderFileTree(",
        "function uploadPendingFiles(",
    ):
        assert source.count(declaration) == 1

    facade = (STATIC_DIR / "ui.js").read_text(encoding="utf-8")
    assert "window.HermesUI.modules" in facade
    assert "window.HermesUI.compat" in facade
