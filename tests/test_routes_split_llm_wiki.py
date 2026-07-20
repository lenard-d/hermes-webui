"""Compatibility checks for the LLM Wiki route-domain extraction."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from api import routes
from api.routes_parts import llm_wiki


REPO = Path(__file__).resolve().parents[1]

_LLM_WIKI_FUNCTION_EXPORTS = (
    "_llm_wiki_active_hermes_home",
    "_llm_wiki_env_file_path",
    "_llm_wiki_get_config_path_value",
    "_llm_wiki_config_path",
    "_llm_wiki_resolve_path",
    "_llm_wiki_safe_iso",
    "_llm_wiki_count_files",
    "_llm_wiki_page_files_cache_signature",
    "_llm_wiki_page_files_uncached",
    "_llm_wiki_page_files",
    "_llm_wiki_clear_page_files_cache",
    "_llm_wiki_allowlisted_entries",
    "_llm_wiki_status_file_entry_stat",
    "_llm_wiki_verified_status_file_stat",
    "_llm_wiki_last_writer",
    "_build_llm_wiki_status",
    "_handle_llm_wiki_status",
    "_handle_llm_wiki_browse",
    "_handle_llm_wiki_page",
)


def test_llm_wiki_part_declares_the_complete_cohesive_owner():
    assert llm_wiki.__routes_exports__ == (
        "_LLM_WIKI_DOCS_URL",
        "_LLM_WIKI_PAGE_DIRS",
        "_LLM_WIKI_MAX_FILES",
        "_LLM_WIKI_MAX_PAGE_BYTES",
        "_LLM_WIKI_FORBIDDEN_ROOTS",
        "_WIKI_ALLOWLIST_TTL",
        "_wiki_allowlist_cache",
        "_wiki_allowlist_cache_lock",
        *_LLM_WIKI_FUNCTION_EXPORTS,
    )


def test_llm_wiki_functions_remain_owned_by_routes_facade():
    for name in _LLM_WIKI_FUNCTION_EXPORTS:
        route_export = getattr(routes, name)
        assert route_export.__module__ == "api.routes"
        assert route_export.__globals__ is vars(routes)


def test_llm_wiki_status_resolves_facade_monkeypatches_at_call_time(monkeypatch, tmp_path):
    missing = tmp_path / "missing-wiki"
    monkeypatch.setattr(
        routes,
        "_llm_wiki_resolve_path",
        lambda: (missing, "test-monkeypatch", True),
    )

    status = routes._build_llm_wiki_status()

    assert status["status"] == "missing"
    assert status["path_source"] == "test-monkeypatch"
    assert status["path_configured"] is True


def test_llm_wiki_mutable_cache_state_is_facade_owned(monkeypatch):
    replacement = {"sentinel": {"files": ()}}
    monkeypatch.setattr(routes, "_wiki_allowlist_cache", replacement)

    routes._llm_wiki_clear_page_files_cache()

    assert replacement == {}
    assert routes._wiki_allowlist_cache_lock is llm_wiki._wiki_allowlist_cache_lock


def test_llm_wiki_part_imports_without_loading_routes_facade():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import api.routes_parts.llm_wiki as owner; "
                "assert owner.__routes_exports__; "
                "assert 'api.routes' not in sys.modules"
            ),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_llm_wiki_owner_stops_before_insights_domain():
    owner_source = Path(llm_wiki.__file__).read_text(encoding="utf-8")

    assert "def _handle_insights(" not in owner_source
    assert "_handle_insights" not in llm_wiki.__routes_exports__
    assert routes._handle_insights.__module__ == "api.routes"
