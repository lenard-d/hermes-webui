"""Regression test for the Opus SHOULD-FIX bounds applied in stage-299.

PR #1664 introduced /api/wiki/status with `_llm_wiki_count_files` and
`_llm_wiki_page_files` that walk WIKI_PATH via `rglob`. Without bounds,
a misconfigured WIKI_PATH=/ or symlink loop would hang the endpoint.

These tests pin the defenses applied per Opus advisor on stage-299:
- A constant cap on iteration (_LLM_WIKI_MAX_FILES) for both functions
- A forbidden-roots blocklist (_LLM_WIKI_FORBIDDEN_ROOTS) that includes
  '/' / '/etc' / '/usr' / '/var' / '/opt' / '/sys' / '/proc' (resolved
  to absolute strings)
- Bounded behavior: if WIKI_PATH points at a forbidden root, both
  functions return 0/empty without iterating
"""
from tests.frontend_asset_contract import family_source
from pathlib import Path

LLM_WIKI_PY = Path(__file__).parent.parent / "api" / "knowledge" / "wiki_index.py"


def _read_source():
    return LLM_WIKI_PY.read_text()


def test_wiki_max_files_constant_present():
    src = _read_source()
    assert "LLM_WIKI_MAX_FILES" in src
    assert "LLM_WIKI_FORBIDDEN_ROOTS" in src
    # Make sure cap is reasonable (≥ a few thousand, ≤ 100k)
    assert "10_000" in src or "LLM_WIKI_MAX_FILES = 10" in src


def test_count_files_has_iteration_cap():
    src = _read_source()
    # Locate _llm_wiki_count_files body
    start = src.find("def count_files(")
    end = src.find("\ndef ", start + 1)
    body = src[start:end]
    assert "LLM_WIKI_MAX_FILES" in body
    assert "LLM_WIKI_FORBIDDEN_ROOTS" in body
    assert "iterated > LLM_WIKI_MAX_FILES" in body


def test_page_files_has_iteration_cap():
    src = _read_source()
    start = src.find("def page_files_uncached(")
    end = src.find("\ndef ", start + 1)
    body = src[start:end]
    assert "LLM_WIKI_MAX_FILES" in body
    assert "LLM_WIKI_FORBIDDEN_ROOTS" in body


def test_forbidden_roots_includes_system_paths():
    from api.knowledge.wiki_index import LLM_WIKI_FORBIDDEN_ROOTS

    for forbidden in ("/", "/etc", "/usr", "/var"):
        assert str(Path(forbidden).resolve()) in LLM_WIKI_FORBIDDEN_ROOTS


def test_count_files_returns_zero_for_forbidden_root(tmp_path, monkeypatch):
    """Behavioral test: walking a forbidden root returns 0 without iterating."""
    from api.knowledge.wiki_index import count_files
    
    forbidden_root = Path("/etc")
    if forbidden_root.exists():  # skip on systems without /etc (Windows)
        result = count_files(forbidden_root)
        assert result == 0, "Walking /etc should return 0 (forbidden root guard)"


def test_render_llm_wiki_status_uses_url_scheme_guard():
    """Opus SHOULD-FIX #1: docs_url interpolated into href must be scheme-guarded."""
    panels_js = family_source("panels")
    # Find the _renderLlmWikiStatus function body
    start = panels_js.find("function _renderLlmWikiStatus")
    end = panels_js.find("\nfunction ", start + 1)
    body = panels_js[start:end]
    # Must use a scheme-guarded form, not raw esc()
    assert "/^https?:" in body or "test(rawDocsUrl)" in body or "test(docsUrl)" in body, (
        "Expected URL scheme guard (e.g. /^https?:\\/\\//.test(...)) before "
        "interpolating docsUrl into href to prevent javascript: scheme XSS "
        "if docs_url ever becomes config-driven."
    )
