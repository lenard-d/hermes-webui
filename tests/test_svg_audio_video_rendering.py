"""Test: SVG, audio, video inline rendering (#481)"""
import re
import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
MEDIA_JS = (REPO_ROOT / "static" / "modules" / "ui" / "media-and-quota.js").read_text(encoding="utf-8")
UPLOAD_TRAY_JS = (REPO_ROOT / "static" / "modules" / "ui" / "upload-tray.js").read_text(encoding="utf-8")
MEDIA_CSS = (REPO_ROOT / "static" / "style_parts" / "004-chat-workspace-responsive.css").read_text(encoding="utf-8")
LOCALE_SOURCES = tuple(
    path.read_text(encoding="utf-8")
    for path in sorted((REPO_ROOT / "static" / "i18n_parts").glob("locale-*.js"))
)
NODE = shutil.which("node")


def test_media_extension_regexes_exist():
    """Verify SVG/audio/video extension regexes are defined."""
    src = MEDIA_JS
    assert '_SVG_EXTS' in src, "Missing _SVG_EXTS regex"
    assert '_AUDIO_EXTS' in src, "Missing _AUDIO_EXTS regex"
    assert '_VIDEO_EXTS' in src, "Missing _VIDEO_EXTS regex"
    # Verify they test correct extensions
    assert 'svg' in src, "SVG regex should match .svg"
    assert 'mp3' in src, "AUDIO regex should match .mp3"
    assert 'ogg' in src, "AUDIO regex should match .ogg"
    assert 'mp4' in src, "VIDEO regex should match .mp4"
    assert 'webm' in src, "VIDEO regex should match .webm"


def test_svg_rendered_before_image_catch_all():
    """Verify SVG handler for URLs runs before the catch-all image handler."""
    src = MEDIA_JS
    # Find positions of SVG vs image catch-all in the URL section
    svg_url_match = src.find("SVG URLs")
    # Comment can say either variant of the catch-all description
    image_catch_all = src.find("Render all https:// URLs as <img>")
    assert svg_url_match > 0, "SVG URL handler not found"
    assert image_catch_all > 0, "Image catch-all handler not found"
    assert svg_url_match < image_catch_all, \
        "SVG handler must come before image catch-all to avoid being shadowed"


def test_local_svg_inline_rendering():
    """Verify local SVG files render as inline image."""
    src = MEDIA_JS
    assert "msg-media-svg" in src, "Missing msg-media-svg CSS class for SVG rendering"
    # Both URL-based and local-path SVG handlers are centralised in
    # _inlineMediaHtmlForRef (the single MEDIA renderer exported by ui.js for
    # use by both renderMd() and the streaming smd path). The shared function
    # contains all SVG markup, so we only need the helper to exist once.
    count = src.count("msg-media-svg")
    assert count >= 1, f"Expected >=1 msg-media-svg references in _inlineMediaHtmlForRef, got {count}"


def test_local_audio_inline_rendering():
    """Verify local audio files render as inline player."""
    src = MEDIA_JS
    assert "msg-media-audio" in src, "Missing msg-media-audio CSS class"
    assert '<audio class="msg-media-player msg-media-audio"' in src
    assert "controls preload=\"metadata\"" in src, "Should render an audio element with controls"
    # See comment in test_svg_rendered_before_image_catch_all — audio markup
    # lives in a single shared helper now.
    count = src.count("msg-media-audio")
    assert count >= 1, f"Expected >=1 msg-media-audio references in _inlineMediaHtmlForRef, got {count}"


def test_local_video_inline_rendering():
    """Verify local video files render as inline player."""
    src = MEDIA_JS
    assert "msg-media-video" in src, "Missing msg-media-video CSS class"
    assert '<video class="msg-media-player msg-media-video"' in src
    assert "controls preload=\"metadata\"" in src, "Should render a video element with controls"
    # See comment in test_svg_rendered_before_image_catch_all — video markup
    # lives in a single shared helper now.
    count = src.count("msg-media-video")
    assert count >= 1, f"Expected >=1 msg-media-video references in _inlineMediaHtmlForRef, got {count}"


