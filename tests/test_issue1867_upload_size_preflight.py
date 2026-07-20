from tests.frontend_asset_contract import family_source


def _function_body(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.index(marker)
    signature_end = src.index(")", start)
    brace = src.index("{", signature_end)
    depth = 0
    for idx in range(brace, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[brace : idx + 1]
    raise AssertionError(f"{name} function body not found")


def test_upload_limit_constant_matches_server_limit():
    """The browser preflight should read the runtime upload limit."""
    import api.config as config
    from api.config import media_types

    ui = family_source("ui")

    assert "window.__HERMES_CONFIG__.maxUploadBytes" in ui
    assert config.MAX_UPLOAD_BYTES is media_types.MAX_UPLOAD_BYTES
    assert config.MAX_UPLOAD_BYTES > 0


def test_file_picker_rejects_oversize_files_before_queueing():
    """Selecting an oversized file should never add it to pending uploads."""
    src = family_source("ui")
    body = _function_body(src, "addFiles")

    size_gate = body.index("f&&f.size>MAX_UPLOAD_BYTES")
    status_notice = body.index("_showUploadTooLarge(f)")
    push_pending = body.index("S.pendingFiles.push(f)")

    assert size_gate < status_notice < push_pending
    assert "continue;" in body[size_gate:push_pending]


def test_pending_uploads_skip_fetch_for_oversize_files():
    """Restored or queued oversized files should fail locally before fetch()."""
    src = family_source("ui")
    body = _function_body(src, "uploadPendingFiles")

    size_gate = body.index("f&&f.size>MAX_UPLOAD_BYTES")
    form_data = body.index("const fd=new FormData()")
    upload_fetch = body.index("fetch(url")

    assert size_gate < form_data < upload_fetch
    assert "throw new Error(_uploadTooLargeMessage(f))" in body[size_gate:form_data]


def test_upload_too_large_has_user_facing_message():
    """The status toast should explain the upload limit instead of a network reset."""
    i18n = family_source("i18n")
    ui = family_source("ui")

    assert "upload_too_large" in i18n
    assert "Maximum upload size is" in i18n
    assert "_uploadTooLargeMessage(file)" in ui


def test_archive_extraction_limit_tracks_upload_limit(monkeypatch):
    """Archive extraction guard should scale with the configured upload limit."""
    from api.config import MAX_UPLOAD_BYTES
    from api.media.uploads import MAX_EXTRACTED_BYTES, max_extracted_bytes

    monkeypatch.delenv("HERMES_WEBUI_MAX_EXTRACTED_MB", raising=False)

    assert MAX_EXTRACTED_BYTES == 10 * MAX_UPLOAD_BYTES
    assert max_extracted_bytes() == 10 * MAX_UPLOAD_BYTES
