from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from tests.frontend_asset_contract import family_asset_paths


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
MODULES_DIR = STATIC / "modules" / "commands"
INDEX = MODULES_DIR / "index.js"


def test_commands_use_semantic_native_modules_without_legacy_parts():
    assert [path.name for path in family_asset_paths("commands")] == [
        "forced-skill-directive.js",
        "capability-commands.js",
        "desktop-companion.js",
        "manual-compression.js",
        "model-command.js",
        "preference-commands.js",
        "run-controls.js",
        "session-history.js",
        "workspace-commands.js",
        "command-catalog.js",
        "remote-command-catalog.js",
        "slash-autocomplete.js",
        "command-dropdown.js",
        "registry.js",
        "index.js",
    ]
    assert not (STATIC / "commands.js").exists()
    assert not (STATIC / "command_parts").exists()
    assert not any(re.match(r"\d{3}-", path.name) for path in MODULES_DIR.glob("*.js"))


def test_command_modules_parse_independently_and_export_interfaces():
    node = shutil.which("node")
    for path in family_asset_paths("commands"):
        source = path.read_text(encoding="utf-8")
        assert source.endswith("\n")
        assert "HermesCommands.parts" not in source
        assert "export" in source
        if node:
            result = subprocess.run(
                [node, "--check", str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 0, result.stderr


def test_registry_composes_domain_owners_and_exports_the_command_interface():
    source = (MODULES_DIR / "registry.js").read_text(encoding="utf-8")
    for owner in (
        "./command-catalog.js",
        "./command-dropdown.js",
        "./manual-compression.js",
        "./remote-command-catalog.js",
        "./run-controls.js",
        "./slash-autocomplete.js",
        "./session-history.js",
    ):
        assert owner in source
    assert "const commandInterface=Object.freeze({" in source
    assert "Object.assign(root" not in source

    catalog = (MODULES_DIR / "command-catalog.js").read_text(encoding="utf-8")
    assert "const COMMANDS=[" in catalog
    assert "function parseCommand(" in catalog
    assert "function executeCommand(" in catalog


def test_command_modules_follow_the_directed_owner_graph():
    catalog = (MODULES_DIR / "command-catalog.js").read_text(encoding="utf-8")
    remote = (MODULES_DIR / "remote-command-catalog.js").read_text(encoding="utf-8")
    autocomplete = (MODULES_DIR / "slash-autocomplete.js").read_text(encoding="utf-8")
    dropdown = (MODULES_DIR / "command-dropdown.js").read_text(encoding="utf-8")

    assert "./command-catalog.js" in remote
    assert "./remote-command-catalog.js" in autocomplete
    assert "./slash-autocomplete.js" in dropdown
    assert "remote-command-catalog.js" not in catalog
    assert "command-dropdown.js" not in autocomplete


def test_commands_are_loaded_transitively_by_the_single_boot_entrypoint():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    legacy_interface = (STATIC / "modules" / "boot" / "legacy-interface.js").read_text(encoding="utf-8")
    assert "static/commands.js" not in html
    assert "static/command_parts/" not in html
    assert "../commands/index.js" in legacy_interface


def test_compatibility_seam_documents_and_exports_remaining_global_callers():
    seam = (STATIC / "modules" / "compatibility.js").read_text(encoding="utf-8")
    source = (STATIC / "modules" / "boot" / "legacy-interface.js").read_text(encoding="utf-8")
    assert "Temporary classic-script compatibility seam" in seam
    assert "Object.defineProperty(globalThis" in seam
    assert "publishCompatibilityDomain('commands'" in source
    assert "publishCompatibilityDomain('boot'" in source
    for name in (
        "COMMANDS",
        "handlePetSlashCommand",
        "resumeManualCompressionForSession",
        "_trySteer",
        "undoLastExchange",
        "forkFromMessage",
        "invalidateSlashSkillCaches",
        "_invalidateSlashModelCache",
    ):
        assert name in source


def test_command_module_graph_is_statically_valid():
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
