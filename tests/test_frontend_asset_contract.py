from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

from tests.frontend_asset_contract import (
    FRONTEND_FAMILIES,
    REPO_ROOT,
    family_asset_paths,
)


INDEX_HTML = REPO_ROOT / "static" / "index.html"
SERVICE_WORKER = REPO_ROOT / "static" / "sw.js"
VERSION_QUERY = "?v=__WEBUI_VERSION__"


class _DirectAssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        if tag == "link" and attributes.get("rel") == "stylesheet":
            href = attributes.get("href")
            if href:
                self.urls.append(href)
        elif tag == "script":
            src = attributes.get("src")
            if src:
                self.urls.append(src)


def _direct_asset_urls() -> list[str]:
    parser = _DirectAssetParser()
    parser.feed(INDEX_HTML.read_text(encoding="utf-8"))
    return parser.urls


def _relative_family_assets(family: str) -> list[str]:
    return [path.relative_to(REPO_ROOT).as_posix() for path in family_asset_paths(family)]


def _direct_urls_for_family(family: str) -> list[str]:
    expected = set(_relative_family_assets(family))
    return [url for url in _direct_asset_urls() if url.partition("?")[0] in expected]


@pytest.mark.parametrize("family", FRONTEND_FAMILIES)
def test_index_direct_load_order_matches_each_split_family_architecture(family: str):
    expected = _relative_family_assets(family)
    actual_urls = _direct_urls_for_family(family)

    assert actual_urls == [f"{path}{VERSION_QUERY}" for path in expected]
    assert all(path.is_file() for path in family_asset_paths(family))


def test_facades_have_the_required_side_of_each_direct_load_order():
    assert family_asset_paths("sessions")[-1].name == "sessions.js"

    for family in ("i18n", "ui", "messages", "panels"):
        assert family_asset_paths(family)[0].name == f"{family}.js"


def test_every_direct_split_asset_is_precached_with_the_version_query():
    sw_source = SERVICE_WORKER.read_text(encoding="utf-8")
    assert f"const VQ = '{VERSION_QUERY}';" in sw_source
    shell_assets = sw_source[sw_source.index("const SHELL_ASSETS = [") :]
    shell_assets = shell_assets[: shell_assets.index("];")]
    versioned_shell_paths = set(
        re.findall(r"'\./(static/[^']+)'\s*\+\s*VQ", shell_assets)
    )

    direct_split_assets = {
        url.removesuffix(VERSION_QUERY)
        for family in FRONTEND_FAMILIES
        for url in _direct_urls_for_family(family)
    }
    assert direct_split_assets <= versioned_shell_paths


def test_split_families_use_direct_assets_not_runtime_manifests():
    direct_urls = {
        url
        for family in FRONTEND_FAMILIES
        for url in _direct_urls_for_family(family)
    }
    expected_urls = {
        f"{path}{VERSION_QUERY}"
        for family in FRONTEND_FAMILIES
        for path in _relative_family_assets(family)
    }

    assert direct_urls == expected_urls
    assert not any("manifest.json" in url for url in direct_urls)
