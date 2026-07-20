"""Guarded byte-range, inline preview, session media, and file delivery."""

# Implementations are rebound to the canonical facade for compatibility.
# ruff: noqa: F821

from __future__ import annotations

import re


def _content_disposition_value(disposition: str, filename: str) -> str:
    """Build a latin-1-safe Content-Disposition value with RFC 5987 filename*."""
    import urllib.parse as _up

    safe_name = Path(filename).name.replace("\r", "").replace("\n", "")
    ascii_fallback = "".join(
        ch if 32 <= ord(ch) < 127 and ch not in {'"', '\\'} else "_"
        for ch in safe_name
    ).strip(" .")
    if not ascii_fallback:
        suffix = Path(safe_name).suffix
        ascii_suffix = "".join(
            ch if 32 <= ord(ch) < 127 and ch not in {'"', '\\'} else "_"
            for ch in suffix
        )
        ascii_fallback = f"download{ascii_suffix}" if ascii_suffix else "download"
    quoted_name = _up.quote(safe_name, safe="")
    return (
        f'{disposition}; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{quoted_name}"
    )


def _parse_range_header(range_header: str, file_size: int) -> tuple[int, int] | None:
    """Parse a single HTTP bytes range into inclusive start/end offsets."""
    if not range_header or not range_header.startswith("bytes=") or file_size < 1:
        return None
    spec = range_header.split("=", 1)[1].strip()
    if "," in spec or "-" not in spec:
        return None
    start_s, end_s = spec.split("-", 1)
    try:
        if start_s == "":
            # suffix range: bytes=-500
            suffix_len = int(end_s)
            if suffix_len <= 0:
                return None
            start = max(0, file_size - suffix_len)
            end = file_size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else file_size - 1
            if start < 0:
                return None
            end = min(end, file_size - 1)
        if start > end or start >= file_size:
            return None
        return start, end
    except ValueError:
        return None


def _open_file_read_fd(target: Path, anchor_root: Path | None = None) -> int:
    if anchor_root is None:
        return os.open(str(target), os.O_RDONLY)
    return open_anchored_fd(anchor_root, target.resolve(), want_dir=False)


