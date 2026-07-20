from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

from tests.frontend_asset_contract import (
    FRONTEND_FAMILIES,
    REPO_ROOT,
    direct_family_asset_paths,
    family_asset_paths,
    family_direct_asset_paths,
    family_entrypoint_path,
    module_family_paths,
    ui_module_paths,
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
    return [path.relative_to(REPO_ROOT).as_posix() for path in family_direct_asset_paths(family)]


def _direct_urls_for_family(family: str) -> list[str]:
    expected = set(_relative_family_assets(family))
    return [url for url in _direct_asset_urls() if url.partition("?")[0] in expected]


def _expected_direct_urls(family: str) -> list[str]:
    if family == "commands":
        return []
    if family in {"boot", "messages", "panels", "sessions", "ui"}:
        entrypoint = family_entrypoint_path(family)
        assert entrypoint is not None
        path = entrypoint.relative_to(REPO_ROOT).as_posix()
        return [f"{path}{VERSION_QUERY}"]
    return [f"{path}{VERSION_QUERY}" for path in _relative_family_assets(family)]


@pytest.mark.parametrize("family", FRONTEND_FAMILIES)
def test_index_direct_load_order_matches_each_split_family_architecture(family: str):
    expected = _relative_family_assets(family)
    actual_urls = _direct_urls_for_family(family)

    assert actual_urls == _expected_direct_urls(family)
    assert all(path.is_file() for path in family_asset_paths(family))


def test_facades_have_the_required_side_of_each_direct_load_order():
    assert family_asset_paths("sessions") == (
        REPO_ROOT / "static" / "modules" / "sessions" / "index.js",
    )

    for family in ("i18n",):
        assert family_asset_paths(family)[0].name == f"{family}.js"
    assert family_direct_asset_paths("messages")[0].as_posix().endswith(
        "static/modules/messages/index.js"
    )

    assert family_direct_asset_paths("panels")[0].as_posix().endswith("modules/panels/index.js")

    assert direct_family_asset_paths("ui")[0].as_posix().endswith("modules/ui/index.js")


def test_every_frontend_asset_is_precached_at_its_browser_request_url():
    sw_source = SERVICE_WORKER.read_text(encoding="utf-8")
    assert f"const VQ = '{VERSION_QUERY}';" in sw_source
    shell_assets = sw_source[sw_source.index("const SHELL_ASSETS = [") :]
    shell_assets = shell_assets[: shell_assets.index("];")]
    versioned_shell_paths = set(
        re.findall(r"'\./(static/[^']+)'\s*\+\s*VQ", shell_assets)
    )
    unversioned_shell_paths = set(
        re.findall(r"'\./(static/[^']+)'\s*,", shell_assets)
    )

    direct_assets = {
        url.removesuffix(VERSION_QUERY)
        for family in FRONTEND_FAMILIES
        for url in _direct_urls_for_family(family)
    }
    assert direct_assets <= versioned_shell_paths

    native_dependencies = {
        path.relative_to(REPO_ROOT).as_posix()
        for family in ("boot", "commands", "messages", "panels", "sessions", "assistant-turn-anchors")
        for path in module_family_paths(family)
        if path != family_entrypoint_path(family)
    }
    native_dependencies.add("static/modules/compatibility.js")
    assert native_dependencies <= unversioned_shell_paths


@pytest.mark.parametrize("family", ("messages", "panels", "sessions", "assistant-turn-anchors"))
def test_native_module_family_imports_are_explicit_and_precached(family: str):
    paths = module_family_paths(family)
    entrypoint = paths[-1]
    source = entrypoint.read_text(encoding="utf-8")
    imported_names = set(re.findall(r"from\s+['\"]\./([^'\"]+)['\"]", source))
    expected_names = {path.name for path in paths[:-1]}

    assert imported_names == expected_names

    shell_assets = SERVICE_WORKER.read_text(encoding="utf-8")
    for dependency in paths[:-1]:
        relative = dependency.relative_to(REPO_ROOT).as_posix()
        assert f"'./{relative}'" in shell_assets


def test_split_families_use_direct_assets_not_runtime_manifests():
    direct_urls = {
        url
        for family in FRONTEND_FAMILIES
        for url in _direct_urls_for_family(family)
    }
    expected_urls = {
        url
        for family in FRONTEND_FAMILIES
        for url in _expected_direct_urls(family)
    }

    assert direct_urls == expected_urls
    assert not any("manifest.json" in url for url in direct_urls)


def test_ui_entrypoint_imports_every_semantic_module_once():
    entrypoint = direct_family_asset_paths("ui")[0]
    source = entrypoint.read_text(encoding="utf-8")
    imports = re.findall(r"from './([^']+\.js)'", source)

    assert sorted(imports) == sorted(path.name for path in ui_module_paths())
    assert len(imports) == len(set(imports))


def test_service_worker_precaches_native_ui_dependencies_at_import_urls():
    sw_source = SERVICE_WORKER.read_text(encoding="utf-8")
    shell_assets = sw_source[sw_source.index("const SHELL_ASSETS = [") :]
    shell_assets = shell_assets[: shell_assets.index("]; ") if "]; " in shell_assets else shell_assets.index("];")]

    for path in ui_module_paths():
        relative = path.relative_to(REPO_ROOT).as_posix()
        assert f"'./{relative}'," in shell_assets
