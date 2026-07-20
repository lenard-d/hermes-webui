import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_DIR = ROOT / "static" / "modules" / "messages"
MODULE_NAMES = {
    "anchor-scene.js",
    "approvals.js",
    "clarify.js",
    "composer-context.js",
    "core.js",
    "index.js",
    "live-tools.js",
    "markdown-tables.js",
    "notifications.js",
    "rendering.js",
    "run-journal.js",
    "send.js",
    "session-events.js",
    "stream-lifecycle.js",
    "stream.js",
}


def test_messages_are_one_semantic_native_module_family():
    assert {path.name for path in MODULE_DIR.glob("*.js")} == MODULE_NAMES
    assert not (ROOT / "static" / "messages.js").exists()
    assert not (ROOT / "static" / "messages_parts").exists()
    assert not any(path.name[:1].isdigit() for path in MODULE_DIR.glob("*.js"))


def test_each_messages_module_parses_independently():
    node = shutil.which("node")
    if node is None:
        return
    for path in sorted(MODULE_DIR.glob("*.js")):
        result = subprocess.run(
            [node, "--check", str(path)],
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, f"{path.name}: {result.stderr}"


def test_internal_modules_use_imports_exports_not_classic_assembly():
    compatibility = (MODULE_DIR.parent / "compatibility.js").read_text(encoding="utf-8")
    entrypoint = (MODULE_DIR / "index.js").read_text(encoding="utf-8")
    assert "Object.defineProperty(globalThis" in compatibility
    assert "publishCompatibilityDomain('messages'" in entrypoint
    assert "from '../compatibility.js'" in entrypoint

    for path in MODULE_DIR.glob("*.js"):
        source = path.read_text(encoding="utf-8")
        assert "messages_parts" not in source
        assert "Object.assign(HermesMessages" not in source
        assert "globalThis.HermesMessages" not in source

    stream = (MODULE_DIR / "stream.js").read_text(encoding="utf-8")
    assert "from './stream-lifecycle.js'" in stream
    assert "from './anchor-scene.js'" in stream
    assert "from './live-tools.js'" in stream
    assert "from './rendering.js'" in stream
    assert "from './run-journal.js'" in stream
    assert "HermesMessages.createStream" not in stream


def test_stream_factories_keep_frozen_interfaces_and_fail_closed_validation():
    node = shutil.which("node")
    if node is None:
        return
    runner = r"""
globalThis.document={addEventListener(){},getElementById(){return null},baseURI:'http://localhost/'};
globalThis.window=globalThis;
globalThis.addEventListener=()=>{};
globalThis.location={href:'http://localhost/'};
const api=await import('./static/modules/messages/index.js');
for(const name of ['createStreamRunJournalCursor','createStreamLiveToolTracker','createStreamRenderer']){
  if(!Object.isFrozen(api[name]())) throw new Error('unfrozen interface: '+name);
}
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", runner],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    stream = (MODULE_DIR / "stream.js").read_text(encoding="utf-8")
    validation = stream.index("const _requiredStreamFactories")
    mutation = stream.index("_bindStreamHiddenTracker();")
    assert validation < mutation
    assert "required stream modules must load before attachLiveStream" in stream


def test_page_and_service_worker_use_the_native_entrypoint_contract():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    worker = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
    assert (
        '<script type="module" '
        'src="static/modules/messages/index.js?v=__WEBUI_VERSION__"></script>'
    ) in html
    assert "static/messages.js" not in html
    assert "static/messages_parts/" not in html
    assert "'./static/modules/messages/index.js' + VQ" in worker
    for name in MODULE_NAMES - {"index.js"}:
        assert f"'./static/modules/messages/{name}'" in worker
    assert "static/messages_parts/" not in worker


def test_module_sizes_keep_the_cohesive_stream_owner_intact():
    sizes = {
        path.name: len(path.read_text(encoding="utf-8").splitlines())
        for path in MODULE_DIR.glob("*.js")
    }
    assert 1200 < sizes["stream.js"] < 5000
    assert all(lines <= 1200 for name, lines in sizes.items() if name != "stream.js")
    stream = (MODULE_DIR / "stream.js").read_text(encoding="utf-8")
    assert stream.count("function attachLiveStream(") == 1
