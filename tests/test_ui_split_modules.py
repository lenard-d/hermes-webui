import re
import subprocess

from tests.frontend_asset_contract import UI_ENTRYPOINT, UI_MODULE_DIR, ui_module_paths


REQUIRED_MODULES = {
    "state.js",
    "navigation.js",
    "model-state.js",
    "model-catalog.js",
    "model-picker-rendering.js",
    "model-selection.js",
    "reasoning-effort.js",
    "composer-footer-fit.js",
    "composer-menu-registry.js",
    "composer.js",
    "composer-primary-control.js",
    "composer-queue.js",
    "composer-state.js",
    "markdown-renderer.js",
    "toast-notifications.js",
    "composer-controls.js",
    "toolsets-controls.js",
    "mobile-composer-config.js",
    "message-scroll-follow.js",
    "activity-timing.js",
    "agent-health-monitor.js",
    "session-recovery.js",
    "system-health-monitor.js",
    "update-banner.js",
    "update-lifecycle.js",
    "update-summary.js",
    "topbar-presentation.js",
    "assistant-turn-presentation.js",
    "activity-presentation.js",
    "presentation.js",
    "render-support.js",
    "renderer.js",
    "live-turn-preservation.js",
    "settled-activity-renderer.js",
    "settled-turn-finalization.js",
}


def test_ui_uses_semantic_native_modules_without_numbered_parts():
    paths = ui_module_paths()
    names = {path.name for path in paths}

    assert REQUIRED_MODULES <= names
    assert all(not re.match(r"\d", name) for name in names)
    assert not (UI_MODULE_DIR.parents[1] / "ui_parts").exists()
    assert not (UI_MODULE_DIR.parents[1] / "ui.js").exists()


def test_ui_modules_are_individually_parseable():
    for path in (UI_ENTRYPOINT, *ui_module_paths()):
        result = subprocess.run(
            ["node", "--check", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{path.name}: {result.stderr}"


def test_ui_modules_declare_explicit_interfaces_and_dependencies():
    for path in ui_module_paths():
        source = path.read_text(encoding="utf-8")
        assert "export {" in source
        assert "compatibilityBindings" in source
        assert "window.HermesUI.register" not in source
        assert "ui_parts" not in source

        for target in re.findall(r"from './([^']+\.js)'", source):
            assert (UI_MODULE_DIR / target).is_file(), f"{path.name}: missing {target}"


def test_ui_entrypoint_is_the_complete_module_inventory():
    source = UI_ENTRYPOINT.read_text(encoding="utf-8")
    imports = re.findall(r"from './([^']+\.js)'", source)

    assert sorted(imports) == sorted(path.name for path in ui_module_paths())
    assert len(imports) == len(set(imports))
    assert "exposeCompatibilityBinding" in source
    assert "hermes-ui-ready" in source


def test_cohesive_ui_owners_remain_reviewable():
    for path in ui_module_paths():
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        assert line_count <= 1500, f"{path.name} has grown beyond a cohesive owner"


def test_primary_ui_interfaces_have_one_authoritative_owner():
    sources = {path.name: path.read_text(encoding="utf-8") for path in ui_module_paths()}
    expected = {
        "syncTopbar": "topbar-presentation.js",
        "_assistantMessageHasVisibleContent": "assistant-turn-presentation.js",
        "_decorateTransparentEventRow": "activity-presentation.js",
        "renderModelDropdown": "model-selection.js",
        "_applyToolsetsChip": "toolsets-controls.js",
        "openComposerContextMenu": "mobile-composer-config.js",
        "_resetStreamScrollFollow": "message-scroll-follow.js",
        "_formatTurnDuration": "activity-timing.js",
        "pollAgentHealth": "agent-health-monitor.js",
        "getPendingSessionMessage": "session-recovery.js",
        "pollSystemHealth": "system-health-monitor.js",
        "_showUpdateBanner": "update-banner.js",
        "applyUpdates": "update-lifecycle.js",
        "showWhatsNewSummary": "update-summary.js",
        "renderMd": "markdown-renderer.js",
        "renderMessages": "renderer.js",
        "buildToolCard": "tool-worklog.js",
        "renderFileTree": "workspace-tree.js",
        "uploadPendingFiles": "upload-transport.js",
    }

    for function_name, owner in expected.items():
        declaration = f"function {function_name}("
        matches = [name for name, source in sources.items() if declaration in source]
        assert matches == [owner]


def test_native_ui_entrypoint_installs_live_classic_compatibility_bindings():
    runner = r"""
    const { pathToFileURL } = require('node:url');
globalThis.window = globalThis;
globalThis.CustomEvent = class CustomEvent { constructor(type){ this.type = type; } };
globalThis.addEventListener = () => {};
globalThis.dispatchEvent = () => true;
globalThis.navigator = { onLine: true };
globalThis.location = { href: 'http://localhost/', origin: 'http://localhost/' };
globalThis.localStorage = { getItem(){ return null; }, setItem(){}, removeItem(){} };
globalThis.document = {
  readyState: 'loading',
  baseURI: 'http://localhost/',
  addEventListener(){},
  getElementById(){ return null; },
  querySelector(){ return null; },
  querySelectorAll(){ return []; },
  createElement(){ return {style:{},classList:{add(){},remove(){},toggle(){}},appendChild(){},setAttribute(){},querySelector(){return null;},querySelectorAll(){return [];}}; },
};
globalThis.MutationObserver = class { observe(){} disconnect(){} };
globalThis.IntersectionObserver = class { observe(){} disconnect(){} };
globalThis.ResizeObserver = class { observe(){} disconnect(){} };
globalThis.fetch = async () => ({ ok: false, status: 503, json: async () => ({}) });
globalThis.matchMedia = () => ({ matches: false, addEventListener(){}, removeEventListener(){} });
(async () => {
await import(pathToFileURL(process.argv[1]));
if (!window.HermesUI?.ready) throw new Error('HermesUI did not become ready');
if (typeof window.renderMessages !== 'function') throw new Error('renderer compatibility binding missing');
if (window.S !== window.HermesUI.modules.state.S) throw new Error('state binding is not live');
window._messageRenderWindowSize = 321;
if (window.HermesUI.modules.state._messageRenderWindowSize !== 321) throw new Error('mutable compatibility binding is not live');
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        ["node", "-e", runner, str(UI_ENTRYPOINT)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
