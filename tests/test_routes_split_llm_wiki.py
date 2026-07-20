"""Architecture checks for the knowledge-backed LLM Wiki HTTP adapter."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from api import knowledge, routes
from api.knowledge import wiki_index
from api.routes_parts import llm_wiki

REPO = Path(__file__).resolve().parents[1]

FUNCTION_EXPORTS = (
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


def test_llm_wiki_adapter_preserves_legacy_exports_without_rebinding():
    assert llm_wiki.__routes_exports__ == (
        "_LLM_WIKI_DOCS_URL",
        "_LLM_WIKI_PAGE_DIRS",
        "_LLM_WIKI_MAX_FILES",
        "_LLM_WIKI_MAX_PAGE_BYTES",
        "_LLM_WIKI_FORBIDDEN_ROOTS",
        "_WIKI_ALLOWLIST_TTL",
        "_wiki_allowlist_cache",
        "_wiki_allowlist_cache_lock",
        *FUNCTION_EXPORTS,
    )
    for name in llm_wiki.__routes_exports__:
        assert getattr(routes, name) is getattr(llm_wiki, name)
    assert routes._handle_llm_wiki_page.__module__ == "api.routes_parts.llm_wiki"
    assert routes._handle_llm_wiki_page.__globals__ is vars(llm_wiki)


def test_llm_wiki_domain_interface_owns_index_and_read_behavior():
    assert routes._llm_wiki_resolve_path is knowledge.resolve_wiki_path
    assert routes._llm_wiki_page_files is wiki_index.page_files
    assert routes._build_llm_wiki_status is knowledge.build_wiki_status
    assert routes._wiki_allowlist_cache is wiki_index._page_cache


def test_knowledge_package_imports_without_loading_routes_facade():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import api.knowledge; assert 'api.routes' not in sys.modules",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_llm_wiki_adapter_is_transport_only():
    source = Path(llm_wiki.__file__).read_text(encoding="utf-8")
    assert "knowledge.build_wiki_status" in source
    assert "knowledge.browse_wiki_pages" in source
    assert "knowledge.read_wiki_page" in source
    assert "rglob(" not in source
    assert "os.open(" not in source
    assert "sys.modules" not in source


def test_knowledge_domain_has_no_reverse_route_dependency():
    package_dir = Path(knowledge.__file__).parent
    for path in package_dir.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "api.routes" not in source
        assert "routes_parts" not in source


def test_wiki_env_file_resolution_is_scoped_to_the_selected_profile(tmp_path):
    first_home = tmp_path / "first"
    second_home = tmp_path / "second"
    first_home.mkdir()
    second_home.mkdir()
    (first_home / ".env").write_text("WIKI_PATH=/first/wiki\n", encoding="utf-8")
    (second_home / ".env").write_text("WIKI_PATH=/second/wiki\n", encoding="utf-8")

    first = wiki_index.resolve_wiki_path(
        hermes_home=first_home, config={}, environ={}
    )
    second = wiki_index.resolve_wiki_path(
        hermes_home=second_home, config={}, environ={}
    )

    assert first == (Path("/first/wiki"), "WIKI_PATH", True)
    assert second == (Path("/second/wiki"), "WIKI_PATH", True)