def _close_fd_quietly(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _serve_file_bytes(handler, target: Path, mime: str, disposition: str, cache_control: str, *, csp: str | None = None, anchor_root: Path | None = None):
    """Serve a file with correct MIME/disposition and optional byte-range support."""
    fd = None
    try:
        fd = _open_file_read_fd(target, anchor_root)
        file_size = os.fstat(fd).st_size
    except PermissionError:
        _close_fd_quietly(fd)
        return bad(handler, "Permission denied", 403)
    except FileNotFoundError:
        _close_fd_quietly(fd)
        return j(handler, {"error": "not found"}, status=404)
    except ValueError as e:
        _close_fd_quietly(fd)
        return bad(handler, _sanitize_error(e), 403)
    except Exception:
        _close_fd_quietly(fd)
        return bad(handler, "Could not stat file", 500)

    try:
        byte_range = _parse_range_header(handler.headers.get("Range", ""), file_size)
        if handler.headers.get("Range") and byte_range is None:
            handler.send_response(416)
            handler.send_header("Content-Range", f"bytes */{file_size}")
            handler.send_header("Accept-Ranges", "bytes")
            handler.send_header("Content-Length", "0")
            _security_headers(handler)
            handler.end_headers()
            return True

        start, end = byte_range if byte_range else (0, max(0, file_size - 1))
        content_length = end - start + 1 if file_size else 0
        handler.send_response(206 if byte_range else 200)
        handler.send_header("Content-Type", mime)
        handler.send_header("Content-Length", str(content_length))
        handler.send_header("Accept-Ranges", "bytes")
        if byte_range:
            handler.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        handler.send_header("Cache-Control", cache_control)
        handler.send_header("Content-Disposition", _content_disposition_value(disposition, target.name))
        if csp:
            # Sandboxed inline HTML must remain frameable for workspace previews;
            # X-Frame-Options: DENY would block the iframe before CSP sandbox applies.
            handler.send_header("Content-Security-Policy", csp)
            handler.send_header("X-Content-Type-Options", "nosniff")
            handler.send_header("Referrer-Policy", "same-origin")
            handler.send_header(
                "Permissions-Policy",
                "camera=(), microphone=(self), geolocation=(), clipboard-write=(self)",
            )
        else:
            _security_headers(handler)
        handler.end_headers()

        if content_length:
            try:
                with os.fdopen(fd, "rb", closefd=True) as f:
                    fd = None
                    f.seek(start)
                    remaining = content_length
                    while remaining:
                        chunk = f.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        handler.wfile.write(chunk)
                        remaining -= len(chunk)
            except PermissionError:
                return True
        return True
    finally:
        _close_fd_quietly(fd)


def _html_preview_with_blank_base(raw: bytes) -> bytes:
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


def _serve_inline_html_preview(handler, target: Path, cache_control: str, *, csp: str, anchor_root: Path | None = None):
    """Serve sandboxed workspace HTML preview with links targeting a new tab."""
    fd = None
    try:
        fd = _open_file_read_fd(target, anchor_root)
        with os.fdopen(fd, "rb", closefd=True) as f:
            fd = None
            body = _html_preview_with_blank_base(f.read())
    except PermissionError:
        return bad(handler, "Permission denied", 403)
    except FileNotFoundError:
        return j(handler, {"error": "not found"}, status=404)
    except ValueError as e:
        return bad(handler, _sanitize_error(e), 403)
    except Exception:
        return bad(handler, "Could not read file", 500)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Accept-Ranges", "none")
    handler.send_header("Cache-Control", cache_control)
    handler.send_header("Content-Disposition", _content_disposition_value("inline", target.name))
    handler.send_header("Content-Security-Policy", csp)
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "same-origin")
    handler.send_header(
        "Permissions-Policy",
        "camera=(), microphone=(self), geolocation=(), clipboard-write=(self)",
    )
    handler.end_headers()
    handler.wfile.write(body)
    return True


_MEDIA_TOKEN_RE = re.compile(r"MEDIA:([^\s\)\]]+)")


def _message_content_text(content) -> str:
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or ""))
            else:
                parts.append(str(part or ""))
        return "\n".join(parts)
    return str(content or "")


def _session_media_token_allows_path(sid: str, target: Path, allowed_mimes: set[str]) -> bool:
    """Allow exact safe MEDIA: paths already present in the requested session."""
    sid = str(sid or "").strip()
    if not sid:
        return False
    mime = MIME_MAP.get(target.suffix.lower(), "application/octet-stream")
    if mime not in allowed_mimes:
        return False
    try:
        target_resolved = target.resolve()
    except Exception:
        return False
    try:
        session = get_session(sid)
    except Exception:
        return False

    for message in getattr(session, "messages", []) or []:
        if not isinstance(message, dict):
            continue
        # Only honor MEDIA: tokens that the assistant/tool emitted. User-authored
        # content cannot mint allow-list entries even if it contains a MEDIA:
        # token — keeps the implicit threat model (assistant-emitted artifacts
        # only) explicit.
        role = str(message.get("role") or "").strip().lower()
        if role == "user":
            continue
        text = _message_content_text(message.get("content"))
        if "MEDIA:" not in text:
            continue
        for ref in _MEDIA_TOKEN_RE.findall(text):
            if "://" in ref:
                continue
            try:
                if Path(ref).expanduser().resolve() == target_resolved:
                    return True
            except Exception:
                continue
    return False


def _session_media_token_allows_image_path(sid: str, target: Path, image_mimes: set[str]) -> bool:
    """Backward-compatible image-only wrapper for existing callers/tests."""
    return _session_media_token_allows_path(sid, target, image_mimes)


def _path_is_within_root(child: Path, root: Path) -> bool:
    """Return True when ``child`` is inside ``root`` without crashing on Windows drives."""
    try:
        return os.path.commonpath([str(child), str(root)]) == str(root)
    except ValueError:
        return False


