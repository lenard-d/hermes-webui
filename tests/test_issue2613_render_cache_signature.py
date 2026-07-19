from pathlib import Path


UI_JS = Path("static/ui.js").read_text(encoding="utf-8")
INDEX_HTML = Path("static/index.html").read_text(encoding="utf-8")


def test_session_html_cache_uses_render_signature_not_only_count():
    assert "function _messageRenderCacheSignature()" in UI_JS
    assert "const renderSignature=_messageRenderCacheSignature();" in UI_JS
    assert "cached.signature===renderSignature" in UI_JS
    assert "signature:renderSignature" in UI_JS


def test_render_signature_tracks_message_content_and_settled_tool_cards():
    signature_fn = UI_JS[UI_JS.index("function _messageRenderCacheSignature()"):UI_JS.index("function _clipCliToolSnippet")]
    assert "msgContent(m)" in signature_fn
    assert "m.tool_calls" in signature_fn
    assert "m._partial_tool_calls" in signature_fn
    assert "S.toolCalls" in signature_fn
    assert "tc.snippet" in signature_fn
    assert "compression_anchor_summary" in signature_fn


def test_documentation_no_longer_allows_same_count_stale_html():
    assert "Known limitation: cache key is session_id + message count" not in UI_JS
    assert "mutate message content without changing the count will serve stale HTML" not in UI_JS


def test_ui_routes_session_html_caching_through_the_owned_cache_module():
    cache_script = 'src="static/session_render_cache_adapter.js?v=__WEBUI_VERSION__"'
    ui_script = 'src="static/ui.js?v=__WEBUI_VERSION__"'

    assert INDEX_HTML.index(cache_script) < INDEX_HTML.index(ui_script)
    assert f'<script type="module" {cache_script}></script>' in INDEX_HTML
    assert "window.HermesSessionRenderCache.create({" in UI_JS
    assert "const cached=_sessionHtmlCache.get(sid);" in UI_JS
    assert "_sessionHtmlCache.set(sid,{html:_html,msgCount,renderWindowKey,signature:renderSignature});" in UI_JS
    assert "_sessionHtmlCache.delete(_sessionHtmlCacheSid);" in UI_JS
