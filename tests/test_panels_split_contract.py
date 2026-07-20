import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
PARTS_DIR = STATIC / "panels_parts"
MANIFEST = PARTS_DIR / "manifest.json"


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _production_scripts() -> list[Path]:
    return [STATIC / "panels.js", *(PARTS_DIR / name for name in _manifest()["parts"])]


def _read_panels_source() -> str:
    return "".join(path.read_text(encoding="utf-8") for path in _production_scripts())


def test_panels_manifest_is_the_direct_script_load_order():
    manifest = _manifest()
    assert set(manifest) == {"version", "parts"}
    assert manifest["version"] == 1
    assert manifest["parts"]
    assert len(manifest["parts"]) == len(set(manifest["parts"]))
    assert all(Path(name).name == name and name.endswith(".js") for name in manifest["parts"])
    assert sorted(path.name for path in PARTS_DIR.glob("*.js")) == manifest["parts"]


def test_panels_domains_are_readable_standalone_classic_scripts():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for JavaScript syntax checks")

    namespaces = []
    for script in _production_scripts():
        source = script.read_text(encoding="utf-8")
        assert source.endswith("\n")
        assert len(source.splitlines()) <= 1200, script
        result = subprocess.run(
            [node, "--check", str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        matches = re.findall(r"window\.HermesPanels\.([A-Za-z0-9_]+)\s*=", source)
        assert matches, f"{script.name} must publish an explicit domain namespace"
        namespaces.extend(matches)

    assert len(namespaces) == len(set(namespaces))


def test_composed_test_source_matches_direct_load_order():
    expected = "".join(path.read_text(encoding="utf-8") for path in _production_scripts())
    assert _read_panels_source() == expected
    node = shutil.which("node")
    if node:
        result = subprocess.run(
            [node, "--check", "-"],
            input=expected,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def test_settings_load_orchestrates_cohesive_hydration_steps_in_order():
    source = (PARTS_DIR / "012-settings-preferences.js").read_text(encoding="utf-8")
    orchestration = source[source.index("async function loadSettingsPanel") :]
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