def _handle_media(handler, parsed):
    """Serve a local file by absolute path for inline display in the chat.

    Security:
    - Path must resolve to an allowed root (hermes home, /tmp, common dirs)
    - Auth-gated when auth is enabled
    - Safe preview MIME types can render inline when requested; SVG always downloads
    - SVG always served as attachment (XSS risk)
    - No path traversal: resolved path must stay within an allowed root
    - Additional roots can be added via MEDIA_ALLOWED_ROOTS env var
      (os.pathsep-separated list of absolute paths; ":" on POSIX, ";" on Windows)
    """
    import os as _os
    from api.auth import is_auth_enabled, parse_cookie, verify_session
    _HOME = Path(_os.path.expanduser("~"))
    _HERMES_HOME = Path(_os.getenv("HERMES_HOME", str(_HOME / ".hermes"))).expanduser()

    # Auth check
    if is_auth_enabled():
        cv = parse_cookie(handler)
        if not (cv and verify_session(cv)):
            body = b'{"error":"Authentication required"}'
            handler.send_response(401)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            return

    qs = parse_qs(parsed.query)
    raw_path = qs.get("path", [""])[0].strip()
    if not raw_path:
        return bad(handler, "path parameter required", 400)

    # Resolve the path and check it is within an allowed root
    try:
        target = Path(raw_path).resolve()
    except Exception:
        return bad(handler, "Invalid path", 400)

    # Allowed roots: hermes home, /tmp, and active workspace.
    # Intentionally NOT the entire home dir — that would expose ~/.ssh,
    # ~/.aws, browser profiles, etc. to any authenticated user.
    allowed_roots = [
        _HERMES_HOME.resolve(),
        Path("/tmp").resolve(),
        (_HOME / ".hermes").resolve(),
    ]
    # Also allow the active workspace directory (where screenshots land)
    try:
        from api.workspace import get_last_workspace
        ws = Path(get_last_workspace()).resolve()
        if ws.is_dir():
            allowed_roots.append(ws)
    except Exception:
        pass

    # Also allow additional roots from MEDIA_ALLOWED_ROOTS env var
    # (os.pathsep-separated list; ":" on POSIX, ";" on Windows).
    extra_roots = _os.environ.get("MEDIA_ALLOWED_ROOTS", "").strip()
    if extra_roots:
        for root in extra_roots.split(_os.pathsep):
            root = root.strip()
            if root:
                try:
                    rp = Path(root).resolve()
                    if rp.is_dir():
                        allowed_roots.append(rp)
                except Exception:
                    pass

    _INLINE_IMAGE_TYPES = {
        "image/png", "image/jpeg", "image/gif", "image/webp",
        "image/x-icon", "image/bmp",
    }
    within_allowed = any(
        _path_is_within_root(target, root)
        for root in allowed_roots
        if root.exists()
    )
    _AUDIO_VIDEO_PDF_TYPES = {
        "audio/mpeg", "audio/wav", "audio/x-wav", "audio/mp4", "audio/aac",
        "audio/ogg", "audio/opus", "audio/flac",
        "video/mp4", "video/quicktime", "video/webm", "video/ogg",
        "application/pdf",
    }
    _SESSION_MEDIA_TOKEN_TYPES = _INLINE_IMAGE_TYPES | _AUDIO_VIDEO_PDF_TYPES | {"text/html"}
    session_media_allowed = _session_media_token_allows_path(
        qs.get("session_id", [""])[0],
        target,
        _SESSION_MEDIA_TOKEN_TYPES,
    )

    # ── #3234: hard-deny Hermes's own state + secret/config files ────────────
    # The allowlist above grants the whole Hermes home (and base ~/.hermes), so
    # an authenticated session rendering attacker-influenced agent output that
    # emits a file:// / MEDIA: link to a state/secret file could fetch it
    # through /api/media. This guard runs BEFORE the allow/serve decision so it
    # covers every entry path (bare file:// URLs, markdown anchors, MEDIA:
    # tokens, and session-token grants).
    #
    # Model: the ACTIVE WORKSPACE is a legitimate-media carve-out — the user is
    # entitled to their own workspace files (that is also how the workspace file
    # browser reaches them), even when a workspace happens to live under a
    # Hermes root. The deny rules target Hermes's OWN internal state, which lives
    # OUTSIDE any workspace. So: if the target is inside the active workspace, it
    # is never denied here; otherwise we deny known secret/config basenames and
    # the internal state subdirectories across every Hermes root the allowlist
    # accepts (active-profile HERMES_HOME, base ~/.hermes, the api.profiles
    # default home, and STATE_DIR — which also defends sibling profiles).
    _DENY_FILENAMES = {
        "settings.json", "state.db", "state.db-wal", "state.db-shm",
        "auth.json", "auth.lock", "config.yaml", "config.yml", ".env",
        ".signing_key", ".pbkdf2_key", ".sessions.json",
        "google_token.json", "google_client_secret.json",
        "gateway_state.json", "channel_directory.json", "jobs.json",
        "passkeys.json", ".passkey_challenges.json", ".login_attempts.json",
    }
    # Internal state subdirs that are sensitive in their entirety. NOTE:
    # `profiles` is intentionally NOT here — it is a container of profile roots,
    # each of which has its own legitimate workspace/. We instead enumerate each
    # named-profile root below and deny ITS state subdirs, so a sibling profile's
    # secrets are blocked without 403-ing a named-profile workspace. (#3234.)
    _DENY_SUBDIRS = (
        "sessions", "memories", "cron", "logs",
        "checkpoints", "backups",
    )
    _state_dir = None
    try:
        from api.config import STATE_DIR as _STATE_DIR
        _state_dir = Path(_STATE_DIR).resolve()
    except Exception:
        _state_dir = None
    _base_hermes_home = None
    try:
        from api.profiles import _DEFAULT_HERMES_HOME as _BASE_HH
        _base_hermes_home = Path(_BASE_HH).resolve()
    except Exception:
        _base_hermes_home = None
    _hermes_roots = []
    for _r in (
        _HERMES_HOME.resolve(),
        (_HOME / ".hermes").resolve(),
        _base_hermes_home,
        _state_dir,
    ):
        if _r is not None and _r not in _hermes_roots:
            _hermes_roots.append(_r)
    # Enumerate named-profile roots (<root>/profiles/<name>) and treat each as a
    # Hermes root in its own right, so a sibling/other profile's sensitive subdirs
    # + secret files are denied — WITHOUT denying the whole `profiles` container
    # (which would block a legit named-profile workspace at
    # <root>/profiles/<name>/workspace/). (Codex review #3234.)
    _profile_roots = []
    for _root in list(_hermes_roots):
        _profiles_dir = (_root / "profiles")
        try:
            if _profiles_dir.is_dir():
                for _pchild in _profiles_dir.iterdir():
                    if _pchild.is_dir():
                        _pr = _pchild.resolve()
                        if _pr not in _hermes_roots and _pr not in _profile_roots:
                            _profile_roots.append(_pr)
        except OSError:
            pass
    _hermes_roots.extend(_profile_roots)

    # Case-insensitive path helpers so STATE.DB / Sessions/ casing variants
    # cannot bypass the deny on macOS/Windows filesystems (Codex review #3234).
    def _norm(p):
        return os.path.normcase(str(Path(p).resolve())).casefold()
    def _within_ci(child, root):
        try:
            c, r = _norm(child), _norm(root)
            return os.path.commonpath([c, r]) == r
        except (ValueError, OSError):
            return False
    def _equal_ci(a, b):
        try:
            return _norm(a) == _norm(b)
        except (ValueError, OSError):
            return False

    # State-subdir deny set: each DENY_SUBDIR directly under any Hermes root
    # (which includes STATE_DIR — so STATE_DIR/sessions, STATE_DIR/memories,
    # etc. are covered). These ALWAYS apply — even to a file under the active
    # workspace — so a workspace pointed at (or overlapping) a state dir cannot
    # expose sessions/memories/profiles/etc. We do NOT deny STATE_DIR itself
    # wholesale: the default workspace lives at STATE_DIR/workspace, and that is
    # legitimate user media — direct sensitive files there are still caught by
    # the filename denies below. (Codex review #3234.)
    _deny_dirs = []
    for _root in _hermes_roots:
        for _sub in _DENY_SUBDIRS:
            _deny_dirs.append((_root / _sub).resolve())
        # Per-profile WebUI state lives at <root>/webui_state (api/workspace.py),
        # so its state subdirs (<root>/webui_state/sessions, etc.) must be denied
        # too — they are NOT direct children of <root>. (Codex review #3234.)
        _ws_state = (_root / "webui_state")
        for _sub in _DENY_SUBDIRS:
            _deny_dirs.append((_ws_state / _sub).resolve())
    _deny_names_ci = {n.casefold() for n in _DENY_FILENAMES}

    # Active-workspace carve-out: a file inside a genuine PROJECT workspace is
    # the user's own content, so the secret/config FILENAME denies are relaxed
    # for it. The carve-out is DISABLED when the workspace is a broad/internal
    # location ($HOME, a Hermes root itself, an ANCESTOR of a Hermes root, a
    # */profiles dir, a named-profile root, or a state subdir) — honoring those
    # would re-open the disclosure. A workspace that is a proper DESCENDANT of a
    # Hermes root (e.g. STATE_DIR/workspace) is still a legit project workspace
    # and keeps the carve-out. The dir-based denies above are NOT relaxed.
    _active_workspace = None
    try:
        from api.workspace import get_last_workspace
        _aw = Path(get_last_workspace()).resolve()
        if _aw.is_dir():
            _active_workspace = _aw
    except Exception:
        _active_workspace = None

    def _workspace_is_safe_carveout(ws):
        if ws is None:
            return False
        if _equal_ci(ws, _HOME):
            return False
        for _root in _hermes_roots:
            # ws IS a root, or ws is an ANCESTOR of a root → unsafe. (A proper
            # descendant of a root is fine — that's a normal project workspace.)
            if _equal_ci(ws, _root) or _within_ci(_root, ws):
                return False
        if ws.name == "profiles" or ws.parent.name == "profiles":
            return False
        if ws.name in _DENY_SUBDIRS:
            return False
        return True

    _in_active_workspace = (
        _active_workspace is not None
        and _workspace_is_safe_carveout(_active_workspace)
        and _within_ci(target, _active_workspace)
    )

    # Dir-based denies always fire (even inside the active workspace).
    if any(_within_ci(target, d) for d in _deny_dirs):
        return bad(handler, "Path not in allowed location", 403)
    # Filename-based denies fire for files under a Hermes root, UNLESS the file
    # is inside a genuine project workspace (carve-out).
    if not _in_active_workspace:
        _under_hermes_root = any(_within_ci(target, _root) for _root in _hermes_roots)
        _name_cf = target.name.casefold()
        # Exact secret/state basenames, plus atomic-write temp files for those
        # (api/auth.py and api/passkeys.py write via a `tmp*.<name>.tmp` / `tmp*.tmp`
        # sidecar then rename) — deny those suffixes too so a momentary temp file
        # cannot be fetched. (Codex review #3234.)
        _deny_tmp_suffixes = (".sessions.tmp", ".login_attempts.tmp",
                              ".passkeys.tmp", ".passkey_challenges.tmp")
        if _under_hermes_root and (
            _name_cf in _deny_names_ci
            or _name_cf.endswith(_deny_tmp_suffixes)
        ):
            return bad(handler, "Path not in allowed location", 403)
    # ── end #3234 deny ───────────────────────────────────────────────────────

    if not within_allowed and not session_media_allowed:
        return bad(handler, "Path not in allowed location", 403)

    if not target.exists() or not target.is_file():
        return j(handler, {"error": "not found"}, status=404)

    # Determine MIME type
    ext = target.suffix.lower()
    mime = MIME_MAP.get(ext, "application/octet-stream")

    # Only serve safe media/PDF types inline when explicitly requested. HTML is
    # allowed inline only with a CSP sandbox so "open full page" can work without
    # granting same-origin access to the WebUI. SVG is always a download (XSS risk).
    _INLINE_PREVIEW_TYPES = _INLINE_IMAGE_TYPES | _AUDIO_VIDEO_PDF_TYPES
    _DOWNLOAD_TYPES = {"image/svg+xml"}  # SVG: XSS risk, force download
    inline_preview = qs.get("inline", [""])[0] == "1"
    html_inline_ok = inline_preview and mime == "text/html"
    disposition = "inline" if (
        mime not in _DOWNLOAD_TYPES and (
            mime in _INLINE_IMAGE_TYPES or (inline_preview and mime in _INLINE_PREVIEW_TYPES)
            or html_inline_ok
        )
    ) else "attachment"
    # _serve_file_bytes sends Content-Security-Policy when csp is set.
    csp = "sandbox allow-scripts" if html_inline_ok else None
    return _serve_file_bytes(handler, target, mime, disposition, "private, max-age=3600", csp=csp)