def test_url_svg_audio_video_handlers():
    """Verify HTTPS URLs for SVG/audio/video get inline rendering."""
    src = MEDIA_JS
    # SVG URLs should be handled via _SVG_EXTS test on urlPath
    url_svg = "_SVG_EXTS.test(urlPath)" in src or ("_SVG_EXTS.test" in src and "urlPath" in src)
    # Audio/video via mediaKindForName or explicit _AUDIO/_VIDEO tests
    url_audio = src.count("_AUDIO_EXTS.test(src.split") + src.count("_AUDIO_EXTS.test(urlPath") + src.count("mediaKindForName")
    url_video = src.count("_VIDEO_EXTS.test(src.split") + src.count("_VIDEO_EXTS.test(urlPath") + src.count("mediaKindForName")
    assert url_svg, "URL SVG handler should test extension on src"
    assert url_audio >= 1, "URL audio handler should test extension on src"
    assert url_video >= 1, "URL video handler should test extension on src"


def test_webm_prefers_video_when_audio_and_video_regexes_overlap():
    """Verify .webm is not shadowed by the audio regex."""
    src = MEDIA_JS
    kind_start = src.find("function _mediaKindForName")
    kind_body = src[kind_start:kind_start + 400]
    assert "_VIDEO_EXTS.test(clean)" in kind_body
    assert "_AUDIO_EXTS.test(clean)" in kind_body
    assert kind_body.index("_VIDEO_EXTS.test(clean)") < kind_body.index("_AUDIO_EXTS.test(clean)"), \
        "_mediaKindForName must check video before audio so .webm renders as video"
    inline_start = src.find("function _inlineMediaHtmlForRef")
    inline_body = src[inline_start:inline_start + 4500]
    assert "const kind=_AUDIO_EXTS.test(ref)?'audio':'video';" not in inline_body, \
        "Local MEDIA rendering must reuse _mediaKindForName instead of reintroducing audio-first .webm ordering"
    assert "if(localKind==='audio'||localKind==='video')" in inline_body


def test_attachment_svg_audio_video():
    """Verify file attachments for SVG/audio/video get inline previews."""
    src = UPLOAD_TRAY_JS
    assert "attach-thumb--svg" in src, "Missing attach-thumb--svg for SVG thumbnails"
    assert "_SVG_EXTS.test(f.name)" in src, "SVG files must enter the media-chip branch"
    assert "mediaKind==='audio'" in src, "Missing audio attachment branch"
    assert "mediaKind==='video'" in src, "Missing video attachment branch"
    assert "attach-chip-media" in src, "Missing attach-chip-media label"


def test_attachment_blob_url_cleanup():
    """Verify audio/video attachment chips create blob URLs."""
    src = UPLOAD_TRAY_JS
    # SVG and media attachments should use createObjectURL
    assert "URL.createObjectURL(f)" in src, "Should create blob URLs for attachments"


def test_preload_metadata():
    """Verify audio/video elements use preload='metadata' for performance."""
    src = UPLOAD_TRAY_JS
    assert 'preload="metadata"' in src, "Audio/video should use preload='metadata'"


def test_media_label_class():
    """Verify media label class exists for type identification."""
    src = MEDIA_CSS
    assert "msg-media-label" in src, "Missing msg-media-label class"


def test_i18n_keys():
    """Verify media rendering i18n keys exist in all locales."""
    src = LOCALE_SOURCES
    required_keys = [
        'media_audio_label',
        'media_svg_label',
        'media_video_label',
    ]
    for key in required_keys:
        count = sum(f"{key}:" in locale for locale in src)
        assert count >= 8, f"Key '{key}' found in {count} locale modules, expected at least 8"


def test_css_classes_exist():
    """Verify all media CSS classes are defined."""
    src = MEDIA_CSS
    required_classes = [
        'msg-media-svg',
        'msg-media-label',
        'msg-media-audio',
        'msg-media-video',
        'attach-thumb--svg',
        'attach-chip--audio',
        'attach-chip--video',
        'attach-chip-media',
    ]
    for cls in required_classes:
        assert cls in src, f"Missing CSS class: .{cls}"


def test_svg_not_matched_by_image_exts():
    """Verify .svg is NOT in _IMAGE_EXTS (SVG has its own handler)."""
    src = MEDIA_JS
    # Extract the _IMAGE_EXTS regex
    match = re.search(r"const _IMAGE_EXTS=/([^/]+)/i", src)
    assert match, "Could not find _IMAGE_EXTS regex"
    exts = match.group(1)
    assert 'svg' not in exts.lower(), ".svg should NOT be in _IMAGE_EXTS"


