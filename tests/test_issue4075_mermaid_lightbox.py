"""Static regression coverage for Mermaid diagram lightbox wiring."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parent.parent
MEDIA_AND_QUOTA_JS = (ROOT / "static" / "modules" / "ui" / "media-and-quota.js").read_text(encoding="utf-8")
MARKDOWN_POSTPROCESSING_JS = (ROOT / "static" / "modules" / "ui" / "markdown-postprocessing.js").read_text(encoding="utf-8")
TRANSCRIPT_CSS = (ROOT / "static" / "style_parts" / "008-sessions-transcript.css").read_text(encoding="utf-8")
WORKSPACE_RESPONSIVE_CSS = (ROOT / "static" / "style_parts" / "004-chat-workspace-responsive.css").read_text(encoding="utf-8")


class TestMermaidLightboxHelper:
    def test_mermaid_lightbox_has_dedicated_helper(self):
        src = MEDIA_AND_QUOTA_JS
        assert re.search(r"function\s+_mountMermaidViewer\(svgEl,\s*options\s*=\s*\{\}\)\s*\{", src)
        assert re.search(r"function\s+_openMermaidLightbox\(svgEl\)\s*\{", src)
        assert "const viewer = _mountMermaidViewer(clone, {mode:'lightbox'});" in src
        assert "mermaid-lightbox-svg" in src

    def test_mermaid_render_path_mounts_inline_viewer_shell(self):
        assert "const renderedSvg=block.querySelector('svg');" in MARKDOWN_POSTPROCESSING_JS
        assert "if(renderedSvg) _mountMermaidViewer(renderedSvg,{mode:'inline'});" in MARKDOWN_POSTPROCESSING_JS

    def test_mermaid_lightbox_reuses_existing_modal_chrome(self):
        src = MEDIA_AND_QUOTA_JS
        assert "img-lightbox" in src
        assert "img-lightbox-close" in src
        assert "_closeImgLightbox(lb)" in src

    def test_mermaid_lightbox_rewrites_cloned_svg_ids(self):
        src = MEDIA_AND_QUOTA_JS
        assert "const idMap = new Map();" in src
        assert "const idPrefix = 'mermaid-lightbox-'" in src
        assert "replace(/url\\(#([^)]+)\\)/g" in src

    def test_mermaid_lightbox_rewrites_embedded_style_selectors(self):
        src = MEDIA_AND_QUOTA_JS
        assert "clone.querySelectorAll('style').forEach(styleEl => {" in src
        assert "styleText = styleText.replace(new RegExp(`url\\\\(#${escapedId}\\\\)`" in src
        assert "styleText = styleText.replace(new RegExp(`(^|[^\\\\w-])#${escapedId}(?=$|[^\\\\w-])`" in src


class TestDocumentClickDelegate:
    def test_delegate_routes_rendered_mermaid_svgs_before_attach_thumb(self):
        src = MEDIA_AND_QUOTA_JS
        mermaid_branch = (
            "  const mermaidSvg = e.target.closest('.mermaid-rendered svg');\n"
            "  if(mermaidSvg){ _openMermaidLightbox(mermaidSvg); return; }\n"
        )
        attach_branch = (
            "  img = e.target.closest('.attach-thumb');\n"
            "  if(img && img.tagName === 'IMG'){\n"
        )
        assert mermaid_branch in src
        assert attach_branch in src
        assert src.index(mermaid_branch) < src.index(attach_branch)

    def test_delegate_still_handles_message_images(self):
        src = MEDIA_AND_QUOTA_JS
        msg_branch = "let img = e.target.closest('.msg-media-img');\n  if(img){ _openImgLightbox(img); return; }"
        assert msg_branch in src


class TestMermaidLightboxCss:
    def test_rendered_mermaid_svg_advertises_zoom(self):
        src = TRANSCRIPT_CSS
        assert ".mermaid-rendered svg{max-width:100%;height:auto;cursor:default;}" in src

    def test_lightbox_svg_uses_modal_viewport_limits(self):
        assert ".mermaid-viewer--lightbox .mermaid-viewer-viewport{max-width:90vw;max-height:90vh;" in WORKSPACE_RESPONSIVE_CSS
        assert ".img-lightbox .mermaid-lightbox-svg{background:var(--code-bg);cursor:default;}" in WORKSPACE_RESPONSIVE_CSS
        assert "background:var(--code-bg);" in WORKSPACE_RESPONSIVE_CSS


class TestLightboxAria:
    def test_lightboxes_set_aria_modal(self):
        assert MEDIA_AND_QUOTA_JS.count("setAttribute('aria-modal', 'true')") >= 2