def _file_raw_target(session, sid: str, rel: str) -> tuple[Path, Path] | None:
    """Resolve /api/file/raw paths from the workspace or this session's uploads."""
    workspace_root = Path(session.workspace)
    try:
        target = safe_resolve(workspace_root, rel)
    except ValueError:
        target = None
    if target and target.exists() and target.is_file():
        return workspace_root, target

    # Chat uploads now live in a per-session attachment inbox outside the
    # workspace. Keep the public URL stable while scoping fallback lookup to
    # the requesting session's own attachment directory.
    try:
        from api.upload import _session_attachment_dir

        attachment_root = _session_attachment_dir(sid)
        attachment_target = safe_resolve(attachment_root, rel)
    except Exception:
        return None
    if attachment_target.exists() and attachment_target.is_file():
        return attachment_root, attachment_target
    return None


# ─── /api/folder/download ───────────────────────────────────────────────────
# Configurable caps. Match the HERMES_WEBUI_MAX_UPLOAD_MB style used elsewhere
# (api/config.py) so operators have one consistent env-var convention.
# Bound on per-request wall-clock and bandwidth, not RSS. The zip streams
# straight into handler.wfile, so peak memory is the per-file read buffer
# inside zipfile, not the cap value.
def _folder_zip_max_bytes() -> int:
    try:
        mb = int(os.getenv("HERMES_WEBUI_FOLDER_ZIP_MAX_MB", "1024"))
    except ValueError:
        mb = 1024
    return max(1, mb) * 1024 * 1024


