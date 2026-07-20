"""Session export and bounded transcript search HTTP owners."""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs

from api.config import load_settings
from api.helpers import _redact_text, bad, j, redact_session_data
from api.http.session_visibility import _all_profiles_enabled
from api.profiles import _profiles_match, get_active_profile_name
from api.sessions import session_sidebar_projection as sidebar_projection
from api.sessions.store import all_sessions, get_session

def _handle_session_export(handler, parsed):
    sid = parse_qs(parsed.query).get("session_id", [""])[0]
    if not sid:
        return bad(handler, "session_id is required")
    try:
        s = get_session(sid)
    except KeyError:
        return bad(handler, "Session not found", 404)
    active_profile = get_active_profile_name()
    if not _profiles_match(getattr(s, "profile", None), active_profile):
        return bad(handler, "Session not found", 404)
    safe = redact_session_data(s.__dict__)
    qs = parse_qs(parsed.query)
    fmt = qs.get("format", ["json"])[0].lower()
    if fmt == "html":
        from api.sessions.export import render_session_html
        theme = qs.get("theme", ["dark"])[0].lower()
        palette: dict | None = None
        raw_palette = qs.get("palette", [""])[0]
        if raw_palette:
            try:
                import base64 as _b64
                decoded = _b64.b64decode(raw_palette, validate=False).decode("utf-8")
                parsed_palette = json.loads(decoded)
                if isinstance(parsed_palette, dict):
                    # Cap payload so a hostile client can't blow up the response.
                    if len(parsed_palette) <= 64:
                        palette = parsed_palette
            except Exception:
                palette = None
        payload = render_session_html(safe, theme=theme, palette=palette)
        content_type = "text/html; charset=utf-8"
        ext = "html"
    else:
        payload = json.dumps(safe, ensure_ascii=False, indent=2)
        content_type = "application/json; charset=utf-8"
        ext = "json"
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header(
        "Content-Disposition", f'attachment; filename="hermes-{sid}.{ext}"'
    )
    handler.send_header("Content-Length", str(len(payload.encode("utf-8"))))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(payload.encode("utf-8"))
    return True


def _session_search_message_text(message):
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, list):
        return " ".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(content or "")


def _session_search_preview(text, query, max_len=124):
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    q = re.sub(r"\s+", " ", str(query or "")).strip()
    if not normalized or not q:
        return ""
    idx = normalized.lower().find(q.lower())
    if idx < 0:
        return ""

    max_len = max(32, int(max_len or 124))
    if len(normalized) <= max_len:
        return normalized

    context = max(12, (max_len - len(q)) // 2)
    start = max(0, idx - context)
    end = min(len(normalized), idx + len(q) + context)
    if start > 0:
        while start < idx and normalized[start] != " ":
            start += 1
        if start >= idx:
            start = max(0, idx - context)
    if end < len(normalized):
        while end > idx + len(q) and normalized[end - 1] != " ":
            end -= 1
        if end <= idx + len(q):
            end = min(len(normalized), idx + len(q) + context)
    excerpt = normalized[start:end].strip()
    if start > 0:
        excerpt = "..." + excerpt
    if end < len(normalized):
        excerpt = excerpt + "..."
    return excerpt


def _handle_sessions_search(handler, parsed):
    qs = parse_qs(parsed.query)
    q = qs.get("q", [""])[0].lower().strip()
    content_search = qs.get("content", ["1"])[0] == "1"
    from api.profiles import get_active_profile_name
    active_profile = get_active_profile_name()
    all_profiles = _all_profiles_enabled(parsed)
    sessions = all_sessions()
    if not all_profiles:
        sessions = [
            s for s in sessions
            if _profiles_match(s.get("profile"), active_profile)
        ]
    # Reject a malformed depth instead of letting int() raise ValueError and
    # surface as a confusing 500. Clamp to >= 0 so a negative value can't reach
    # the messages[:depth] slice below — messages[:-n] would silently exclude
    # the most recent messages from the content search instead of capping it.
    # (depth == 0 keeps its existing meaning: search the full transcript.)
    try:
        depth = max(0, int(qs.get("depth", ["5"])[0]))
    except (ValueError, TypeError):
        depth = 5
    # Read the redaction setting ONCE for the whole response (mirrors the
    # /api/sessions read-once optimization, #4662) and thread it through every
    # branch + the shared title-field redactor so search rows redact the same
    # fields as the sidebar list.
    try:
        _search_redact_enabled = bool(load_settings().get("api_redact_enabled", True))
    except Exception:
        _search_redact_enabled = True  # fail safe: redact when settings unreadable
    if not q:
        safe_sessions = []
        for s in sessions:
            item = dict(s)
            if isinstance(item.get("title"), str):
                item["title"] = _redact_text(item["title"], _enabled=_search_redact_enabled)
            sidebar_projection.redact_titles(item, _search_redact_enabled)
            safe_sessions.append(item)
        return j(handler, {
            "sessions": safe_sessions,
            "all_profiles": all_profiles,
            "active_profile": active_profile,
        })
    results = []
    for s in sessions:
        title_match = q in (s.get("title") or "").lower()
        if title_match:
            item = dict(s, match_type="title")
            if isinstance(item.get("title"), str):
                item["title"] = _redact_text(item["title"], _enabled=_search_redact_enabled)
            sidebar_projection.redact_titles(item, _search_redact_enabled)
            results.append(item)
            continue
        if content_search:
            try:
                sess = get_session(s["session_id"])
                msgs = sess.messages[:depth] if depth else sess.messages
                for m in msgs:
                    c = _session_search_message_text(m)
                    if q in str(c).lower():
                        item = dict(s, match_type="content")
                        preview = _session_search_preview(c, q)
                        if preview:
                            item["match_preview"] = _redact_text(preview, _enabled=_search_redact_enabled)
                        if isinstance(item.get("title"), str):
                            item["title"] = _redact_text(item["title"], _enabled=_search_redact_enabled)
                        sidebar_projection.redact_titles(item, _search_redact_enabled)
                        results.append(item)
                        break
            except (KeyError, Exception):
                pass
    return j(handler, {
        "sessions": results,
        "query": q,
        "count": len(results),
        "all_profiles": all_profiles,
        "active_profile": active_profile,
    })


__routes_exports__ = (
    "_handle_session_export",
    "_session_search_message_text",
    "_session_search_preview",
    "_handle_sessions_search",
)
