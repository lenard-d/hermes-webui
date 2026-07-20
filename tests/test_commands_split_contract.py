from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from tests.frontend_asset_contract import family_asset_paths


REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC = REPO_ROOT / "static"
PARTS_DIR = STATIC / "command_parts"
MANIFEST = PARTS_DIR / "manifest.json"
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")


def _part_paths() -> tuple[Path, ...]:
    return family_asset_paths("commands")[:-1]


def test_commands_manifest_pins_the_direct_load_order():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    part_paths = _part_paths()

    assert manifest == {
        "version": 1,
        "parts": [path.name for path in part_paths],
    }
    assert len(part_paths) == 4
    assert len(part_paths) == len(set(part_paths))
    assert [path.name for path in part_paths] == sorted(path.name for path in part_paths)


def test_command_parts_and_facade_are_individually_parseable():
    for path in family_asset_paths("commands"):
        result = subprocess.run(
            ["node", "--check", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{path.relative_to(REPO_ROOT)}: {result.stderr}"


def test_command_parts_register_cohesive_namespaced_interfaces():
    expected = {
        "001-desktop-companion.js": "desktopCompanion",
        "002-manual-compression.js": "manualCompression",
        "003-run-controls.js": "runControls",
        "004-session-history.js": "sessionHistory",
    }

    for path in _part_paths():
        source = path.read_text(encoding="utf-8")
        module = expected[path.name]
        assert (
            f"globalThis.HermesCommands.parts.{module}=Object.freeze(" in source
        ), f"{path.name} must publish its owned interface"


def test_commands_facade_loads_after_all_parts_and_before_messages():
    asset_names = [
        f"static/command_parts/{path.name}?v=__WEBUI_VERSION__"
        for path in _part_paths()
    ]
    asset_names.extend(
        [
            "static/commands.js?v=__WEBUI_VERSION__",
            "static/messages.js?v=__WEBUI_VERSION__",
        ]
    )
    positions = [INDEX.index(name) for name in asset_names]
    assert positions == sorted(positions)

    facade = (STATIC / "commands.js").read_text(encoding="utf-8")
    match = re.search(
        r"commands\.loadOrder=Object\.freeze\(\[(.*?)\]\);",
        facade,
        re.DOTALL,
    )
    assert match is not None
    assert re.findall(r"'([^']+\.js)'", match.group(1)) == [
        path.name for path in _part_paths()
    ]
    assert "Object.assign(root,commands.api);" in facade


def test_commands_family_installs_registry_and_compatibility_interface():
    runner = r"""
const fs=require('fs');
const vm=require('vm');
const context=vm.createContext({
  console,
  setTimeout,
  clearTimeout,
  URLSearchParams,
  t:key=>key,
});
context.window=context;
context.document={getElementById(){return null;}};

for(const path of process.argv.slice(1)){
  vm.runInContext(fs.readFileSync(path,'utf8'),context,{filename:path});
}

if(!context.HermesCommands||context.HermesCommands.registry.length<20){
  throw new Error('command registry was not installed');
}
for(const name of ['desktopCompanion','manualCompression','runControls','sessionHistory']){
  if(!context.HermesCommands.parts[name]){
    throw new Error('missing command part: '+name);
  }
}
if(context.handlePetSlashCommand!==context.HermesCommands.parts.desktopCompanion.handlePetSlashCommand){
  throw new Error('desktop companion compatibility global drifted');
}
if(context.resumeManualCompressionForSession!==context.HermesCommands.parts.manualCompression.resumeManualCompressionForSession){
  throw new Error('manual compression compatibility global drifted');
}
if(context._trySteer!==context.HermesCommands.parts.runControls.trySteer){
  throw new Error('run-control compatibility global drifted');
}
if(context.forkFromMessage!==context.HermesCommands.parts.sessionHistory.forkFromMessage){
  throw new Error('session-history compatibility global drifted');
}
"""
    result = subprocess.run(
        [
            "node",
            "-e",
            runner,
            *(str(path) for path in family_asset_paths("commands")),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_command_modules_keep_cohesive_reviewable_sizes():
    limits = {
        "commands.js": 1400,
        "001-desktop-companion.js": 200,
        "002-manual-compression.js": 350,
        "003-run-controls.js": 650,
        "004-session-history.js": 400,
    }
    for path in family_asset_paths("commands"):
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        assert line_count <= limits[path.name], (
            f"{path.relative_to(REPO_ROOT)} has {line_count} lines; "
            f"expected at most {limits[path.name]}"
        )