def _folder_zip_max_files() -> int:
    try:
        return max(1, int(os.getenv("HERMES_WEBUI_FOLDER_ZIP_MAX_FILES", "50000")))
    except ValueError:
        return 50000


def _folder_download_collect(target: Path, workspace_root: Path,
                              max_bytes: int, max_files: int):
    """Walk target dir; return (files, total_bytes, hit_limit_reason_or_None).

    files is a list of (filesystem_path, archive_name) tuples. Each filesystem
    path is reopened through the workspace anchor when streamed into the ZIP.
    Symlinks escaping the workspace are skipped.
    """
    import os as _os
    files = []
    total_bytes = 0
    for root, dirs, names in _os.walk(target, followlinks=False):
        root_path = Path(root)
        try:
            if not root_path.resolve().is_relative_to(workspace_root):
                dirs[:] = []
                continue
        except (ValueError, OSError):
            dirs[:] = []
            continue
        for name in names:
            fp = root_path / name
            if fp.is_symlink():
                try:
                    if not fp.resolve().is_relative_to(workspace_root):
                        continue
                except (ValueError, OSError):
                    continue
            try:
                size = fp.stat().st_size
            except OSError:
                continue
            if len(files) >= max_files:
                return files, total_bytes, "max_files"
            if total_bytes + size > max_bytes:
                return files, total_bytes, "max_bytes"
            try:
                arcname = fp.relative_to(target)
            except ValueError:
                continue
            files.append((fp, str(arcname)))
            total_bytes += size
    return files, total_bytes, None


