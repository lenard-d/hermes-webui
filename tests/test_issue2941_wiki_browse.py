"""Behavioral security coverage for the LLM Wiki browser (issue #2941)."""

from __future__ import annotations

import os
import time as stdlib_time
from pathlib import Path

import pytest

from api.knowledge import WikiIndex, browse_wiki_pages, read_wiki_page
from api.knowledge import wiki_index
from api.knowledge.wiki_read import WikiInvalidPathError, WikiNotFoundError
from tests.frontend_asset_contract import family_source

REPO = Path(__file__).resolve().parents[1]


def _page(root: Path, relative: str, content: str = "# page\n") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_wiki_routes_remain_registered_through_thin_adapters():
    routes_source = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
    adapter_source = (REPO / "api" / "routes_parts" / "llm_wiki.py").read_text(
        encoding="utf-8"
    )
    assert '"/api/wiki/browse"' in routes_source
    assert '"/api/wiki/page"' in routes_source
    assert "knowledge.browse_wiki_pages" in adapter_source
    assert "knowledge.read_wiki_page" in adapter_source
    assert "os.open(" not in adapter_source
    assert "rglob(" not in adapter_source


def test_render_llm_wiki_status_references_browse():
    source = family_source("panels")
    assert "_renderLlmWikiStatus" in source
    assert "async function _openWikiBrowser" in source
    assert "/api/wiki/browse" in source
    assert "/api/wiki/page" in source


def test_wiki_browse_skips_pages_that_disappear_during_listing(tmp_path):
    root = tmp_path / "wiki"
    ok = _page(root, "concepts/ok.md")
    index = WikiIndex(root)
    original_entries = index.entries()
    ok.unlink()

    class SnapshotIndex:
        root = index.root

        def entries(self):
            return original_entries

    assert browse_wiki_pages(SnapshotIndex()) == []


def test_wiki_page_vanished_between_index_and_read_is_not_found(tmp_path):
    root = tmp_path / "wiki"
    page = _page(root, "concepts/gone.md")
    entries = WikiIndex(root).entries()
    page.unlink()

    class SnapshotIndex:
        def __init__(self, root_path, indexed_entries):
            self.root = root_path
            self._entries = indexed_entries

        def entries(self):
            return self._entries

    with pytest.raises(WikiNotFoundError):
        read_wiki_page(SnapshotIndex(root, entries), "concepts/gone.md")


def test_wiki_page_read_is_restricted_to_allowlisted_pages(tmp_path):
    root = tmp_path / "wiki"
    _page(root, "concepts/real.md", "# real page\n")
    secret = _page(root, ".env", "DONOTLEAK=secretmarker_abc\n")
    index = WikiIndex(root)

    with pytest.raises(WikiNotFoundError):
        read_wiki_page(index, ".env")
    assert secret.read_text(encoding="utf-8") not in repr(index.entries())
    assert read_wiki_page(index, "concepts/real.md")["content"] == "# real page\n"


@pytest.mark.parametrize("target", ("../.env", ".hidden/secret.md"))
def test_wiki_symlink_page_cannot_escape_or_reach_hidden_target(tmp_path, target):
    root = tmp_path / "wiki"
    _page(root, ".env", "DONOTLEAK=outside\n")
    _page(root, "concepts/.hidden/secret.md", "DONOTLEAK=hidden\n")
    link = root / "concepts" / "leak.md"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported")

    index = WikiIndex(root)
    assert "concepts/leak.md" not in index.entries()
    with pytest.raises(WikiNotFoundError):
        read_wiki_page(index, "concepts/leak.md")


def test_cached_nested_entry_rechecks_resolved_containment(tmp_path, monkeypatch):
    root = tmp_path / "wiki"
    page = _page(root, "concepts/sub/real.md")
    _page(root, ".env", "DONOTLEAK=stale_cache_marker\n")
    wiki_index.clear_wiki_page_cache()
    monkeypatch.setattr(wiki_index, "WIKI_ALLOWLIST_TTL", 60.0)
    assert wiki_index.page_files(root) == [page]
    page.unlink()
    try:
        page.symlink_to(os.path.join("..", "..", ".env"))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported")

    with pytest.raises(WikiNotFoundError):
        read_wiki_page(WikiIndex(root), "concepts/sub/real.md")


def test_cached_entry_rejects_hardlink_swap_to_outside(tmp_path, monkeypatch):
    root = tmp_path / "wiki"
    page = _page(root, "concepts/page.md")
    outside = _page(tmp_path, "outside-secret", "DONOTLEAK=hardlink\n")
    wiki_index.clear_wiki_page_cache()
    monkeypatch.setattr(wiki_index, "WIKI_ALLOWLIST_TTL", 60.0)
    assert wiki_index.page_files(root) == [page]
    page.unlink()
    try:
        os.link(outside, page)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("hardlinks not supported")

    with pytest.raises(WikiNotFoundError):
        read_wiki_page(WikiIndex(root), "concepts/page.md")


