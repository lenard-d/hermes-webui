"""Regression coverage for panel-driven document.title ownership (#4039)."""

from __future__ import annotations

from tests.frontend_asset_contract import family_source


def _src(name: str) -> str:
    with open(f"static/{name}", encoding="utf-8") as f:
        return f.read()


def test_sync_app_titlebar_stamps_non_chat_document_title():
    src = family_source("panels")

    assert "if (panel !== 'chat') {" in src
    assert "const bot = typeof assistantDisplayName === 'function' ? assistantDisplayName() : '';" in src
    assert "document.title = bot ? mainText + ' \\u2014 ' + bot : mainText;" in src


def test_switch_panel_restores_chat_title_via_sync_topbar():
    src = family_source("panels")

    assert "if (nextPanel === 'chat' && typeof syncTopbar === 'function') syncTopbar();" in src
    assert "else syncAppTitlebar();" in src


def test_chat_title_format_stays_owned_by_sync_topbar():
    panels_src = family_source("panels")
    ui_src = family_source("ui")

    assert "document.title=sessionTitle+' \\u2014 '+assistantDisplayName();" in ui_src
    assert "document.title=sessionTitle+' \\u2014 '+assistantDisplayName();" not in panels_src
