from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from tests.frontend_asset_contract import family_asset_paths


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
MODULES_DIR = STATIC / "modules" / "boot"
INDEX = MODULES_DIR / "index.js"


def test_boot_uses_semantic_native_modules_without_legacy_parts():
    assert [path.name for path in family_asset_paths("boot")] == [
        "server-lifecycle.js",
        "run-control.js",
        "speech-capture.js",
        "public-interfaces.js",
        "appearance.js",
        "navigation.js",
        "composer.js",
        "voice-mode.js",
        "legacy-interface.js",
        "index.js",
    ]
    assert not (STATIC / "boot.js").exists()
    assert not (STATIC / "boot_parts").exists()
    assert not any(re.match(r"\d{3}-", path.name) for path in MODULES_DIR.glob("*.js"))


def test_boot_modules_parse_independently_and_declare_interfaces():
    node = shutil.which("node")
    for path in family_asset_paths("boot"):
        source = path.read_text(encoding="utf-8")
        assert source.endswith("\n")
        assert "HermesBoot.begin" not in source
        assert "HermesBoot.publish" not in source
        assert re.search(r"\b(?:import|export)\b", source), path
        if node:
            result = subprocess.run(
                [node, "--check", str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 0, result.stderr


def test_boot_index_imports_owners_and_compatibility_before_coordinating():
    source = INDEX.read_text(encoding="utf-8")
    assert "import './legacy-interface.js';" in source
    legacy_interface = (MODULES_DIR / "legacy-interface.js").read_text(encoding="utf-8")
    assert "from '../compatibility.js'" in legacy_interface
    assert "publishCompatibilityDomain('boot'" in legacy_interface
    for owner in (
        "./navigation.js",
        "./run-control.js",
        "./public-interfaces.js",
        "./appearance.js",
        "./composer.js",
        "./voice-mode.js",
    ):
        assert owner in source
    assert "(async()=>{" in source


def test_index_loads_one_boot_module_entrypoint_after_classic_dependencies():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    marker = 'type="module" src="static/modules/boot/index.js?v=__WEBUI_VERSION__"'
    assert html.count(marker) == 1
    assert html.index('src="static/outline.js?v=__WEBUI_VERSION__"') < html.index(marker)
    assert "static/boot.js" not in html
    assert "static/boot_parts/" not in html


def test_service_worker_precaches_boot_entry_and_unversioned_dependencies():
    worker = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert "'./static/modules/boot/index.js' + VQ" in worker
    for path in family_asset_paths("boot")[:-1]:
        rel = path.relative_to(ROOT).as_posix()
        assert f"'./{rel}'," in worker
    assert "'./static/modules/compatibility.js'," in worker


def test_boot_module_graph_is_statically_valid():
    deno = shutil.which("deno")
    if not deno:
        return
    result = subprocess.run(
        [deno, "check", "--no-config", str(INDEX)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
