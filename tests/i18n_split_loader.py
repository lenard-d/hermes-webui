"""Shared paths and source helpers for the split classic-script i18n bundle."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PARTS_DIR = REPO_ROOT / "static" / "i18n_parts"
LOCALES = [
    ("en", "en"),
    ("it", "it"),
    ("ja", "ja"),
    ("ru", "ru"),
    ("es", "es"),
    ("de", "de"),
    ("zh", "zh"),
    ("zh_hant", "zh-Hant"),
    ("pt", "pt"),
    ("ko", "ko"),
    ("fr", "fr"),
    ("cs", "cs"),
    ("tr", "tr"),
    ("pl", "pl"),
    ("vi", "vi"),
]
def i18n_script_paths() -> list[Path]:
    """Return the required deterministic classic-script load order."""

    paths = [REPO_ROOT / "static" / "i18n.js", PARTS_DIR / "helpers.js"]
    paths.extend(PARTS_DIR / f"locale-{slug}.js" for slug, _locale in LOCALES)
    paths.append(PARTS_DIR / "runtime.js")
    return paths


def ordered_i18n_source() -> str:
    """Join scripts for isolated VM tests; production loads them individually."""

    return "\n".join(path.read_text(encoding="utf-8") for path in i18n_script_paths())


def _locale_part_body(path: Path, locale: str) -> str:
    source = path.read_text(encoding="utf-8")
    marker = f"  api.registerLocale({locale!r}, {{\n"
    start = source.index(marker) + len(marker)
    end = source.rindex("\n  });")
    return source[start:end]


def locale_block_source(locale: str) -> str:
    """Return the readable registration object body for one locale."""

    slug = next(slug for slug, code in LOCALES if code == locale)
    return _locale_part_body(PARTS_DIR / f"locale-{slug}.js", locale)


def source_shaped_i18n() -> str:
    """Reconstruct the historical locale literal shape for source-level tests."""

    locale_entries: list[str] = []
    for slug, locale in LOCALES:
        key = f"'{locale}'" if "-" in locale else locale
        locale_entries.append(f"  {key}: {{\n" + locale_block_source(locale) + "\n  },")
    runtime = (PARTS_DIR / "runtime.js").read_text(encoding="utf-8")
    return "const LOCALES = {\n" + "\n".join(locale_entries) + "\n};\n" + runtime
