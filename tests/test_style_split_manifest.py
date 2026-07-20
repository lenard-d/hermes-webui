import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STYLE_BASE = ROOT / "static" / "style.css"
STYLE_PARTS_DIR = ROOT / "static" / "style_parts"
STYLE_MANIFEST = STYLE_PARTS_DIR / "manifest.json"
BASELINE_SHA256 = "780067e3693c819265b0fd0cef46e2c5b42970ac0ad406b732a5a43bed3f638c"
MAX_CSS_LINES = 1200


def style_part_paths() -> list[Path]:
    manifest = json.loads(STYLE_MANIFEST.read_text(encoding="utf-8"))
    return [STYLE_PARTS_DIR / name for name in manifest["parts"]]


def read_style_css() -> str:
    paths = [STYLE_BASE, *style_part_paths()]
    return "".join(path.read_text(encoding="utf-8") for path in paths)


def test_style_manifest_reconstructs_the_pre_split_stylesheet_byte_for_byte():
    reconstructed = read_style_css().encode("utf-8")
    assert hashlib.sha256(reconstructed).hexdigest() == BASELINE_SHA256


def test_style_base_and_every_domain_part_stay_within_1200_lines():
    css_files = [STYLE_BASE, *style_part_paths()]
    oversized = {
        str(path.relative_to(ROOT)): len(path.read_text(encoding="utf-8").splitlines())
        for path in css_files
        if len(path.read_text(encoding="utf-8").splitlines()) > MAX_CSS_LINES
    }
    assert oversized == {}


def test_style_manifest_is_complete_ordered_and_has_no_unlisted_css_parts():
    manifest = json.loads(STYLE_MANIFEST.read_text(encoding="utf-8"))
    assert set(manifest) == {"version", "parts"}
    assert manifest["version"] == 1
    assert all(Path(name).name == name for name in manifest["parts"])

    part_paths = style_part_paths()
    assert part_paths
    assert len(part_paths) == len(set(part_paths))
    assert all(path.parent == STYLE_PARTS_DIR and path.suffix == ".css" for path in part_paths)
    assert set(part_paths) == set(STYLE_PARTS_DIR.glob("*.css"))


def test_every_part_is_a_balanced_top_level_css_fragment():
    for path in style_part_paths():
        text = path.read_text(encoding="utf-8")
        depth = 0
        comment = False
        quote = None
        escaped = False
        index = 0
        while index < len(text):
            char = text[index]
            if comment:
                if char == "*" and index + 1 < len(text) and text[index + 1] == "/":
                    comment = False
                    index += 2
                    continue
            elif quote is not None:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
            elif char == "\\":
                index += 2
                continue
            elif char == "/" and index + 1 < len(text) and text[index + 1] == "*":
                comment = True
                index += 2
                continue
            elif char in {'"', "'"}:
                quote = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                assert depth >= 0, f"unexpected closing brace in {path.name}"
            index += 1
        assert not comment, f"unterminated comment in {path.name}"
        assert quote is None, f"unterminated string in {path.name}"
        assert depth == 0, f"part ends inside a CSS block: {path.name}"


def test_style_base_does_not_create_a_second_runtime_loading_path():
    base = STYLE_BASE.read_text(encoding="utf-8")
    assert "@import" not in base
