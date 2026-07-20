import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.frontend_asset_contract import family_asset_paths, family_entry_paths


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
MODULE_DIR = STATIC / "modules" / "panels"
INDEX = STATIC / "index.html"
WORKER = STATIC / "sw.js"


def _panel_modules() -> tuple[Path, ...]:
    return family_asset_paths("panels")


def test_panels_use_one_native_module_entry_and_semantic_files():
    modules = _panel_modules()
    names = [path.name for path in modules]

    assert family_entry_paths("panels") == (MODULE_DIR / "index.js",)
    assert set(MODULE_DIR.rglob("*.js")) == set(modules)
    assert not (STATIC / "panels.js").exists()
    assert not (STATIC / "panels_parts").exists()
    assert not any(re.match(r"\d", name) or "part" in name for name in names)
    for required in (
        "cron-list.js",
        "cron-editor.js",
        "kanban-board.js",
        "kanban-tasks.js",
        "kanban-boards.js",
        "settings-state.js",
        "settings-navigation.js",
        "settings-preferences.js",
        "profiles.js",
        "skills-memory.js",
        "workspaces.js",
    ):
        assert required in names

    html = INDEX.read_text(encoding="utf-8")
    assert html.count('type="module" src="static/modules/panels/index.js?v=__WEBUI_VERSION__"') == 1
    assert "panels_parts" not in html


def test_every_panel_module_parses_independently():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for JavaScript syntax checks")

    for module in _panel_modules():
        source = module.read_text(encoding="utf-8")
        assert source.endswith("\n")
        result = subprocess.run(
            [node, "--input-type=module", "--check"],
            input=source,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"{module.name}:\n{result.stderr}"


def test_panel_entry_graph_links_with_explicit_exports():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for JavaScript module link checks")

    script = r"""
const vm = require('vm');
const fs = require('fs');
const path = require('path');
const root = path.resolve(process.argv[1]);
const cache = new Map();
function load(file) {
  file = path.resolve(file);
  if (cache.has(file)) return cache.get(file);
  const module = new vm.SourceTextModule(fs.readFileSync(file, 'utf8'), {identifier: file});
  cache.set(file, module);
  return module;
}
(async () => {
  const entry = load(path.join(root, 'index.js'));
  await entry.link((specifier, referrer) => load(path.resolve(path.dirname(referrer.identifier), specifier)));
  process.stdout.write(String(cache.size));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    result = subprocess.run(
        [node, "--experimental-vm-modules", "-e", script, str(MODULE_DIR)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert int(result.stdout) == len(_panel_modules()) + 1


def test_internal_modules_do_not_publish_or_rebind_globals():
    legacy_interface = (MODULE_DIR / "legacy-interface.js").read_text(encoding="utf-8")
    entrypoint = (MODULE_DIR / "index.js").read_text(encoding="utf-8")
    compatibility = (MODULE_DIR.parent / "compatibility.js").read_text(encoding="utf-8")
    assert "panelLegacyBindings" in legacy_interface
    assert "Object.defineProperty" not in legacy_interface
    assert "_currentPanel" in legacy_interface
    assert "_workspaceList" in legacy_interface
    assert "publishCompatibilityDomain('panels'" in entrypoint
    assert "Object.defineProperty(globalThis" in compatibility

    for module in _panel_modules():
        source = module.read_text(encoding="utf-8")
        assert "window.HermesPanels" not in source
        assert "window.checkWebUIVersionSkew=" not in source
        assert "window._pickChatActivityDisplayMode=" not in source
        assert "window._pickTransparentEventTimestamps=" not in source
        assert not re.search(r"^switchPanel\s*=", source, re.MULTILINE)
        assert not re.search(r"^switchSettingsSection\s*=", source, re.MULTILINE)


def test_service_worker_precaches_entry_and_unversioned_module_imports():
    worker = WORKER.read_text(encoding="utf-8")
    assert "'./static/modules/panels/index.js' + VQ" in worker
    for module in _panel_modules():
        if module.name == "index.js":
            continue
        rel = module.relative_to(ROOT).as_posix()
        assert f"'./{rel}'" in worker


def test_settings_load_orchestrates_cohesive_hydration_steps_in_order():
    source = (MODULE_DIR / "settings-preferences.js").read_text(encoding="utf-8")
    orchestration = source[source.index("export async function loadSettingsPanel") :]
    expected = [
        "const settings=await api('/api/settings')",
        "_loadSettingsAppearance(settings)",
        "await _loadSettingsModelControls(settings)",
        "_loadSettingsPreferences(settings,resolvedLanguage)",
        "_loadSettingsSpeechAndRuntime(settings)",
        "await _loadSettingsAuthentication(settings)",
    ]
    positions = [orchestration.index(marker) for marker in expected]
    assert positions == sorted(positions)
