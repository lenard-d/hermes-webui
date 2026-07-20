"""MIME, disposition, and sandbox policy for media previews and downloads."""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from api.config import MIME_MAP


INLINE_IMAGE_TYPES = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/webp",
    "image/x-icon", "image/bmp",
})
AUDIO_VIDEO_PDF_TYPES = frozenset({
    "audio/mpeg", "audio/wav", "audio/x-wav", "audio/mp4", "audio/aac",
    "audio/ogg", "audio/opus", "audio/flac",
    "video/mp4", "video/quicktime", "video/webm", "video/ogg",
    "application/pdf",
})
SESSION_MEDIA_TOKEN_TYPES = INLINE_IMAGE_TYPES | AUDIO_VIDEO_PDF_TYPES | {"text/html"}
DANGEROUS_TYPES = frozenset({"text/html", "application/xhtml+xml", "image/svg+xml"})
HTML_SANDBOX_CSP = "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox"
LOCAL_MEDIA_HTML_CSP = "sandbox allow-scripts"


@dataclass(frozen=True)
class PreviewPolicy:
    mime: str
    disposition: str
    csp: str | None
    transform_html: bool = False


def mime_for_path(path: str | Path) -> str:
    return MIME_MAP.get(Path(path).suffix.lower(), "application/octet-stream")


def preview_policy(
    path: str | Path,
    *,
    inline_requested: bool,
    force_download: bool = False,
    local_media: bool = False,
) -> PreviewPolicy:
    """Resolve one authoritative MIME/disposition/CSP decision."""
    mime = mime_for_path(path)
    html_inline = inline_requested and mime == "text/html" and not force_download
    if local_media:
        inline = (
            not force_download
            and mime != "image/svg+xml"
            and (mime in INLINE_IMAGE_TYPES or (inline_requested and mime in AUDIO_VIDEO_PDF_TYPES) or html_inline)
        )
    else:
        inline = not force_download and (mime not in DANGEROUS_TYPES or html_inline)
    disposition = "inline" if inline else "attachment"
    if local_media:
        csp = LOCAL_MEDIA_HTML_CSP if html_inline else None
    else:
        csp = HTML_SANDBOX_CSP if inline_requested and disposition == "inline" else None
    return PreviewPolicy(
        mime=mime,
        disposition=disposition,
        csp=csp,
        transform_html=html_inline and not local_media,
    )


def content_disposition_value(disposition: str, filename: str) -> str:
    """Build a latin-1-safe Content-Disposition value with RFC 5987 filename*."""
    safe_name = Path(filename).name.replace("\r", "").replace("\n", "")
    ascii_fallback = "".join(
        ch if 32 <= ord(ch) < 127 and ch not in {'"', "\\"} else "_"
        for ch in safe_name
    ).strip(" .")
    if not ascii_fallback:
        suffix = Path(safe_name).suffix
        ascii_suffix = "".join(
            ch if 32 <= ord(ch) < 127 and ch not in {'"', "\\"} else "_"
            for ch in suffix
        )
        ascii_fallback = f"download{ascii_suffix}" if ascii_suffix else "download"
    quoted_name = urllib.parse.quote(safe_name, safe="")
    return (
        f'{disposition}; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{quoted_name}"
    )


def html_preview_with_blank_base(raw: bytes) -> bytes:
    """Inject a blank-target base element into sandboxed HTML previews."""
    base = '<base target="_blank">'
    text = raw.decode("utf-8", errors="replace")
    if re.search(r"<head(?:\s[^>]*)?>", text, flags=re.IGNORECASE):
        text = re.sub(r"(<head\b[^>]*>)", r"\1" + base, text, count=1, flags=re.IGNORECASE)
    elif re.search(r"<!doctype[^>]*>", text, flags=re.IGNORECASE):
        text = re.sub(
            r"(<!doctype[^>]*>)",
            r"\1<head>" + base + "</head>",
            text,
            count=1,
            flags=re.IGNORECASE,
        )
    else:
        text = "<head>" + base + "</head>" + text
    return text.encode("utf-8")