def test_audio_video_not_matched_by_image_exts():
    """Verify audio/video extensions are NOT in _IMAGE_EXTS."""
    src = MEDIA_JS
    match = re.search(r"const _IMAGE_EXTS=/([^/]+)/i", src)
    assert match
    exts = match.group(1)
    for ext in ['mp3', 'mp4', 'wav', 'ogg', 'webm', 'mov', 'm4a']:
        assert ext not in exts.lower(), f".{ext} should NOT be in _IMAGE_EXTS"


@pytest.mark.skipif(NODE is None, reason="node is required to execute the media renderer")
def test_actual_media_renderer_returns_the_expected_dom_markup():
    """Exercise the native ESM owner instead of a concatenated UI facade."""
    driver = r"""
globalThis.window=globalThis;
globalThis.__HERMES_CONFIG__={};
globalThis.addEventListener=()=>{};
globalThis.document={
  addEventListener:()=>{},
  getElementById:()=>null,
  baseURI:'https://hermes.example/app/',
};
globalThis.localStorage={getItem:()=>null,setItem:()=>{}};
const media=await import('./static/modules/ui/media-and-quota.js');
const render=media._inlineMediaHtmlForRef;
console.log(JSON.stringify({
  remoteSvg:render('https://cdn.example/diagram.svg'),
  remoteAudio:render('https://cdn.example/track.mp3'),
  remoteWebm:render('https://cdn.example/clip.webm'),
  localAudio:render('/tmp/voice.ogg','session-1'),
  localVideo:render('/tmp/clip.mp4','session-1'),
}));
"""
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", driver],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    markup = json.loads(result.stdout)
    assert 'class="msg-media-svg"' in markup["remoteSvg"]
    assert '<audio class="msg-media-player msg-media-audio"' in markup["remoteAudio"]
    assert '<video class="msg-media-player msg-media-video"' in markup["remoteWebm"]
    assert 'preload="metadata"' in markup["remoteAudio"]
    assert 'api/media?path=%2Ftmp%2Fvoice.ogg&amp;session_id=session-1&amp;inline=1' in markup["localAudio"]
    assert 'api/media?path=%2Ftmp%2Fclip.mp4&amp;session_id=session-1&amp;inline=1' in markup["localVideo"]


@pytest.mark.skipif(NODE is None, reason="node is required to execute the upload tray")
def test_actual_upload_tray_renders_svg_audio_and_video_chips():
    """The attachment tray must classify media through its native ESM owner."""
    driver = r"""
function element(){
  return {
    className:'', dataset:{}, children:[], innerHTML:'',
    classList:{add:()=>{},remove:()=>{}},
    appendChild(child){this.children.push(child);},
    querySelector(){return {};},
  };
}
const tray=element();
globalThis.window=globalThis;
globalThis.__HERMES_CONFIG__={};
globalThis.addEventListener=()=>{};
globalThis.document={
  addEventListener:()=>{},
  getElementById:id=>id==='attachTray'?tray:null,
  createElement:()=>element(),
};
globalThis.URL={createObjectURL:file=>'blob:'+file.name,revokeObjectURL:()=>{}};
globalThis.t=key=>key;
globalThis.li=()=>'<i></i>';
const {S}=await import('./static/modules/ui/state.js');
const {renderTray}=await import('./static/modules/ui/upload-tray.js');
S.pendingFiles.push(
  {name:'diagram.svg',size:1},
  {name:'voice.ogg',size:1},
  {name:'clip.webm',size:1},
);
renderTray();
console.log(JSON.stringify(tray.children.map(chip=>({className:chip.className,html:chip.innerHTML}))));
"""
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", driver],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    svg, audio, video = json.loads(result.stdout)
    assert "attach-chip--media attach-chip--" in svg["className"]
    assert "attach-thumb--svg" in svg["html"]
    assert "attach-chip--audio" in audio["className"]
    assert "<audio controls preload=\"metadata\"" in audio["html"]
    assert "attach-chip--video" in video["className"]
    assert "<video controls preload=\"metadata\"" in video["html"]
