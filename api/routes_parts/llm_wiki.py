"""Thin HTTP adapters and legacy route exports for LLM Wiki knowledge."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs

from api import knowledge
from api.helpers import bad, j
from api.knowledge import wiki_index as _index
from api.knowledge import wiki_read as _read

_LLM_WIKI_DOCS_URL = _index.LLM_WIKI_DOCS_URL
_LLM_WIKI_PAGE_DIRS = _index.LLM_WIKI_PAGE_DIRS
_LLM_WIKI_MAX_FILES = _index.LLM_WIKI_MAX_FILES
_LLM_WIKI_MAX_PAGE_BYTES = _index.LLM_WIKI_MAX_PAGE_BYTES
_LLM_WIKI_FORBIDDEN_ROOTS = _index.LLM_WIKI_FORBIDDEN_ROOTS
_WIKI_ALLOWLIST_TTL = _index.WIKI_ALLOWLIST_TTL
_wiki_allowlist_cache = _index._page_cache
_wiki_allowlist_cache_lock = _index._page_cache_lock
_llm_wiki_active_hermes_home = knowledge.active_hermes_home
_llm_wiki_env_file_path = _index.env_file_wiki_path
_llm_wiki_get_config_path_value = _index.config_path_value
_llm_wiki_config_path = _index.config_wiki_path
_llm_wiki_resolve_path = knowledge.resolve_wiki_path
_llm_wiki_safe_iso = _index.safe_iso
_llm_wiki_count_files = _index.count_files
_llm_wiki_page_files_cache_signature = _index.page_cache_signature
_llm_wiki_page_files_uncached = _index.page_files_uncached
_llm_wiki_page_files = _index.page_files
_llm_wiki_clear_page_files_cache = knowledge.clear_wiki_page_cache
_llm_wiki_allowlisted_entries = _index.allowlisted_entries
_llm_wiki_status_file_entry_stat = _index.status_file_entry_stat
_llm_wiki_verified_status_file_stat = _index.verified_status_file_stat
_llm_wiki_last_writer = _index.last_writer
_build_llm_wiki_status = knowledge.build_wiki_status


def _handle_llm_wiki_status(handler, parsed) -> bool:
    j(handler, knowledge.build_wiki_status())
    return True


def _handle_llm_wiki_browse(handler, parsed) -> bool:
    wiki_root, _, _ = knowledge.resolve_wiki_path()
    try:
        pages = knowledge.browse_wiki_pages(knowledge.WikiIndex(Path(wiki_root)))
    except _read.WikiNotFoundError as exc:
        return bad(handler, str(exc), status=404)
    return j(handler, {"pages": pages})


def _handle_llm_wiki_page(handler, parsed) -> bool:
    wiki_root, _, _ = knowledge.resolve_wiki_path()
    page_path = parse_qs(parsed.query or "").get("path", [""])[0]
    try:
        payload = knowledge.read_wiki_page(
            knowledge.WikiIndex(Path(wiki_root)),
            page_path,
        )
    except _read.WikiInvalidPathError as exc:
        return bad(handler, str(exc), status=400)
    except (_read.WikiNotFoundError, _read.WikiFileReadError) as exc:
        return bad(handler, str(exc), status=404)
    return j(handler, payload)


__routes_exports__ = (
    "_LLM_WIKI_DOCS_URL",
    "_LLM_WIKI_PAGE_DIRS",
    "_LLM_WIKI_MAX_FILES",
    "_LLM_WIKI_MAX_PAGE_BYTES",
    "_LLM_WIKI_FORBIDDEN_ROOTS",
    "_WIKI_ALLOWLIST_TTL",
    "_wiki_allowlist_cache",
    "_wiki_allowlist_cache_lock",
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