@pytest.mark.parametrize(
    ("target", "marker"),
    (
        (os.path.join("..", "..", "drafts", "page.md"), "cross_section_marker"),
        (os.path.join("..", "..", "entities", "page.md"), "other_allowlisted_marker"),
    ),
)
def test_cached_entry_cannot_jump_to_other_section(tmp_path, monkeypatch, target, marker):
    root = tmp_path / "wiki"
    page = _page(root, "concepts/sub/real.md")
    if "entities" in target:
        _page(root, "entities/page.md", marker)
    else:
        _page(root, "drafts/page.md", marker)
    wiki_index.clear_wiki_page_cache()
    monkeypatch.setattr(wiki_index, "WIKI_ALLOWLIST_TTL", 60.0)
    assert page in wiki_index.page_files(root)
    page.unlink()
    try:
        page.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported")

    with pytest.raises(WikiNotFoundError):
        read_wiki_page(WikiIndex(root), "concepts/sub/real.md")


def test_wiki_page_read_with_symlinked_root(tmp_path):
    real_root = tmp_path / "real-wiki"
    _page(real_root, "concepts/real.md", "# real\n")
    link_root = tmp_path / "link-wiki"
    try:
        os.symlink(real_root, link_root, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks not supported")

    assert read_wiki_page(WikiIndex(link_root), "concepts/real.md")["content"] == "# real\n"


def test_wiki_symlinked_section_cannot_expose_outside_tree(tmp_path):
    root = tmp_path / "wiki"
    root.mkdir()
    outside = tmp_path / "outside"
    _page(outside, "leak.md", "DONOTLEAK=outside_marker\n")
    try:
        (root / "concepts").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported")

    assert browse_wiki_pages(WikiIndex(root)) == []


def test_legit_dotdot_substring_opens_but_alias_spellings_are_rejected(tmp_path):
    root = tmp_path / "wiki"
    _page(root, "concepts/v1..v2.md", "# diff notes\n")
    index = WikiIndex(root)
    assert "diff notes" in read_wiki_page(index, "concepts/v1..v2.md")["content"]
    for alias in (
        "concepts/./v1..v2.md",
        "concepts//v1..v2.md",
        "concepts/v1..v2.md/",
        "concepts\\v1..v2.md",
    ):
        with pytest.raises(WikiInvalidPathError):
            read_wiki_page(index, alias)


def test_wiki_page_is_bounded_to_two_mib(tmp_path):
    root = tmp_path / "wiki"
    _page(root, "concepts/large.md", "x" * (wiki_index.LLM_WIKI_MAX_PAGE_BYTES + 128))
    payload = read_wiki_page(WikiIndex(root), "concepts/large.md")
    assert len(payload["content"]) == wiki_index.LLM_WIKI_MAX_PAGE_BYTES


def test_wiki_page_files_documents_hardlink_trust_boundary():
    doc = wiki_index.page_files.__doc__ or ""
    assert "allowlisted" in doc.lower()


def test_page_files_reuses_cache_with_unchanged_section_mtime(tmp_path, monkeypatch):
    root = tmp_path / "wiki"
    page = _page(root, "concepts/one.md")
    wiki_index.clear_wiki_page_cache()
    monkeypatch.setattr(wiki_index, "WIKI_ALLOWLIST_TTL", 60.0)
    calls = []

    def fake_uncached(resolved_root):
        calls.append(resolved_root)
        return [page]

    monkeypatch.setattr(wiki_index, "page_files_uncached", fake_uncached)
    assert wiki_index.page_files(root) == [page]
    assert wiki_index.page_files(root) == [page]
    assert len(calls) == 1


def test_page_files_cache_invalidates_when_section_identity_changes(tmp_path, monkeypatch):
    root = tmp_path / "wiki"
    section = root / "concepts"
    first = _page(root, "concepts/one.md")
    second = section / "two.md"
    wiki_index.clear_wiki_page_cache()
    monkeypatch.setattr(wiki_index, "WIKI_ALLOWLIST_TTL", 60.0)
    assert wiki_index.page_files(root) == [first]
    second.write_text("# two\n", encoding="utf-8")
    stdlib_time.sleep(0.02)
    os.utime(section, None)
    assert set(wiki_index.page_files(root)) == {first, second}


def test_page_files_cache_expires_after_ttl(tmp_path, monkeypatch):
    root = tmp_path / "wiki"
    page = _page(root, "concepts/one.md")
    wiki_index.clear_wiki_page_cache()
    monkeypatch.setattr(wiki_index, "WIKI_ALLOWLIST_TTL", 1.0)
    ticks = iter([100.0, 100.5, 101.5])
    monkeypatch.setattr(wiki_index.time, "monotonic", lambda: next(ticks))
    calls = []

    def fake_uncached(resolved_root):
        calls.append(resolved_root)
        return [page]

    monkeypatch.setattr(wiki_index, "page_files_uncached", fake_uncached)
    wiki_index.page_files(root)
    wiki_index.page_files(root)
    wiki_index.page_files(root)
    assert len(calls) == 2


def test_i18n_wiki_keys_in_all_locales():
    source = family_source("i18n")
    for key in (
        "wiki_browse",
        "wiki_search_placeholder",
        "wiki_no_pages",
        "wiki_not_configured",
    ):
        assert source.count(key + ":") >= 12
