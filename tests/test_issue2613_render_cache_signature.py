from tests.frontend_asset_contract import family_source

from pathlib import Path


UI_JS = family_source("ui")
INDEX_HTML = Path("static/index.html").read_text(encoding="utf-8")


def test_ui_routes_session_html_caching_through_the_owned_cache_module():
    ui_script = 'src="static/modules/ui/index.js?v=__WEBUI_VERSION__"'

    assert f'<script type="module" {ui_script}></script>' in INDEX_HTML
    assert "from '../../session_render_cache.js'" in UI_JS
    assert "createSessionRenderCache({" in UI_JS
    assert "createRenderSignature({" in UI_JS
    assert "HermesSessionRenderCache" not in UI_JS
    assert "const cached=_sessionHtmlCache.get(sid);" in UI_JS
    assert "_sessionHtmlCache.set(sid,{html:_html,msgCount,renderWindowKey,signature:renderSignature});" in UI_JS
    assert "_sessionHtmlCache.delete(_sessionHtmlCacheSid);" in UI_JS
