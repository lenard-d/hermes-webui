from tests.frontend_asset_contract import family_source

from pathlib import Path


UI_JS = family_source("ui")
INDEX_HTML = Path("static/index.html").read_text(encoding="utf-8")


def test_ui_routes_session_html_caching_through_the_owned_cache_module():
    cache_script = 'src="static/session_render_cache_adapter.js?v=__WEBUI_VERSION__"'
    ui_script = 'src="static/ui.js?v=__WEBUI_VERSION__"'

    assert INDEX_HTML.index(cache_script) < INDEX_HTML.index(ui_script)
    assert f'<script type="module" {cache_script}></script>' in INDEX_HTML
    assert "window.HermesSessionRenderCache.create({" in UI_JS
    assert "const cached=_sessionHtmlCache.get(sid);" in UI_JS
    assert "_sessionHtmlCache.set(sid,{html:_html,msgCount,renderWindowKey,signature:renderSignature});" in UI_JS
    assert "_sessionHtmlCache.delete(_sessionHtmlCacheSid);" in UI_JS