def _handle_folder_download(handler, parsed):
    """GET /api/folder/download?session_id=...&path=...

    Streams a zip of <session.workspace>/<path>. Symlinks escaping the
    workspace are skipped. Empty folders return an empty (valid) zip.
    Respects HERMES_WEBUI_FOLDER_ZIP_MAX_MB and HERMES_WEBUI_FOLDER_ZIP_MAX_FILES.
    Pre-flights the walk so size/count failures return a clean 413 with JSON
    body BEFORE any zip bytes are sent.
    """
    import zipfile
    from urllib.parse import parse_qs

    qs = parse_qs(parsed.query)
    sid = qs.get("session_id", [""])[0]
    if not sid:
        return bad(handler, "session_id is required")
    try:
        s = get_session_for_file_ops(sid)
    except KeyError:
        return bad(handler, "Session not found", 404)

    rel = qs.get("path", [""])[0]
    try:
        target = safe_resolve(Path(s.workspace), rel)
    except ValueError:
        return bad(handler, "invalid path", 400)
    if not target.exists():
        return j(handler, {"error": "not found"}, status=404)
    if not target.is_dir():
        return bad(handler, "path must be a directory; use /api/file/raw for single files", 400)

    workspace_root = Path(s.workspace).resolve()
    max_bytes = _folder_zip_max_bytes()
    max_files = _folder_zip_max_files()

    files, total_bytes, limit_hit = _folder_download_collect(
        target, workspace_root, max_bytes, max_files
    )
    if limit_hit == "max_files":
        return j(handler, {
            "error": "too many files",
            "limit": max_files,
            "configure": "HERMES_WEBUI_FOLDER_ZIP_MAX_FILES",
        }, status=413)
    if limit_hit == "max_bytes":
        return j(handler, {
            "error": "folder too large",
            "limit_bytes": max_bytes,
            "configure": "HERMES_WEBUI_FOLDER_ZIP_MAX_MB",
        }, status=413)

    zip_name = (target.name or "workspace") + ".zip"
    handler.send_response(200)
    handler.send_header("Content-Type", "application/zip")
    handler.send_header(
        "Content-Disposition",
        _content_disposition_value("attachment", zip_name),
    )
    handler.send_header("Cache-Control", "no-store")
    # Under HTTP/1.1 (Handler.protocol_version, see server.py post-#2836)
    # a response with no Content-Length and no Transfer-Encoding requires
    # Connection: close so the client knows the body ends at FIN. The ZIP
    # is built on-the-fly so we cannot send Content-Length up front; mirror
    # the SSE-endpoint pattern #2836 uses. Without this header the client
    # hangs waiting for the next pipelined response after the central
    # directory bytes finish. Caught by Opus pre-release advisor on
    # stage-batch11.
    handler.send_header("Connection", "close")
    handler.end_headers()

    written = 0
    with zipfile.ZipFile(handler.wfile, mode="w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for fp, arcname in files:
            fd = None
            try:
                fd = open_anchored_fd(workspace_root, fp.resolve(), want_dir=False)
                info = zipfile.ZipInfo(arcname)
                info.compress_type = zipfile.ZIP_DEFLATED
                with os.fdopen(fd, "rb", closefd=True) as src:
                    fd = None
                    with zf.open(info, "w") as dst:
                        shutil.copyfileobj(src, dst, length=1024 * 1024)
                written += 1
            except (ValueError, OSError, PermissionError) as e:
                logger.warning("folder-download: skipping %s: %s", fp, e)
            finally:
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
    logger.info(
        "folder-download: streamed %d/%d files (~%d bytes) from %s",
        written, len(files), total_bytes, target,
    )


def _handle_file_raw(handler, parsed):
    qs = parse_qs(parsed.query)
    sid = qs.get("session_id", [""])[0]
    if not sid:
        return bad(handler, "session_id is required")
    try:
        s = get_session_for_file_ops(sid)
    except KeyError:
        return bad(handler, "Session not found", 404)
    rel = qs.get("path", [""])[0]
    force_download = qs.get("download", [""])[0] == "1"
    resolved = _file_raw_target(s, sid, rel)
    if resolved is None:
        return j(handler, {"error": "not found"}, status=404)
    anchor_root, target = resolved
    ext = target.suffix.lower()
    mime = MIME_MAP.get(ext, "application/octet-stream")
    # Security: force download for dangerous MIME types to prevent XSS.
    # Exception: ?inline=1 permits text/html to be served inline for the
    # sandboxed workspace HTML preview iframe (sandbox="allow-scripts" with no
    # allow-same-origin, so the iframe cannot access parent cookies/storage).
    inline_preview = qs.get("inline", [""])[0] == "1"
    dangerous_types = {"text/html", "application/xhtml+xml", "image/svg+xml"}
    html_inline_ok = inline_preview and mime == "text/html"
    disposition = "attachment" if force_download or (mime in dangerous_types and not html_inline_ok) else "inline"
    # Defense-in-depth for ?inline=1 HTML: even though the workspace.js iframe
    # sets sandbox="allow-scripts", a user could be tricked into opening the
    # ?inline=1 URL directly in a top-level tab (e.g. via a chat link), which
    # would render the HTML in the WebUI's origin without iframe sandbox. The
    # CSP sandbox directive applies the same isolation server-side: without
    # allow-same-origin, the document is treated as a unique opaque origin and
    # cannot read WebUI cookies, localStorage, or postMessage to the parent.
    sandbox_csp = "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox"
    csp = sandbox_csp if (inline_preview and not force_download and disposition == "inline") else None
    # _serve_file_bytes sends Content-Security-Policy when csp is set.
    if html_inline_ok:
        return _serve_inline_html_preview(handler, target, "no-store", csp=sandbox_csp, anchor_root=anchor_root)
    return _serve_file_bytes(handler, target, mime, disposition, "no-store", csp=csp, anchor_root=anchor_root)


def _handle_file_read(handler, parsed):
    qs = parse_qs(parsed.query)
    sid = qs.get("session_id", [""])[0]
    if not sid:
        return bad(handler, "session_id is required")
    try:
        s = get_session_for_file_ops(sid)
    except KeyError:
        return bad(handler, "Session not found", 404)
    rel = qs.get("path", [""])[0]
    if not rel:
        return bad(handler, "path is required")
    try:
        return j(handler, read_file_content(Path(s.workspace), rel))
    except ImportError as e:
        return bad(handler, str(e), 503)
    except (FileNotFoundError, ValueError) as e:
        return bad(handler, _sanitize_error(e), 404)


def _read_anchored_file_bytes(ws_root: Path, target: Path) -> bytes:
    fd = open_anchored_fd(ws_root, target, want_dir=False)
    with os.fdopen(fd, "rb", closefd=True) as fh:
        st = os.fstat(fh.fileno())
        if not _stat.S_ISREG(st.st_mode):
            raise FileNotFoundError(f"Not a file: {target}")
        if st.st_size > MAX_FILE_BYTES:
            raise ValueError(f"File too large ({st.st_size} bytes, max {MAX_FILE_BYTES})")
        return fh.read(MAX_FILE_BYTES + 1)


__routes_exports__ = (
    "_content_disposition_value",
    "_parse_range_header",
    "_open_file_read_fd",
    "_close_fd_quietly",
    "_serve_file_bytes",
    "_html_preview_with_blank_base",
    "_serve_inline_html_preview",
    "_MEDIA_TOKEN_RE",
    "_message_content_text",
    "_session_media_token_allows_path",
    "_session_media_token_allows_image_path",
    "_path_is_within_root",
    "_handle_media",
    "_file_raw_target",
    "_folder_zip_max_bytes",
    "_folder_zip_max_files",
    "_folder_download_collect",
    "_handle_folder_download",
    "_handle_file_raw",
    "_handle_file_read",
    "_read_anchored_file_bytes",
)
