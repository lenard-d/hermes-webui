import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULES = [
    ROOT / "static" / "messages.js",
    ROOT / "static" / "messages_parts" / "markdown_tables.js",
    ROOT / "static" / "messages_parts" / "composer_context.js",
    ROOT / "static" / "messages_parts" / "send.js",
    ROOT / "static" / "messages_parts" / "stream_lifecycle.js",
    ROOT / "static" / "messages_parts" / "stream.js",
    ROOT / "static" / "messages_parts" / "composer_approvals.js",
    ROOT / "static" / "messages_parts" / "session_events.js",
    ROOT / "static" / "messages_parts" / "clarify.js",
    ROOT / "static" / "messages_parts" / "notifications_background.js",
]


def test_split_modules_are_complete_and_manifest_free():
    part_dir = ROOT / "static" / "messages_parts"
    assert not (part_dir / "manifest.json").exists()
    assert {path.name for path in MODULES[1:]} == {
        path.name for path in part_dir.glob("*.js")
    }


def test_each_module_is_an_independently_valid_classic_script():
    node = shutil.which("node")
    if node is None:
        return
    for path in MODULES:
        result = subprocess.run(
            [node, "--check", str(path)],
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, f"{path.name}: {result.stderr}"


def test_modules_use_one_namespace_and_keep_compatibility_declarations():
    for path in MODULES:
        source = path.read_text(encoding="utf-8")
        assert "globalThis.HermesMessages = HermesMessages;" in source

    combined = "\n".join(path.read_text(encoding="utf-8") for path in MODULES)
    for declaration in (
        "async function send(){",
        "function attachLiveStream(activeSid, streamId, uploaded=[], options={}){",
        "function startSessionStream(sid)",
        "async function respondClarify(response)",
    ):
        assert declaration in combined

    exported = set(re.findall(r"^  ([A-Za-z_$][\w$]*),?$", combined, re.MULTILINE))
    assert {
        "send",
        "attachLiveStream",
        "startSessionStream",
        "respondApproval",
        "respondClarify",
        "sendBrowserNotification",
    } <= exported


def test_module_sizes_are_reviewable_with_one_documented_stream_exception():
    sizes = {
        path.relative_to(ROOT).as_posix(): len(
            path.read_text(encoding="utf-8").splitlines()
        )
        for path in MODULES
    }
    stream_name = "static/messages_parts/stream.js"
    assert sizes[stream_name] > 1200
    assert sizes[stream_name] < 5000
    assert all(lines <= 1200 for name, lines in sizes.items() if name != stream_name)

    stream_source = (ROOT / stream_name).read_text(encoding="utf-8")
    assert stream_source.count("function attachLiveStream(") == 1
    assert stream_source.rstrip().endswith("});")
    assert "Object.assign(HermesMessages, {\n  attachLiveStream," in stream_source
