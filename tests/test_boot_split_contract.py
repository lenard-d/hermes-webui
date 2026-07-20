from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
PARTS_DIR = STATIC / "boot_parts"
MANIFEST = PARTS_DIR / "manifest.json"


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _production_scripts() -> list[Path]:
    return [STATIC / "boot.js", *(PARTS_DIR / name for name in _manifest()["parts"])]


def _combined_source() -> str:
    return "".join(path.read_text(encoding="utf-8") for path in _production_scripts())


def test_boot_manifest_is_the_direct_script_load_order():
    manifest = _manifest()
    assert set(manifest) == {"version", "parts"}
    assert manifest["version"] == 1
    assert manifest["parts"] == [
        "001-run-control.js",
        "002-shell-navigation.js",
        "003-speech-capture.js",
        "004-public-interfaces.js",
        "005-conversation-voice.js",
        "006-composer-session-actions.js",
        "007-appearance-preferences.js",
        "008-bootstrap-coordinator.js",
    ]
    assert sorted(path.name for path in PARTS_DIR.glob("*.js")) == manifest["parts"]


def test_boot_parts_are_parseable_and_publish_one_owner_each():
    node = shutil.which("node")
    owners: list[str] = []
    for script in _production_scripts():
        source = script.read_text(encoding="utf-8")
        assert source.endswith("\n")
        assert len(source.splitlines()) <= 900, script
        if node:
            result = subprocess.run(
                [node, "--check", str(script)],
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 0, result.stderr
        if script.parent == PARTS_DIR:
            begin = re.findall(r"window\.HermesBoot\.begin\('([^']+)'\)", source)
            publish = re.findall(r"window\.HermesBoot\.publish\('([^']+)'", source)
            assert len(begin) == 1, script
            assert publish == begin, script
            owners.extend(begin)
    assert len(owners) == len(set(owners))


def test_boot_facade_declares_the_same_runtime_order_as_the_manifest():
    facade = (STATIC / "boot.js").read_text(encoding="utf-8")
    declared = re.findall(r"'([0-9]{3}-[^']+\.js)'", facade)
    assert declared == _manifest()["parts"]
    assert "root.HermesBoot=api;" in facade
    assert "api.begin=function begin" in facade
    assert "api.publish=function publish" in facade
    coordinator = (PARTS_DIR / _manifest()["parts"][-1]).read_text(encoding="utf-8")
    assert coordinator.rstrip().endswith("window.HermesBoot.assertComplete();")


def test_index_loads_boot_facade_and_parts_in_contract_order():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    expected = [
        'src="static/boot.js?v=__WEBUI_VERSION__"',
        *(
            f'src="static/boot_parts/{name}?v=__WEBUI_VERSION__"'
            for name in _manifest()["parts"]
        ),
        'src="static/outline.js?v=__WEBUI_VERSION__"',
    ]
    positions = [html.index(marker) for marker in expected]
    assert positions == sorted(positions)


def test_service_worker_precaches_the_complete_boot_family():
    worker = (STATIC / "sw.js").read_text(encoding="utf-8")
    expected = [
        "'./static/boot.js' + VQ",
        *(f"'./static/boot_parts/{name}' + VQ" for name in _manifest()["parts"]),
    ]
    positions = [worker.index(marker) for marker in expected]
    assert positions == sorted(positions)


def test_full_boot_family_is_syntax_valid(tmp_path):
    node = shutil.which("node")
    if not node:
        return
    combined = tmp_path / "boot-combined.js"
    combined.write_text(_combined_source(), encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(combined)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
